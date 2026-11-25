"""Off-site copies of the nightly backup, so losing the server's disk doesn't lose everything.

Any S3-compatible storage works; Cloudflare R2 and Backblaze B2 both have free tiers that fit a hub.
Set these as variables on the hub (on Railway: the service's Variables):

  RELAY_OFFSITE_URL     the bucket's address: https://<account>.r2.cloudflarestorage.com/<bucket>
                        or https://s3.<region>.backblazeb2.com/<bucket>
  RELAY_OFFSITE_KEY_ID  the access key's id
  RELAY_OFFSITE_SECRET  the access key's secret
  RELAY_OFFSITE_REGION  "auto" for R2 (the default); the bucket's region for B2, e.g. us-west-004

Each copy is encrypted before it leaves (AES-256-GCM), with a key made from RELAY_ADMIN_TOKEN. That
token lives in the hub's variables, not on its disk, so a lost disk doesn't take the key with it, and
the storage company can't read what it keeps. The last 7 nights are kept there too.

To restore: download the newest relay-YYYY-MM-DD.zip.enc from the bucket, then
  RELAY_ADMIN_TOKEN=<the token> python -m relay.offsite open relay-2026-11-25.zip.enc backup.zip
and restore backup.zip as backup.py says (relay.db and hub_secret into the data folder).
"""
from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

KEEP = 7
MAGIC = b"RELAYBAK1"
STATUS = "offsite.json"
TIMEOUT = 60
EMPTY = hashlib.sha256(b"").hexdigest()


# ------------------------------------------------------------------ the lock on each copy
def _key(admin_token: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"relay-offsite", info=b"backup v1").derive(admin_token.encode())


def seal(data: bytes, admin_token: str) -> bytes:
    nonce = os.urandom(12)
    return MAGIC + nonce + AESGCM(_key(admin_token)).encrypt(nonce, data, MAGIC)


def unseal(blob: bytes, admin_token: str) -> bytes:
    if not blob.startswith(MAGIC):
        raise ValueError("not a Relay off-site backup")
    n = len(MAGIC)
    return AESGCM(_key(admin_token)).decrypt(blob[n:n + 12], blob[n + 12:], MAGIC)


# ------------------------------------------------------------------ S3, signed (AWS Signature Version 4)
def _q(s: str, safe: str = "-_.~") -> str:
    return urllib.parse.quote(s, safe=safe)


def sign(method: str, url: str, key_id: str, secret: str, region: str, payload_hash: str = EMPTY,
         headers: Optional[dict] = None, now: Optional[datetime.datetime] = None) -> dict:
    """The headers that make a request to S3 (or R2, B2) authentic: SigV4, as AWS documents it."""
    u = urllib.parse.urlsplit(url)
    now = now or datetime.datetime.now(datetime.timezone.utc)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    day = stamp[:8]
    hs = {k.lower(): str(v).strip() for k, v in (headers or {}).items()}
    hs.update({"host": u.netloc, "x-amz-date": stamp, "x-amz-content-sha256": payload_hash})
    names = sorted(hs)
    query = "&".join(f"{_q(k)}={_q(v)}" for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    request = "\n".join([method, _q(urllib.parse.unquote(u.path or "/"), "/-_.~"), query,
                         "".join(f"{k}:{hs[k]}\n" for k in names), ";".join(names), payload_hash])
    scope = f"{day}/{region}/s3/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(request.encode()).hexdigest()])
    k = ("AWS4" + secret).encode()
    for part in (day, region, "s3", "aws4_request"):
        k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    out = {k_: v for k_, v in hs.items() if k_ != "host"}
    out["Authorization"] = f"AWS4-HMAC-SHA256 Credential={key_id}/{scope}, SignedHeaders={';'.join(names)}, Signature={sig}"
    return out


class Offsite:
    def __init__(self, url: str, key_id: str, secret: str, region: str = "auto", opener: Optional[Callable] = None):
        self.base = url.rstrip("/")
        if not self.base.startswith("https://"):
            raise ValueError("RELAY_OFFSITE_URL must be an https address")
        self.key_id, self.secret, self.region = key_id, secret, region or "auto"
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))

    @classmethod
    def from_env(cls, env=os.environ, opener=None) -> Optional["Offsite"]:
        url, kid, sec = env.get("RELAY_OFFSITE_URL"), env.get("RELAY_OFFSITE_KEY_ID"), env.get("RELAY_OFFSITE_SECRET")
        return cls(url, kid, sec, env.get("RELAY_OFFSITE_REGION", "auto"), opener) if url and kid and sec else None

    def _do(self, method: str, path: str = "", body: bytes = b"", query: str = "") -> bytes:
        url = self.base + (("/" + _q(path, "/-_.~")) if path else "/") + (("?" + query) if query else "")
        h = sign(method, url, self.key_id, self.secret, self.region, hashlib.sha256(body).hexdigest())
        req = urllib.request.Request(url, data=body if method == "PUT" else None, method=method, headers=h)
        with self.open(req) as r:
            return r.read()

    def put(self, name: str, data: bytes) -> None:
        self._do("PUT", name, data)

    def delete(self, name: str) -> None:
        self._do("DELETE", name)

    def names(self, prefix: str = "relay-") -> list:
        xml = self._do("GET", "", query=f"list-type=2&prefix={_q(prefix)}").decode("utf-8", "replace")
        return sorted(re.findall(r"<Key>([^<]+)</Key>", xml))


# ------------------------------------------------------------------ what the hub does each hour
def status(data_dir: str) -> dict:
    try:
        with open(os.path.join(data_dir, STATUS)) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _say(data_dir: str, **kw) -> None:
    st = {**status(data_dir), **kw}
    with open(os.path.join(data_dir, STATUS), "w") as f:
        json.dump(st, f)


def sync(data_dir: str, store: Optional[Offsite], admin_token: str, keep: int = KEEP, now=time.time) -> Optional[str]:
    """Send the newest nightly backup if it isn't there yet, then keep only the last `keep` there.
    Returns what was sent, or None. Never raises: a failed copy is written down for the owner to see."""
    if not store:
        return None
    folder = os.path.join(data_dir, "backups")
    local = sorted(f for f in os.listdir(folder) if f.startswith("relay-") and f.endswith(".zip")) if os.path.isdir(folder) else []
    if not local:
        return None
    name = local[-1] + ".enc"
    if status(data_dir).get("name") == name and status(data_dir).get("ok"):
        return None
    try:
        with open(os.path.join(folder, local[-1]), "rb") as f:
            store.put(name, seal(f.read(), admin_token))
        there = store.names()
        for old in [n for n in there if n.endswith(".zip.enc")][:-keep]:
            store.delete(old)
        _say(data_dir, name=name, ok=now(), error=None)
        return name
    except urllib.error.HTTPError as e:
        _say(data_dir, error=f"The storage answered {e.code}. Check the bucket address and key.", failed=now())
    except Exception as e:
        _say(data_dir, error=f"Couldn't reach the storage: {e}"[:200], failed=now())
    return None


if __name__ == "__main__":      # python -m relay.offsite open <file.zip.enc> <out.zip>
    if len(sys.argv) != 4 or sys.argv[1] != "open":
        sys.exit("usage: RELAY_ADMIN_TOKEN=... python -m relay.offsite open <backup.zip.enc> <backup.zip>")
    token = os.environ.get("RELAY_ADMIN_TOKEN") or sys.exit("set RELAY_ADMIN_TOKEN to the hub's admin token")
    with open(sys.argv[2], "rb") as f:
        plain = unseal(f.read(), token)
    with open(sys.argv[3], "wb") as f:
        f.write(plain)
    print(f"opened: {sys.argv[3]}")
