"""People: email sign-in (Supabase magic links) and the emails that may sign in.

Agents authenticate with their own Relay keys. People don't have to: the owner attaches an
email to a person, the person types it at /app, Supabase emails them a link, and the link
brings them back signed in. The hub never sees a password and the browser never talks to
Supabase directly (the page's policy only allows the hub), so the hub relays two calls:

  POST {SUPABASE_URL}/auth/v1/otp?redirect_to=<hub>/app   send the link (only to emails on this hub)
  GET  {SUPABASE_URL}/auth/v1/user                        check the token the link handed back
  POST {SUPABASE_URL}/auth/v1/verify                      check a code (or token hash) from the email

Why a code as well as a link: work and school mail gateways open every link in an incoming
email within seconds (seen live: a headless Chrome on AWS opened each link ~13s after it was
sent). A Supabase link works once, so the scanner used it up and the person's own click
failed. A code typed into /app, or a link to /app#token_hash=… that only acts when the person
presses "Sign in", can't be used up by a scanner. That needs {{ .Token }} / {{ .TokenHash }}
in the Supabase email templates (Authentication > Emails in the Supabase dashboard).

When Supabase refuses to send, the hub logs why (address masked) and tells the page when the
person can do something about it. Emails that aren't on the hub still get {"sent": true} and
no call to Supabase, so the page can't be used to find out who is on the hub.

A good token for an email that belongs to a person here becomes a hub session
(`rly_sess_…`), stored hashed, valid for 30 days, ended by /auth/signout.
"""
from __future__ import annotations

import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$")
SESSION_DAYS = 30


class EmailIn(BaseModel):
    email: str


class SessionIn(BaseModel):
    access_token: str


class VerifyIn(BaseModel):
    email: Optional[str] = None
    code: Optional[str] = None
    token_hash: Optional[str] = None


class PersonIn(BaseModel):
    handle: str
    email: str


def mask_email(email: str) -> str:
    """k***@example.com: enough to tell addresses apart in a log, not enough to collect them."""
    local, _, domain = (email or "").partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def scrub(text: str) -> str:
    return EMAIL_ANY.sub(lambda m: mask_email(m.group(0)), text or "")


EMAIL_ANY = re.compile(r"[^@\s\"'<>(),;:]+@[^@\s\"'<>(),;:]+")
CODE_RE = re.compile(r"^[0-9]{6,10}$")
SEND_REFUSED = {
    # Supabase error_code -> (status for the page, what the person can do about it)
    "over_email_send_rate_limit": (429, "Too many emails right now. Try again in a few minutes."),
    "over_request_rate_limit": (429, "Too many emails right now. Try again in a few minutes."),
    "email_address_not_authorized": (503, "This hub's email sender isn't set up for outside addresses yet. "
                                          "Ask the hub's owner to set up email sending, or sign in with a key."),
}
SEND_FAILED = (502, "The email couldn't be sent just now. Try again in a minute, or sign in with a key.")
CODE_TRIES, CODE_WINDOW = 5, 900          # wrong codes allowed per email per 15 minutes


def supabase_error(status: int, body: bytes) -> tuple:
    """(error_code, message) from a Supabase Auth error body; tolerant of every shape it uses."""
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        d = {}
    if not isinstance(d, dict):
        d = {}
    code = d.get("error_code") or d.get("code") or d.get("error") or ""
    msg = d.get("msg") or d.get("message") or d.get("error_description") or ""
    if not isinstance(code, str):
        code = str(code)
    if status == 429 and not code.startswith("over_"):
        code = "over_request_rate_limit"           # any 429 is a rate limit, whatever the body says
    return code, str(msg)


def log(line: str) -> None:
    print("relay.people: " + scrub(line), flush=True)


def norm_email(email: str) -> str:
    e = (email or "").strip().lower()
    if not EMAIL_RE.match(e):
        raise HTTPException(400, "that doesn't look like an email address")
    return e


class Supabase:
    """The two Supabase Auth calls the hub makes. `send` is injectable for tests."""

    def __init__(self, url: str, key: str, send: Optional[Callable] = None):
        self.url, self.key = url.rstrip("/"), key
        self.send = send or self._http

    def _http(self, method: str, url: str, headers: dict, body: Optional[bytes]):
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except (urllib.error.URLError, OSError) as e:     # unreachable, timed out: no answer at all
            return 0, json.dumps({"error_code": "unreachable", "msg": str(e)}).encode()

    def send_link(self, email: str, redirect_to: str) -> tuple:
        """(status, body) from Supabase; the email carries a link and, if the template has it, a code."""
        q = urllib.parse.urlencode({"redirect_to": redirect_to})
        return self.send("POST", f"{self.url}/auth/v1/otp?{q}",
                         {"apikey": self.key, "Content-Type": "application/json"},
                         json.dumps({"email": email, "create_user": True}).encode())

    def verify(self, payload: dict) -> tuple:
        """Trade a code (email + token) or a token hash for a Supabase session: (status, body)."""
        return self.send("POST", f"{self.url}/auth/v1/verify",
                         {"apikey": self.key, "Content-Type": "application/json"},
                         json.dumps({"type": "email", **payload}).encode())

    def user(self, access_token: str) -> Optional[dict]:
        status, body = self.send("GET", f"{self.url}/auth/v1/user",
                                 {"apikey": self.key, "Authorization": f"Bearer {access_token}"}, None)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def mount(app, hub, supabase: Optional[Supabase]) -> None:
    store, auth = hub.store, hub.auth
    wrong_codes: dict = {}          # email -> times of recent wrong codes (in memory; a restart forgets)

    # One-time cleanup, Nov 18 2026: before sign-in links waited for a press of "Sign in", a mail
    # scanner that opened Kobby's links was handed two email sessions. Every email session made
    # before the fix is ended once, here; keys are untouched. A marker keeps it from running again.
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS hub_flags(name TEXT PRIMARY KEY, done REAL)")
        if not c.execute("SELECT 1 FROM hub_flags WHERE name='purge-scanner-sessions-2026-11-18'").fetchone():
            gone = c.execute("DELETE FROM sessions").rowcount      # runs once, at the first start after the fix
            c.execute("INSERT INTO hub_flags(name,done) VALUES ('purge-scanner-sessions-2026-11-18',?)", (hub.now(),))
            print(f"[relay] ended {gone} email session(s) issued before the scanner-safe sign-in", flush=True)

    def need_owner(me):
        if me["role"] != "admin" and not me.get("owner"):
            raise HTTPException(403, "only the hub's owner can do this")

    def person(c, handle: str):
        r = c.execute("SELECT role FROM agents WHERE handle=?", (handle,)).fetchone()
        if not r:
            raise HTTPException(404, f"no one called {handle}")
        if r["role"] != "human":
            raise HTTPException(409, f"{handle} is an agent; agents sign in with their own key")

    def set_email(c, handle: str, email: str) -> None:
        other = c.execute("SELECT handle FROM people_emails WHERE email=?", (email,)).fetchone()
        if other and other["handle"] != handle:
            raise HTTPException(409, "that email already belongs to someone on this hub")
        c.execute("DELETE FROM people_emails WHERE handle=?", (handle,))
        c.execute("INSERT INTO people_emails(email,handle,created) VALUES (?,?,?)", (email, handle, hub.now()))

    # ------------------------------------------------------------ sign-in
    @app.get("/auth/config")
    def auth_config():
        return {"email": supabase is not None}

    @app.post("/auth/email")
    def auth_email(body: EmailIn, request: Request):
        """Send a sign-in link. The answer is the same whether or not the email is on this hub,
        so the page can't be used to find out who is."""
        if supabase is None:
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        email = norm_email(body.email)
        with store.ro() as c:
            known = c.execute("SELECT 1 FROM people_emails WHERE email=?", (email,)).fetchone()
        if known:
            origin = f"{request.url.scheme}://{request.url.netloc}"
            status, raw = supabase.send_link(email, origin + "/app")
            if not 200 <= status < 300:
                code, msg = supabase_error(status, raw)
                log(f"Supabase refused to send a sign-in email to {mask_email(email)}: "
                    f"status={status} code={code or '-'} msg={msg[:200] or '-'}")
                raise HTTPException(*SEND_REFUSED.get(code, SEND_FAILED))
        return {"sent": True}

    @app.post("/auth/session")
    def auth_session(body: SessionIn):
        if supabase is None:
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        return start_session(supabase.user(body.access_token.strip()))

    @app.post("/auth/verify")
    def auth_verify(body: VerifyIn):
        """Sign in with the code from the email (with the email it went to), or with the token hash
        from a link that points at /app (the page only sends it when the person presses Sign in)."""
        if supabase is None:
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        bad = HTTPException(401, "That code didn't work. Check it, or ask for a new email.")
        if body.token_hash:
            payload, email = {"token_hash": body.token_hash.strip()}, None
            bad = HTTPException(401, "That sign-in link has expired or was already used. Ask for a new one.")
        else:
            email = norm_email(body.email or "")
            code = re.sub(r"\s+", "", body.code or "")
            if not CODE_RE.match(code):
                raise HTTPException(400, "Type the code from the email.")
            now = time.time()
            recent = [t for t in wrong_codes.get(email, []) if now - t < CODE_WINDOW]
            wrong_codes[email] = recent
            if len(recent) >= CODE_TRIES:
                raise HTTPException(429, "Too many wrong codes. Ask for a new email in a few minutes.")
            with store.ro() as c:
                known = c.execute("SELECT 1 FROM people_emails WHERE email=?", (email,)).fetchone()
            if not known:                   # same answer as a wrong code; Supabase isn't asked
                recent.append(now)
                raise bad
            payload = {"email": email, "token": code}
        status, raw = supabase.verify(payload)
        if not 200 <= status < 300:
            err, msg = supabase_error(status, raw)
            log(f"Supabase didn't accept a sign-in {'code' if email else 'link'}"
                f"{' for ' + mask_email(email) if email else ''}: status={status} code={err or '-'} msg={msg[:200] or '-'}")
            if email:
                wrong_codes.setdefault(email, []).append(time.time())
            if err.startswith("over_"):
                raise HTTPException(429, "Too many tries right now. Wait a few minutes and try again.")
            raise bad
        try:
            got = json.loads(raw)
        except ValueError:
            got = {}
        u = got.get("user") if isinstance(got, dict) else None
        if not u and isinstance(got, dict) and got.get("access_token"):
            u = supabase.user(got["access_token"])
        if email:
            wrong_codes.pop(email, None)
        return start_session(u)

    def start_session(u: Optional[dict]) -> dict:
        """A hub session for a confirmed Supabase user whose email is on this hub."""
        if not u or not u.get("email") or not (u.get("email_confirmed_at") or u.get("confirmed_at")):
            raise HTTPException(401, "that sign-in link didn't check out; ask for a new one")
        email = u["email"].strip().lower()
        with store.tx() as c:
            r = c.execute("SELECT handle FROM people_emails WHERE email=?", (email,)).fetchone()
            if not r:
                raise HTTPException(403, f"{email} isn't on this hub yet. Ask the owner to add it.")
            token = "rly_sess_" + secrets.token_urlsafe(32)
            now = hub.now()
            c.execute("INSERT INTO sessions(token_hash,handle,created,expires,last_used) VALUES (?,?,?,?,?)",
                      (hub.hash(token), r["handle"], now, now + SESSION_DAYS * 86400, now))
            c.execute("UPDATE agents SET last_seen=? WHERE handle=?", (now, r["handle"]))
            owner = bool(c.execute("SELECT owner FROM agents WHERE handle=?", (r["handle"],)).fetchone()["owner"])
        return {"token": token, "handle": r["handle"], "owner": owner}

    @app.post("/auth/signout")
    def auth_signout(request: Request):
        tok = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
        if tok.startswith("rly_sess_"):
            with store.tx() as c:
                c.execute("DELETE FROM sessions WHERE token_hash=?", (hub.hash(tok),))
        return {"signed_out": True}

    # ------------------------------------------------------------ people and their emails
    @app.post("/people")
    def add_person(body: PersonIn, me: dict = Depends(auth)):
        need_owner(me)
        hub.check_new_agent(body.handle, "human")
        email = norm_email(body.email)
        with store.tx() as c:
            if c.execute("SELECT 1 FROM agents WHERE handle=?", (body.handle,)).fetchone():
                raise HTTPException(409, f"{body.handle} is already taken")
            # a person signs in by email; their key is random and never shown
            c.execute("INSERT INTO agents(handle,token_hash,caps,role,created) VALUES (?,?,?,?,?)",
                      (body.handle, hub.hash("rly_" + secrets.token_urlsafe(32)), "[]", "human", hub.now()))
            c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (body.handle,))
            set_email(c, body.handle, email)
        return {"handle": body.handle, "email": email}

    @app.put("/people/{handle}/email")
    def put_email(handle: str, body: EmailIn, me: dict = Depends(auth)):
        if not (me["handle"] == handle and me["role"] == "human"):
            need_owner(me)
        email = norm_email(body.email)
        with store.tx() as c:
            person(c, handle)
            set_email(c, handle, email)
        return {"handle": handle, "email": email}

    @app.delete("/people/{handle}/email")
    def delete_email(handle: str, me: dict = Depends(auth)):
        if not (me["handle"] == handle and me["role"] == "human"):
            need_owner(me)
        with store.tx() as c:
            n = c.execute("DELETE FROM people_emails WHERE handle=?", (handle,)).rowcount
            c.execute("DELETE FROM sessions WHERE handle=?", (handle,))
        return {"removed": bool(n)}
