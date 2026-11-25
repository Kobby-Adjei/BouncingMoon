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
COOLDOWN seconds, plus one more after the cooldown if anything arrived during it. A fire that fails
for a passing reason (the network, a 5xx, a rate limit) is tried again later (RETRY_AFTER); one that
can't work until someone fixes it (a wrong token, a paused task) is not. A Claude routine is fired
at most ROUTINE_PER_HOUR times an hour, under Claude's own limit; past that, the next fire waits
for room. Secrets are encrypted at rest and never returned.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
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
READ_WINDOW = 180.0      # how long after a fire the agent has to read its messages before we say it didn't
RETRY_AFTER = (30.0, 120.0, 600.0)   # a fire that failed for a passing reason is tried again after these
ROUTINE_PER_HOUR = int(os.environ.get("RELAY_ROUTINE_PER_HOUR", 25))   # under Claude's limit on routine runs

SCHEMA = """
CREATE TABLE IF NOT EXISTS wakes(
  handle TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
  created REAL, last_fired REAL, last_status TEXT, fires INTEGER NOT NULL DEFAULT 0);
"""


class WakeError(ValueError):
    pass


# ------------------------------------------------------------ what a wake-up's state means
# A fire that got through only proves the agent was started. Whether it then reached Relay shows in
# `agents.last_read`, stamped when the agent reads its inbox (check_replies, GET /inbox). The handle may
# be shared by other clients of the same agent, so a read is good evidence, not proof.
def wake_state(last_fired: Optional[float], last_status: Optional[str], last_read: Optional[float],
               now: float, window: float = READ_WINDOW) -> str:
    """ready: never fired · failed: the last fire was refused · ok: read since the last fire ·
    waiting: fired less than `window` ago, no read yet · no_read: fired longer ago, no read since."""
    if not last_fired:
        return "ready"
    if last_status and not last_status.startswith("ok"):
        return "failed"
    if last_read and last_read >= last_fired:
        return "ok"
    return "waiting" if now - last_fired < window else "no_read"


def _code(last_status: Optional[str]) -> Optional[int]:
    m = (last_status or "").split()
    return int(m[1]) if len(m) > 1 and m[0] == "failed" and m[1].isdigit() else None


def wake_problem(kind: str, state: str, last_status: Optional[str]) -> Optional[dict]:
    """Plain words and the exact fix, never a bare code. `fix` tells the screen what to offer:
    form (paste the link and token again) · claude (open the task in Claude) · test (try again)."""
    routine = kind == "claude_routine"
    if state == "no_read":
        if routine:
            return {"code": "no_read", "fix": "claude", "text": "Claude woke up but couldn't open Relay. In the task, "
                    "turn on Relay under Connectors, save, then press Test again."}
        return {"code": "no_read", "fix": "test",
                "text": "It woke up, but nothing read its Relay messages afterwards."}
    if state != "failed":
        return None
    code, st = _code(last_status), last_status or ""
    if routine:
        if code in (401, 403):
            return {"code": "token", "fix": "form", "text": "That token doesn't match this task. In Claude, "
                    "generate a new token and paste the link and token again."}
        if code == 404:
            return {"code": "not_found", "fix": "form",
                    "text": "Claude can't find that task. Copy the link again from the task's API trigger."}
        if code == 429:
            return {"code": "rate_limited", "fix": "test",
                    "text": "Claude is limiting how often this task runs. It'll work again within the hour."}
        if code == 400 and st.endswith("paused"):
            return {"code": "paused", "fix": "claude", "text": "This task is paused in Claude. Turn it back on."}
        if code == 400:
            return {"code": "rejected", "fix": "form", "text": "Claude didn't accept the wake-up. "
                    "Copy the link and token again from the task's API trigger."}
        if code and code >= 500:
            return {"code": "server", "fix": "test", "text": "Claude had a problem on its side. Try again in a minute."}
        return {"code": "unreachable", "fix": "test", "text": "Relay couldn't reach Claude. Try again in a minute."}
    if code in (401, 403):
        return {"code": "refused", "fix": "test",
                "text": "Your server turned the wake-up away. Check that it accepts Relay's signature."}
    if code == 404:
        return {"code": "not_found", "fix": "test", "text": "Nothing answers at that address. Check the URL."}
    if code == 429:
        return {"code": "rate_limited", "fix": "test", "text": "Your server is limiting requests. It'll work again soon."}
    if code:
        return {"code": "server", "fix": "test", "text": "Your server answered with an error."}
    return {"code": "unreachable", "fix": "test", "text": "Relay couldn't reach that address."}


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


def _says_paused(e) -> bool:
    """Only a yes/no is kept from the error body; the body itself is never stored or shown."""
    try:
        body = (e.read(2048) or b"").decode("utf-8", "replace").lower()
    except Exception:
        return False
    return "paus" in body or "disabled" in body


class Waker:
    def rekey(self, old) -> int:
        """Re-lock stored wake-up secrets that an older key locked (the admin token, before the hub had
        its own secret). Safe to run on every start: rows the current key opens are left alone."""
        moved = 0
        with self.store.tx() as c:
            for r in c.execute("SELECT handle, secret FROM wakes").fetchall():
                try:
                    self.fernet.decrypt(r["secret"].encode())
                    continue
                except Exception:
                    pass
                try:
                    plain = old.decrypt(r["secret"].encode())
                except Exception:
                    continue                      # neither opens it: leave it, the owner sets it up again
                c.execute("UPDATE wakes SET secret=? WHERE handle=?", (self.fernet.encrypt(plain).decode(), r["handle"]))
                moved += 1
        return moved

    def __init__(self, store, admin_token: str, secret_key: Optional[str] = None,
                 opener: Optional[Callable] = None, check_url: bool = True, cooldown: float = COOLDOWN):
        self.store = store
        self.fernet = _cipher(admin_token, secret_key)
        self.open = opener or (lambda req: urllib.request.urlopen(req, timeout=TIMEOUT))
        self.check_url = check_url
        self.cooldown = cooldown
        self.due: dict[str, float] = {}          # handle -> when to fire
        self.notes: dict[str, set] = {}          # handle -> who the new messages are from
        self.tries: dict[str, int] = {}          # handle -> failed fires in a row
        self.recent: dict[str, list] = {}        # handle -> when it was fired in the last hour
        self.retry_after = RETRY_AFTER
        self.per_hour = ROUTINE_PER_HOUR
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
                      " secret=excluded.secret, last_status=NULL, last_fired=NULL",
                      (handle, kind, url, self.fernet.encrypt(secret.encode()).decode(), time.time()))
        out = self.get(handle)
        if shown:
            out["signing_secret"] = shown
        return out

    def get(self, handle: str) -> Optional[dict]:
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM wakes WHERE handle=?", (handle,)).fetchone()
            a = c.execute("SELECT last_read FROM agents WHERE handle=?", (handle,)).fetchone() if r else None
        if not r:
            return None
        last_read = a["last_read"] if a else None
        u = urllib.parse.urlparse(r["url"])
        shown_url = r["url"] if r["kind"] == "webhook" else f"{u.scheme}://{u.netloc}{u.path[:60]}…/fire"
        state = wake_state(r["last_fired"], r["last_status"], last_read, time.time())
        return {"handle": handle, "kind": r["kind"], "url": shown_url, "last_fired": r["last_fired"],
                "last_status": r["last_status"], "fires": r["fires"], "last_read": last_read,
                "state": state, "read_window": READ_WINDOW,
                "problem": wake_problem(r["kind"], state, r["last_status"])}

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
                        r = c.execute("SELECT kind, last_fired FROM wakes WHERE handle=?", (h,)).fetchone()
                    if not r:
                        self.tries.pop(h, None)
                        continue                       # no wake-up configured: nothing to do
                    if r["last_fired"] and now - r["last_fired"] < self.cooldown:
                        self._later(h, senders, r["last_fired"] + self.cooldown)   # one fire after the cooldown
                        continue
                    hour = [t for t in self.recent.get(h, []) if now - t < 3600]
                    if r["kind"] == "claude_routine" and len(hour) >= self.per_hour:
                        self._later(h, senders, hour[0] + 3600)                    # wait for room under the limit
                        continue
                    status = self._fire(h, senders)
                    if status.startswith("ok"):
                        self.tries.pop(h, None)
                    elif self._passing(status):        # try again later, with whoever wrote meanwhile
                        n = self.tries.get(h, 0)
                        if n < len(self.retry_after):
                            self.tries[h] = n + 1
                            self._later(h, senders, time.time() + self.retry_after[n])
                        else:
                            self.tries.pop(h, None)
                    else:
                        self.tries.pop(h, None)        # needs the owner to fix it: the screen says how
                except Exception:
                    pass                               # a wake-up must never break messaging

    def _later(self, h: str, senders: set, when: float) -> None:
        with self.lock:
            self.notes.setdefault(h, set()).update(senders)
            self.due[h] = min(self.due.get(h, when), when)

    @staticmethod
    def _passing(status: str) -> bool:
        """A failure that may clear by itself: no answer, Claude's or the server's side, a rate limit."""
        code = _code(status)
        return code is None or code == 429 or code >= 500

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
        started = time.time()                 # before the request: a quick agent may read before it returns
        self.recent[handle] = [t for t in self.recent.get(handle, []) if started - t < 3600] + [started]
        try:
            with self.open(req) as resp:
                status = f"ok {getattr(resp, 'status', 200)}"
        except urllib.error.HTTPError as e:
            status = f"failed {e.code}"
            if e.code == 400 and r["kind"] == "claude_routine" and _says_paused(e):
                status += " paused"
        except Exception as e:
            status = f"failed: {type(e).__name__}"
        with self.store.tx() as c:
            c.execute("UPDATE wakes SET last_fired=?, last_status=?, fires=fires+1 WHERE handle=?",
                      (started, status, handle))
        return status
