"""Wake-ups: tell an agent the moment a message arrives for it.

An agent that is not running cannot hear anything, so the hub has to start it. Each agent can
have one wake-up, set by the admin or by the agent itself:

  claude_routine  POST to a Claude Code routine's /fire URL with its token. Claude starts in
                  Anthropic's cloud, reads its Relay messages through the connector and replies.
  webhook         POST a small signed JSON notice to any https URL the agent's owner runs.

Agents on their own always-on machine don't need either: `cli.py listen` holds a long-poll to
the hub and runs a command the moment something arrives.

Rules: a wake-up says only *that* there is something new, never the message itself (the agent
reads messages through its own key). Bursts are coalesced: at most one wake per agent every
COOLDOWN seconds, plus one more after the cooldown if anything arrived during it. Secrets are
encrypted at rest and never returned.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

ROUTINE_PREFIX = "https://api.anthropic.com/v1/claude_code/routines/"
ROUTINE_BETA = "experimental-cc-routine-2026-04-01"
COOLDOWN = 20.0
TIMEOUT = 15.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS wakes(
  handle TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
  created REAL, last_fired REAL, last_status TEXT, fires INTEGER NOT NULL DEFAULT 0);
"""


class WakeError(ValueError):
    pass


def _cipher(admin_token: str, override: Optional[str]):
    from cryptography.fernet import Fernet
    raw = override.encode() if override else hashlib.sha256(b"relay-wake:" + admin_token.encode()).digest()
    key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    return Fernet(key)


def _public_https(url: str) -> str:
    u = urllib.parse.urlparse(url.strip())
    if u.scheme != "https" or not u.hostname:
        raise WakeError("the wake-up URL must start with https://")
    host = u.hostname.lower()
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "." not in host:
        raise WakeError("the wake-up URL must be a public address")
    try:
        infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
    except OSError:
        raise WakeError(f"can't resolve {host}")
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise WakeError("the wake-up URL must be a public address")
    return url.strip()


class Waker:
    def __init__(self, store, admin_token: str, secret_key: Optional[str] = None,
                 opener: Optional[Callable] = None, check_url: bool = True, cooldown: float = COOLDOWN):
        self.store = store
        self.fernet = _cipher(admin_token, secret_key)
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))
        self.check_url = check_url
        self.cooldown = cooldown
        self.due: dict[str, float] = {}          # handle -> when to fire
        self.notes: dict[str, set] = {}          # handle -> who the new messages are from
        self.lock = threading.Lock()
        self.kick = threading.Event()
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
        threading.Thread(target=self._run, name="relay-waker", daemon=True).start()

    # ------------------------------------------------------------ config
    def configure(self, handle: str, kind: str, url: str, token: Optional[str]) -> dict:
        """Returns the stored config; for a webhook also the signing secret (shown once)."""
        url = (url or "").strip()
        if kind == "claude_routine":
            if not url.startswith(ROUTINE_PREFIX) or not url.endswith("/fire"):
                raise WakeError(f"a routine URL looks like {ROUTINE_PREFIX}trig_…/fire")
            if not token or len(token) < 20:
                raise WakeError("paste the routine's API token as well")
            secret, shown = token.strip(), None
        elif kind == "webhook":
            if self.check_url:
                url = _public_https(url)
            secret = token.strip() if token else "whsec_" + secrets.token_urlsafe(24)
            shown = None if token else secret
        else:
            raise WakeError("kind must be claude_routine or webhook")
        with self.store.tx() as c:
            c.execute("INSERT INTO wakes(handle,kind,url,secret,created) VALUES (?,?,?,?,?)"
                      " ON CONFLICT(handle) DO UPDATE SET kind=excluded.kind, url=excluded.url,"
                      " secret=excluded.secret, last_status=NULL",
                      (handle, kind, url, self.fernet.encrypt(secret.encode()).decode(), time.time()))
        out = self.get(handle)
        if shown:
            out["signing_secret"] = shown
        return out

    def get(self, handle: str) -> Optional[dict]:
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM wakes WHERE handle=?", (handle,)).fetchone()
        if not r:
            return None
        u = urllib.parse.urlparse(r["url"])
        shown_url = r["url"] if r["kind"] == "webhook" else f"{u.scheme}://{u.netloc}{u.path[:60]}…/fire"
        return {"handle": handle, "kind": r["kind"], "url": shown_url, "last_fired": r["last_fired"],
                "last_status": r["last_status"], "fires": r["fires"]}

    def remove(self, handle: str) -> bool:
        with self.store.tx() as c:
            return c.execute("DELETE FROM wakes WHERE handle=?", (handle,)).rowcount > 0

    # ------------------------------------------------------------ firing
    def notify(self, handles, sender: str) -> None:
        """Called after a message is written. Cheap: only queues work."""
        now = time.time()
        with self.lock:
            for h in handles:
                if h == sender:
                    continue
                self.notes.setdefault(h, set()).add(sender)
                if h not in self.due:
                    self.due[h] = now + 0.3          # let the writing transaction commit first
        self.kick.set()

    def fire_now(self, handle: str) -> str:
        return self._fire(handle, {"test"})

    def _run(self):
        while True:
            self.kick.wait(timeout=1.0)
            self.kick.clear()
            now = time.time()
            with self.lock:
                ready = [h for h, t in self.due.items() if t <= now]
                batch = {h: self.notes.pop(h, set()) for h in ready}
                for h in ready:
                    self.due.pop(h, None)
            for h, senders in batch.items():
                try:
                    with self.store.ro() as c:
                        r = c.execute("SELECT last_fired FROM wakes WHERE handle=?", (h,)).fetchone()
                    if not r:
                        continue                       # no wake-up configured: nothing to do
                    if r["last_fired"] and now - r["last_fired"] < self.cooldown:
                        with self.lock:                # coalesce into one fire after the cooldown
                            self.notes.setdefault(h, set()).update(senders)
                            self.due[h] = r["last_fired"] + self.cooldown
                        continue
                    self._fire(h, senders)
                except Exception:
                    pass                               # a wake-up must never break messaging

    def _fire(self, handle: str, senders: set) -> str:
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM wakes WHERE handle=?", (handle,)).fetchone()
        if not r:
            return "no wake-up set"
        secret = self.fernet.decrypt(r["secret"].encode()).decode()
        who = ", ".join(sorted(s for s in senders if s)) or "someone"
        if r["kind"] == "claude_routine":
            body = json.dumps({"text": f"New Relay messages for {handle} from {who}."}).encode()
            req = urllib.request.Request(r["url"], data=body, method="POST", headers={
                "Authorization": f"Bearer {secret}", "anthropic-beta": ROUTINE_BETA,
                "anthropic-version": "2023-06-01", "Content-Type": "application/json"})
        else:
            ts = str(int(time.time()))
            body = json.dumps({"type": "relay.messages", "agent": handle, "from": sorted(senders),
                               "time": int(ts)}).encode()
            sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
            req = urllib.request.Request(r["url"], data=body, method="POST", headers={
                "Content-Type": "application/json", "X-Relay-Timestamp": ts,
                "X-Relay-Signature": f"sha256={sig}", "User-Agent": "relay-hub"})
        try:
            with self.open(req) as resp:
                status = f"ok {getattr(resp, 'status', 200)}"
        except urllib.error.HTTPError as e:
            status = f"failed {e.code}"
        except Exception as e:
            status = f"failed: {type(e).__name__}"
        with self.store.tx() as c:
            c.execute("UPDATE wakes SET last_fired=?, last_status=?, fires=fires+1 WHERE handle=?",
                      (time.time(), status, handle))
        return status
