"""Notifications on a person's phone or computer when something needs them: an agent asking to use one
of their keys, an agent answering them, someone asking to join.

Standard Web Push with no outside service. The hub signs every send with its own key (VAPID, RFC 8292),
made once and kept in the data folder like hub_secret, and encrypts each message for the one device it
goes to (RFC 8291, aes128gcm), so the push service in between (Apple, Google, Mozilla) carries it without
being able to read it. Sends happen on a background thread; a device the push service says is gone
(404/410) is forgotten.
"""
import base64
import hashlib
import hmac
import json
import os
import queue
import struct
import threading
import time
import urllib.parse
import urllib.request
from typing import Callable, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand

KINDS = ("approvals", "answers", "invites")
TTL = 24 * 3600            # an approval waits a day; a notice older than that isn't worth showing
TIMEOUT = 10
FAILS_BEFORE_FORGET = 10   # a device that keeps failing (not 404/410) is dropped after this many in a row
ANSWER_GAP = 60            # at most one "answered you" per conversation per person per minute
# Only real push services: a subscription is a URL the hub will POST to, so it can't be just any address.
PUSH_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "push.services.mozilla.com", "push.apple.com",
              "notify.windows.com")

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs(
  endpoint TEXT PRIMARY KEY, handle TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL,
  kinds TEXT NOT NULL, created REAL NOT NULL, last_ok REAL, fails INTEGER NOT NULL DEFAULT 0, label TEXT);
CREATE INDEX IF NOT EXISTS push_subs_handle ON push_subs(handle);
"""


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def unb64u(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _extract(salt: bytes, ikm: bytes) -> bytes:
    return hmac.new(salt, ikm, hashlib.sha256).digest()


def _expand(prk: bytes, info: bytes, n: int) -> bytes:
    return HKDFExpand(hashes.SHA256(), n, info).derive(prk)


def encrypt(payload: bytes, p256dh: str, auth: str, salt: Optional[bytes] = None,
            server_key: Optional[ec.EllipticCurvePrivateKey] = None) -> bytes:
    """RFC 8291: the message, readable only by the device that made this subscription."""
    ua_pub = unb64u(p256dh)
    as_priv = server_key or ec.generate_private_key(ec.SECP256R1())
    as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = as_priv.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_pub))
    ikm = _expand(_extract(unb64u(auth), shared), b"WebPush: info\x00" + ua_pub + as_pub, 32)
    salt = salt or os.urandom(16)
    prk = _extract(salt, ikm)
    cek = _expand(prk, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _expand(prk, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)          # one record, so it is the last
    return salt + struct.pack(">I", 4096) + bytes([len(as_pub)]) + as_pub + body


def push_host_ok(endpoint: str) -> bool:
    u = urllib.parse.urlsplit(endpoint)
    host = (u.hostname or "").lower()
    return u.scheme == "https" and any(host == h or host.endswith("." + h) for h in PUSH_HOSTS)


class Pusher:
    def __init__(self, store, data_dir: str, contact: Callable[[], str], opener: Optional[Callable] = None,
                 background: bool = True):
        self.store = store
        self.contact = contact                   # who runs this hub, for the push services: a URL or mailto:
        self.badge_for: Optional[Callable[[str], int]] = None   # how many things wait for a person (the icon's number)
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))
        path = os.path.join(data_dir, "push_key.pem")
        if os.path.exists(path):
            with open(path, "rb") as f:
                self.key = serialization.load_pem_private_key(f.read(), password=None)
        else:
            self.key = ec.generate_private_key(ec.SECP256R1())
            pem = self.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                         serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
        self.public = b64u(self.key.public_key().public_bytes(serialization.Encoding.X962,
                                                              serialization.PublicFormat.UncompressedPoint))
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
        self.q: "queue.Queue" = queue.Queue()
        self.last_answer: dict = {}
        self.background = background
        if background:
            threading.Thread(target=self._run, name="relay-push", daemon=True).start()

    # ------------------------------------------------------------ devices
    def subscribe(self, handle: str, endpoint: str, p256dh: str, auth: str, kinds=KINDS, label: Optional[str] = None) -> dict:
        if not push_host_ok(endpoint):
            raise ValueError("That isn't a push service this hub sends to.")
        try:
            ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), unb64u(p256dh))
            if len(unb64u(auth)) != 16:
                raise ValueError
        except Exception:
            raise ValueError("The device's push keys don't look right.") from None
        kinds = [k for k in kinds if k in KINDS]
        with self.store.tx() as c:
            c.execute("INSERT INTO push_subs(endpoint,handle,p256dh,auth,kinds,created,label) VALUES (?,?,?,?,?,?,?)"
                      " ON CONFLICT(endpoint) DO UPDATE SET handle=excluded.handle, p256dh=excluded.p256dh,"
                      " auth=excluded.auth, kinds=excluded.kinds, fails=0, label=excluded.label",
                      (endpoint, handle, p256dh, auth, json.dumps(kinds), time.time(), (label or "")[:60] or None))
        return {"endpoint": endpoint, "kinds": kinds}

    def device(self, handle: str, endpoint: str) -> Optional[dict]:
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM push_subs WHERE endpoint=? AND handle=?", (endpoint, handle)).fetchone()
        return {"endpoint": r["endpoint"], "kinds": json.loads(r["kinds"]), "last_ok": r["last_ok"]} if r else None

    def set_kinds(self, handle: str, endpoint: str, kinds) -> Optional[dict]:
        kinds = [k for k in kinds if k in KINDS]
        with self.store.tx() as c:
            n = c.execute("UPDATE push_subs SET kinds=? WHERE endpoint=? AND handle=?",
                          (json.dumps(kinds), endpoint, handle)).rowcount
        return {"endpoint": endpoint, "kinds": kinds} if n else None

    def unsubscribe(self, handle: str, endpoint: str) -> bool:
        with self.store.tx() as c:
            return c.execute("DELETE FROM push_subs WHERE endpoint=? AND handle=?", (endpoint, handle)).rowcount > 0

    def forget_person(self, handle: str) -> None:
        with self.store.tx() as c:
            c.execute("DELETE FROM push_subs WHERE handle=?", (handle,))

    # ------------------------------------------------------------ sending
    def notify(self, handle: str, kind: str, title: str, body: str, url: str, tag: Optional[str] = None,
               urgent: bool = False) -> int:
        """Queue a notification to every device of `handle` that wants this kind. Returns how many."""
        if kind == "answers" and tag:
            key, now = (handle, tag), time.monotonic()
            if now - self.last_answer.get(key, -1e9) < ANSWER_GAP:
                return 0
            self.last_answer[key] = now
        with self.store.ro() as c:
            subs = [dict(r) for r in c.execute("SELECT * FROM push_subs WHERE handle=?", (handle,))
                    if kind in json.loads(r["kinds"])]
        note = {"title": title[:120], "body": body[:240], "url": url, "tag": tag or kind}
        if self.badge_for:                       # the number for the app icon
            try:
                note["badge"] = int(self.badge_for(handle))
            except Exception:
                pass
        payload = json.dumps(note).encode()
        for s in subs:
            item = (s, payload, tag, urgent)
            self._send(*item) if not self.background else self.q.put(item)
        return len(subs)

    def _jwt(self, aud: str) -> str:
        head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64u(json.dumps({"aud": aud, "exp": int(time.time()) + 12 * 3600, "sub": self.contact()},
                                 separators=(",", ":")).encode())
        r, s = decode_dss_signature(self.key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
        return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"

    def _send(self, sub: dict, payload: bytes, tag: Optional[str], urgent: bool) -> Optional[int]:
        u = urllib.parse.urlsplit(sub["endpoint"])
        headers = {"Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream", "TTL": str(TTL),
                   "Urgency": "high" if urgent else "normal",
                   "Authorization": f"vapid t={self._jwt(f'{u.scheme}://{u.netloc}')}, k={self.public}"}
        if tag:      # a newer one for the same thing replaces one still waiting to be delivered
            headers["Topic"] = "".join(ch for ch in tag if ch.isalnum() or ch in "-_")[:32] or "relay"
        req = urllib.request.Request(sub["endpoint"], data=encrypt(payload, sub["p256dh"], sub["auth"]),
                                     method="POST", headers=headers)
        try:
            with self.open(req) as resp:
                status = getattr(resp, "status", 201)
        except urllib.error.HTTPError as e:
            status = e.code
        except Exception:
            status = None
        with self.store.tx() as c:
            if status in (404, 410):             # the person turned it off on that device, or it's gone
                c.execute("DELETE FROM push_subs WHERE endpoint=?", (sub["endpoint"],))
            elif status and status < 300:
                c.execute("UPDATE push_subs SET last_ok=?, fails=0 WHERE endpoint=?", (time.time(), sub["endpoint"]))
            else:
                c.execute("UPDATE push_subs SET fails=fails+1 WHERE endpoint=?", (sub["endpoint"],))
                c.execute("DELETE FROM push_subs WHERE endpoint=? AND fails>=?", (sub["endpoint"], FAILS_BEFORE_FORGET))
        return status

    def _run(self) -> None:
        while True:
            item = self.q.get()
            try:
                self._send(*item)
            except Exception as e:           # one bad send never stops the next
                print(f"[relay] push failed: {e}")
            finally:
                self.q.task_done()

    def drain(self) -> None:
        """Wait until everything queued has been sent (tests, and a clean shutdown)."""
        self.q.join()
