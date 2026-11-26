"""People: email sign-in (a code by email) and the emails that may sign in.

Two ways to send the email; the first one set up wins:
  Resend   (RESEND_API_KEY, RELAY_EMAIL_FROM): the hub makes a 6-digit code, keeps only its hash for
           10 minutes, and sends it in an email with no link at all, so a mail scanner has nothing to
           open. Five wrong tries and the code is dead; one email per address per minute.
  Supabase (SUPABASE_URL, SUPABASE_PUBLISHABLE_KEY): magic links and codes, as below. With both set up,
           Supabase also sends whatever Resend refuses (e.g. before a sending domain is verified).

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
from pydantic import BaseModel, Field

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


class InviteAsk(BaseModel):
    name: str
    contact: str
    note: Optional[str] = None
    apps: list[str] = Field(default_factory=list, max_length=12)   # the AI apps they use, as the form offers them
    website: Optional[str] = None       # left empty by people; bots fill every field they see


class FriendAsk(BaseModel):
    name: str = Field(max_length=60)
    contact: str = Field(max_length=120)
    note: Optional[str] = Field(default=None, max_length=300)


APP_CHOICES = {"claude", "chatgpt", "grok", "muse", "claude-code", "codex", "gemini", "cursor", "other"}
FRIEND_INVITES = 3                       # each person may put this many friends forward; the owner still decides


ASKS_PER_HOUR, ASKS_WAITING = 3, 200     # per address, and in all


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
NOT_SET_UP = (503, "This hub's email sender isn't set up for outside addresses yet. "
                   "Ask the hub's owner to set up email sending, or sign in with a key.")
CODE_SECONDS, RESEND_GAP = 600, 60         # a hub-made code lives 10 minutes; one email a minute per address
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


def http(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple:
    """(status, body); 0 when there was no answer at all."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, OSError) as e:     # unreachable, timed out: no answer at all
        return 0, json.dumps({"error_code": "unreachable", "msg": str(e)}).encode()


class Supabase:
    """The two Supabase Auth calls the hub makes. `send` is injectable for tests."""

    def __init__(self, url: str, key: str, send: Optional[Callable] = None):
        self.url, self.key = url.rstrip("/"), key
        self.send = send or self._http

    def _http(self, method: str, url: str, headers: dict, body: Optional[bytes]):
        return http(method, url, headers, body)

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


class Resend:
    """Sends the hub's own sign-in codes through Resend (resend.com). `send` is injectable for tests."""

    def __init__(self, key: str, sender: str, send: Optional[Callable] = None):
        self.key, self.sender = key, sender
        self.send = send or self._http

    def _http(self, method: str, url: str, headers: dict, body: Optional[bytes]):
        return http(method, url, headers, body)

    def send_code(self, email: str, code: str) -> tuple:
        """(status, body) from Resend. The email carries the code and nothing to click."""
        spaced = f"{code[:3]} {code[3:]}"
        text = (f"Your Relay sign-in code is {spaced}\n\nType it on the page that asked for it. It works for 10 "
                f"minutes, once.\n\nDidn't ask for this? Ignore this email; nobody can sign in without the code.")
        page = ("<div style=\"background:#f6f4ef;padding:40px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
                "Helvetica,Arial,sans-serif;color:#18171d;text-align:center\">"
                "<div style=\"font-size:15px;color:#6b6972\">Your Relay sign-in code</div>"
                f"<div style=\"font-size:40px;font-weight:700;letter-spacing:6px;margin:14px 0 18px\">{spaced}</div>"
                "<div style=\"font-size:14px;color:#6b6972;line-height:1.5\">Type it on the page that asked for it.<br>"
                "It works for 10 minutes, once.</div>"
                "<div style=\"font-size:12px;color:#9a98a0;margin-top:28px\">Didn't ask for this? Ignore it; "
                "nobody can sign in without the code.</div></div>")
        return self.send("POST", "https://api.resend.com/emails",
                         {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                          "User-Agent": "relay-hub"},
                         json.dumps({"from": self.sender, "to": [email], "subject": f"{spaced} is your Relay code",
                                     "text": text, "html": page}).encode())


    def send_digest(self, email: str, lines: list, url: str) -> tuple:
        """Once a day at most, only when something waits: what it is, and one link to Relay."""
        import html as _html
        n = len(lines)
        subject = lines[0] if n == 1 else f"{n} things wait for you on Relay"
        text = "Waiting for you on Relay:\n\n" + "\n".join("- " + l for l in lines) + (f"\n\nOpen Relay: {url}" if url else "") + \
            "\n\nYou get this because notifications are off on your devices. Turn it off in Relay: Me, Notifications."
        items = "".join(f"<li style=\"margin:6px 0\">{_html.escape(l)}</li>" for l in lines)
        button = (f"<a href=\"{_html.escape(url)}\" style=\"display:inline-block;background:#4f44e0;color:#fff;text-decoration:none;"
                  f"padding:12px 22px;border-radius:999px;font-weight:600\">Open Relay</a>") if url else ""
        page = ("<div style=\"background:#f6f4ef;padding:36px 16px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',"
                "Helvetica,Arial,sans-serif;color:#18171d\"><div style=\"max-width:460px;margin:0 auto\">"
                "<div style=\"font-size:20px;font-weight:700;margin-bottom:10px\">Waiting for you on Relay</div>"
                f"<ul style=\"padding-left:20px;font-size:15px;line-height:1.45;margin:0 0 22px\">{items}</ul>{button}"
                "<div style=\"font-size:12px;color:#6b6972;margin-top:28px;line-height:1.5\">You get this at most once a day, only when "
                "something waits, because notifications are off on your devices. Turn it off in Relay: Me, Notifications.</div>"
                "</div></div>")
        return self.send("POST", "https://api.resend.com/emails",
                         {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json", "User-Agent": "relay-hub"},
                         json.dumps({"from": self.sender, "to": [email], "subject": subject, "text": text, "html": page}).encode())


def resend_refusal(status: int, body: bytes) -> tuple:
    """(status for the page, message) for a Resend error."""
    try:
        d = json.loads(body or b"{}")
    except ValueError:
        d = {}
    msg = str(d.get("message") or "") if isinstance(d, dict) else ""
    if status == 429:
        return 429, "Too many emails right now. Try again in a few minutes."
    if status == 403 or "verify a domain" in msg or "testing emails" in msg:
        return NOT_SET_UP
    return SEND_FAILED



class HandoffIn(BaseModel):
    code: str = Field(max_length=100)

def mount(app, hub, supabase: Optional[Supabase], resend: Optional[Resend] = None) -> None:
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
        """What the sign-in page offers. `email`: a code can be emailed. `claimed`: the hub has an owner, so a stranger is told it's
        invite-only rather than asked for an admin key they don't have."""
        with store.ro() as c:
            claimed = bool(c.execute("SELECT 1 FROM agents WHERE owner=1 LIMIT 1").fetchone())
        return {"email": bool(resend or supabase), "claimed": claimed, "home": getattr(hub, "public_url", None)}

    # ------------------------------------------------------------ moving to a new address
    # A passkey belongs to the address it was made on, and a browser keeps a sign-in per address. When the hub
    # moves, the app on the old address asks for a one-time code and opens the new address with it; the new
    # address trades it for a sign-in there, and offers a passkey for itself. Codes work once, for 2 minutes.
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS handoffs(code_hash TEXT PRIMARY KEY, handle TEXT NOT NULL, expires REAL NOT NULL)")

    @app.post("/auth/handoff")
    def handoff(me: dict = Depends(auth)):
        if me["role"] != "human":
            raise HTTPException(403, "Only a person signs in on the new address this way.")
        code = "ho_" + secrets.token_urlsafe(24)
        with store.tx() as c:
            c.execute("DELETE FROM handoffs WHERE expires<?", (hub.now(),))
            c.execute("INSERT INTO handoffs(code_hash,handle,expires) VALUES (?,?,?)", (hub.hash(code), me["handle"], hub.now() + 120))
        return {"code": code, "home": getattr(hub, "public_url", None)}

    @app.post("/auth/handoff/claim")
    def handoff_claim(body: HandoffIn):
        with store.tx() as c:
            r = c.execute("SELECT handle FROM handoffs WHERE code_hash=? AND expires>?", (hub.hash(body.code), hub.now())).fetchone()
            c.execute("DELETE FROM handoffs WHERE code_hash=?", (hub.hash(body.code),))
        if not r:
            raise HTTPException(400, "That link has expired. Open Relay on the old address and try again.")
        return hub.new_session(r["handle"])

    @app.post("/auth/email")
    def auth_email(body: EmailIn, request: Request):
        """Send a sign-in link. The answer is the same whether or not the email is on this hub,
        so the page can't be used to find out who is."""
        if not (resend or supabase):
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        email = norm_email(body.email)
        with store.ro() as c:
            known = c.execute("SELECT 1 FROM people_emails WHERE email=?", (email,)).fetchone()
        if known and not (resend and send_code(email, fallback=supabase is not None)):   # Supabase sends
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
        if not (resend or supabase):
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        bad = HTTPException(401, "That code didn't work. Check it, or ask for a new email.")
        if resend and not body.token_hash:
            email = norm_email(body.email or "")
            with store.ro() as c:
                ours = c.execute("SELECT 1 FROM email_codes WHERE email=?", (email,)).fetchone()
            if ours or supabase is None:          # a code the hub sent; otherwise Supabase sent it (the fallback)
                return start_session(check_code(email, body.code or "", bad))
        if supabase is None:
            raise HTTPException(400, "Type the code from the email.")
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

    # ------------------------------------------------------------ the hub's own codes (Resend)
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS email_codes(email TEXT PRIMARY KEY, code_hash TEXT NOT NULL,"
                  " sent REAL NOT NULL, expires REAL NOT NULL, tries INTEGER NOT NULL DEFAULT 0)")

    def code_hash(email: str, code: str) -> str:
        return hub.hash(f"email-code:{email}:{code}")

    def send_code(email: str, fallback: bool = False) -> bool:
        """Email a hub-made code. False when Resend refused and Supabase should send instead."""
        now = hub.now()
        with store.tx() as c:
            r = c.execute("SELECT sent FROM email_codes WHERE email=?", (email,)).fetchone()
            if r and now - r["sent"] < RESEND_GAP:
                raise HTTPException(429, "An email is already on its way. Wait a minute before asking for another.")
            code = f"{secrets.randbelow(10 ** 6):06d}"
            c.execute("INSERT OR REPLACE INTO email_codes(email,code_hash,sent,expires,tries) VALUES (?,?,?,?,0)",
                      (email, code_hash(email, code), now, now + CODE_SECONDS))
        status, raw = resend.send_code(email, code)
        if not 200 <= status < 300:
            with store.tx() as c:          # nothing was sent: no code to wait for, and no minute to wait
                c.execute("DELETE FROM email_codes WHERE email=?", (email,))
            log(f"Resend refused to send a sign-in code to {mask_email(email)}: status={status} "
                f"body={raw[:200].decode('utf-8', 'replace') if isinstance(raw, bytes) else str(raw)[:200]}"
                + ("; Supabase sends it instead" if fallback else ""))
            if fallback:
                return False
            raise HTTPException(*resend_refusal(status, raw))
        return True

    def check_code(email: str, typed: str, bad: HTTPException) -> dict:
        code = re.sub(r"\s+", "", typed)
        if not CODE_RE.match(code):
            raise HTTPException(400, "Type the code from the email.")
        now, refuse = hub.now(), None
        with store.tx() as c:            # decide inside, raise after: a wrong try must be counted, not rolled back
            r = c.execute("SELECT * FROM email_codes WHERE email=?", (email,)).fetchone()
            if not r:
                refuse = bad
            elif r["expires"] <= now:
                refuse = HTTPException(401, "That code has expired. Ask for a new email.")
            elif r["tries"] >= CODE_TRIES:
                refuse = HTTPException(429, "Too many wrong codes. Ask for a new email.")
            elif not secrets.compare_digest(r["code_hash"], code_hash(email, code)):
                c.execute("UPDATE email_codes SET tries=tries+1 WHERE email=?", (email,))
                refuse = bad
            else:
                c.execute("DELETE FROM email_codes WHERE email=?", (email,))   # a code works once
        if refuse:
            raise refuse
        return {"email": email, "email_confirmed_at": now}

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

    # ------------------------------------------------------------ asking for an invite (strangers)
    with store.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS invite_requests(id TEXT PRIMARY KEY, name TEXT NOT NULL, contact TEXT NOT NULL,"
                  " note TEXT, origin TEXT, created REAL NOT NULL, status TEXT NOT NULL DEFAULT 'waiting', handled REAL)")
        cols = {r["name"] for r in c.execute("PRAGMA table_info(invite_requests)")}
        for col in ("apps TEXT", "by TEXT"):       # which AI apps they use; the person who put them forward
            if col.split()[0] not in cols:
                c.execute(f"ALTER TABLE invite_requests ADD COLUMN {col}")

    @app.post("/invite-requests")
    def ask_for_invite(body: InviteAsk, request: Request):
        """No sign-in: a stranger leaves a name and a way to reach them; the hub's owner decides."""
        name, contact, note = body.name.strip()[:60], body.contact.strip()[:120], (body.note or "").strip()[:300] or None
        if body.website:                   # a bot: say thanks, keep nothing
            return {"asked": True}
        if not name or len(contact) < 3:
            raise HTTPException(400, "Add your name and how to reach you.")
        origin = hub.hash("ask:" + hub.client_ip(request))
        now = hub.now()
        with store.tx() as c:
            if c.execute("SELECT COUNT(*) n FROM invite_requests WHERE origin=? AND created>?", (origin, now - 3600)).fetchone()["n"] >= ASKS_PER_HOUR:
                raise HTTPException(429, "You've asked already. The owner will get back to you.")
            if c.execute("SELECT COUNT(*) n FROM invite_requests WHERE status='waiting'").fetchone()["n"] >= ASKS_WAITING:
                raise HTTPException(429, "Lots of people asked today. Try again tomorrow.")
            apps = sorted({a for a in body.apps if a in APP_CHOICES})
            rid = "ir_" + secrets.token_hex(6)
            c.execute("INSERT INTO invite_requests(id,name,contact,note,origin,created,apps) VALUES (?,?,?,?,?,?,?)",
                      (rid, name, contact, note, origin, now, json.dumps(apps)))
        hub.tell_owner("invites", f"{name} would like an invite", "Tap to review.", f"/app#ask/{rid}", rid)
        return {"asked": True}

    @app.get("/invite-requests/friends")
    def friends(me: dict = Depends(auth)):
        """How many friends this person may still put forward, and what happened to the ones they did."""
        if me["role"] != "human":
            raise HTTPException(403, "Only people invite friends.")
        with store.ro() as c:
            rows = c.execute("SELECT name, status FROM invite_requests WHERE by=? ORDER BY created", (me["handle"],)).fetchall()
        return {"left": max(0, FRIEND_INVITES - len(rows)), "asked": [{"name": r["name"], "status": r["status"]} for r in rows]}

    @app.post("/invite-requests/friend")
    def invite_friend(body: FriendAsk, me: dict = Depends(auth)):
        """A person puts a friend forward. It lands with the owner like any ask, saying who it's from."""
        if me["role"] != "human":
            raise HTTPException(403, "Only people invite friends.")
        name, contact = body.name.strip(), body.contact.strip()
        if not name or len(contact) < 3:
            raise HTTPException(400, "Add their name and how to reach them.")
        with store.tx() as c:
            if c.execute("SELECT COUNT(*) n FROM invite_requests WHERE by=?", (me["handle"],)).fetchone()["n"] >= FRIEND_INVITES:
                raise HTTPException(429, f"You've put {FRIEND_INVITES} friends forward already.")
            rid = "ir_" + secrets.token_hex(6)
            c.execute("INSERT INTO invite_requests(id,name,contact,note,origin,created,apps,by) VALUES (?,?,?,?,?,?,?,?)",
                      (rid, name, contact, (body.note or "").strip() or None,
                       hub.hash("friend:" + me["handle"]), hub.now(), "[]", me["handle"]))
        hub.tell_owner("invites", f"{me['handle']} put {name} forward", "Tap to review.", f"/app#ask/{rid}", rid)
        return {"asked": True}

    @app.get("/invite-requests")
    def invite_requests(me: dict = Depends(auth)):
        need_owner(me)
        with store.ro() as c:
            return [{"id": r["id"], "name": r["name"], "contact": r["contact"], "note": r["note"], "created": r["created"],
                     "apps": json.loads(r["apps"] or "[]"), "by": r["by"]}
                    for r in c.execute("SELECT * FROM invite_requests WHERE status='waiting' ORDER BY created DESC LIMIT 100")]

    @app.get("/stats")
    def stats(me: dict = Depends(auth)):
        """How it's going, for the hub's owner: counted on this hub, never sent anywhere."""
        need_owner(me)
        week = hub.now() - 7 * 86400
        with store.ro() as c:
            one = lambda q, *a: c.execute(q, a).fetchone()[0]
            asked = [json.loads(r["apps"] or "[]") for r in c.execute("SELECT apps FROM invite_requests WHERE by IS NULL")]
            wanted = {}
            for apps in asked:
                for a in apps:
                    wanted[a] = wanted.get(a, 0) + 1
            return {
                "asked": len(asked), "waiting": one("SELECT COUNT(*) FROM invite_requests WHERE status='waiting'"),
                "friends_put_forward": one("SELECT COUNT(*) FROM invite_requests WHERE by IS NOT NULL"),
                "invited": one("SELECT COUNT(*) FROM invites WHERE role='human' AND revoked IS NULL"),
                "people": one("SELECT COUNT(*) FROM agents WHERE role='human' AND owner=0"),
                "apps_connected": one("SELECT COUNT(*) FROM agents WHERE role='agent' AND last_seen IS NOT NULL"),
                "agent_conversations": one(
                    "SELECT COUNT(*) FROM (SELECT m.thread FROM messages m JOIN agents a ON a.handle=m.sender AND a.role='agent'"
                    " GROUP BY m.thread HAVING COUNT(DISTINCT m.sender) >= 2)"),
                "active_week": one("SELECT COUNT(*) FROM agents WHERE last_seen>?", week),
                "wanted": sorted(wanted.items(), key=lambda x: -x[1])}

    def handle_ask(rid: str, me: dict, status: str) -> dict:
        need_owner(me)
        with store.tx() as c:
            if not c.execute("UPDATE invite_requests SET status=?, handled=? WHERE id=? AND status='waiting'",
                             (status, hub.now(), rid)).rowcount:
                raise HTTPException(404, "no such request")
        return {"id": rid, "status": status}

    @app.post("/invite-requests/{rid}/invited")
    def ask_invited(rid: str, me: dict = Depends(auth)):
        return handle_ask(rid, me, "invited")

    @app.delete("/invite-requests/{rid}")
    def ask_dismissed(rid: str, me: dict = Depends(auth)):
        return handle_ask(rid, me, "dismissed")

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
