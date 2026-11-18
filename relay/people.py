"""People: email sign-in (Supabase magic links) and the emails that may sign in.

Agents authenticate with their own Relay keys. People don't have to: the owner attaches an
email to a person, the person types it at /app, Supabase emails them a link, and the link
brings them back signed in. The hub never sees a password and the browser never talks to
Supabase directly (the page's policy only allows the hub), so the hub relays two calls:

  POST {SUPABASE_URL}/auth/v1/otp?redirect_to=<hub>/app   send the link (only to emails on this hub)
  GET  {SUPABASE_URL}/auth/v1/user                        check the token the link handed back

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


class PersonIn(BaseModel):
    handle: str
    email: str


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

    def send_link(self, email: str, redirect_to: str) -> int:
        q = urllib.parse.urlencode({"redirect_to": redirect_to})
        status, _ = self.send("POST", f"{self.url}/auth/v1/otp?{q}",
                              {"apikey": self.key, "Content-Type": "application/json"},
                              json.dumps({"email": email, "create_user": True}).encode())
        return status

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
            supabase.send_link(email, origin + "/app")
        return {"sent": True}

    @app.post("/auth/session")
    def auth_session(body: SessionIn):
        if supabase is None:
            raise HTTPException(404, "email sign-in isn't set up on this hub")
        u = supabase.user(body.access_token.strip())
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
