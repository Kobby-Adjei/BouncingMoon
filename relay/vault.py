"""Keys: an agent uses a saved key without ever seeing it, and nothing changes until a person says OK.

A person saves a key once (an Instagram token, a YouTube key, any API key), says which website it is
for and how that website expects it (a Bearer header, another header, or a query parameter). Their
agents then call that website through the hub with `call_api`: the hub puts the key in, makes the
call, takes the key back out of whatever comes back, and returns the answer. The key is never shown
again, to the person or to any agent: not in a tool result, not in the web app, not in a log.

  stored     encrypted (AES-GCM) with a key derived from RELAY_VAULT_KEY, or the admin token when that
             isn't set; the database alone can't open it. Each key is bound to its id and its owner.
  sent       only to the exact websites its owner listed, over https, to public addresses, never
             following a redirect (a redirect could carry the key somewhere else)
  used by    the owner's own agents (all of them, or the ones the owner picked)
  asks       before anything that can change something (POST, PUT, PATCH, DELETE) by default; the owner
             can make a key ask every time, or never. The person sees exactly what will be sent and
             approves that exact request: the agent can't change it afterwards.

A call that needs an OK waits ~25 s for it, so a person watching can approve while the chat waits.
Otherwise the chat gets a link to the approval page, and the result arrives later in the same
conversation, back in the exact chat that asked (check_replies), like any answer.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9 ._-]{0,39}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
FIELD_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
CHANGES = ("POST", "PUT", "PATCH", "DELETE")
ASK = ("changes", "always", "never")
PLACES = ("bearer", "header", "query")
BLOCKED_HEADERS = {"authorization", "cookie", "host", "proxy-authorization", "content-length", "transfer-encoding",
                   "connection", "x-forwarded-for", "forwarded"}
MAX_BODY = 1 << 20                 # what an agent may send
MAX_READ = 2 << 20                 # what the hub reads back
MAX_RESULT = 20000                 # what goes back to the agent (characters)
WAIT_SECONDS = 25                  # how long call_api waits for an OK before handing over a link
APPROVAL_SECONDS = 24 * 3600       # a request nobody decided on expires
CALLS_PER_HOUR = 60                # per agent, per key


SCHEMA = """
CREATE TABLE IF NOT EXISTS vault_keys(
  id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL, hosts TEXT NOT NULL, place TEXT NOT NULL,
  field TEXT, sealed BLOB NOT NULL, hint TEXT, ask TEXT NOT NULL, agents TEXT NOT NULL DEFAULT '[]',
  created REAL NOT NULL, updated REAL, last_used REAL, UNIQUE(owner, name));
CREATE TABLE IF NOT EXISTS vault_calls(
  id TEXT PRIMARY KEY, key_id TEXT NOT NULL, owner TEXT NOT NULL, agent TEXT NOT NULL, anchor INTEGER,
  method TEXT NOT NULL, url TEXT NOT NULL, headers TEXT NOT NULL DEFAULT '{}', body BLOB, why TEXT,
  status TEXT NOT NULL, decided_by TEXT, created REAL NOT NULL, decided REAL, finished REAL,
  result_status INTEGER, result_type TEXT, result TEXT, error TEXT);
CREATE INDEX IF NOT EXISTS vault_calls_owner ON vault_calls(owner, status, created);
CREATE INDEX IF NOT EXISTS vault_calls_key ON vault_calls(key_id, created);
"""


class VaultError(Exception):
    """Something the agent or person can fix; the text says how."""


class KeyIn(BaseModel):
    name: str
    hosts: list[str]
    secret: str
    place: str = "bearer"
    field: Optional[str] = None       # the header name, or the query parameter name
    ask: str = "changes"
    agents: list[str] = Field(default_factory=list)     # empty: all of the owner's agents


class KeyEdit(BaseModel):
    secret: Optional[str] = None
    hosts: Optional[list[str]] = None
    ask: Optional[str] = None
    agents: Optional[list[str]] = None


def _public(host: str, resolve: Callable) -> bool:
    """Every address the name resolves to is a public one (not this machine, not a private network)."""
    try:
        infos = resolve(host)
    except OSError:
        return False
    addrs = {i[4][0] for i in infos}
    return bool(addrs) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addrs)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):          # the answer is the 3xx itself; the key goes nowhere else
        return None


def _http(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple:
    """(status, headers, body bytes) for one request; no redirects, 30 s timeout, at most MAX_READ."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read(MAX_READ)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers or {}), e.read(MAX_READ)
    except (urllib.error.URLError, OSError) as e:
        raise VaultError(f"The call didn't reach the website: {getattr(e, 'reason', e)}") from None


class Vault:
    def __init__(self, store, now, secret: str, send: Optional[Callable] = None,
                 resolve: Optional[Callable] = None):
        self.store, self.now = store, now
        self.on_waiting = None        # set by the hub: who to tell when a call needs an OK
        self.aead = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=b"relay-vault",
                                info=b"keys v1").derive(secret.encode()))
        self.send = send or _http
        self.watching: set = set()       # requests a call_api is waiting on right now: it delivers their result itself
        self.resolve = resolve or (lambda host: socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP))
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)

    def rekey(self, old_secret: str) -> int:
        """Re-seal saved keys an older secret sealed (the admin token, before the hub had its own).
        Safe on every start: keys the current secret opens are left alone."""
        old = Vault.__new__(Vault)
        old.aead = AESGCM(HKDF(algorithm=hashes.SHA256(), length=32, salt=b"relay-vault", info=b"keys v1").derive(old_secret.encode()))
        moved = 0
        with self.store.tx() as c:
            for r in c.execute("SELECT * FROM vault_keys").fetchall():
                try:
                    self._open(r)
                    continue
                except VaultError:
                    pass
                try:
                    secret = old._open(r)
                except VaultError:
                    continue
                c.execute("UPDATE vault_keys SET sealed=? WHERE id=?", (self._seal(r["id"], r["owner"], secret), r["id"]))
                moved += 1
        return moved

    # ------------------------------------------------------------ sealing
    def _seal(self, kid: str, owner: str, secret: str) -> bytes:
        nonce = secrets.token_bytes(12)
        return nonce + self.aead.encrypt(nonce, secret.encode(), f"{kid}|{owner}".encode())

    def _open(self, row) -> str:
        sealed = bytes(row["sealed"])
        try:
            return self.aead.decrypt(sealed[:12], sealed[12:], f"{row['id']}|{row['owner']}".encode()).decode()
        except Exception:
            raise VaultError(f"The hub can't open the key '{row['name']}' any more (its vault key changed). "
                             f"Ask {row['owner']} to save it again.") from None

    # ------------------------------------------------------------ keys (people)
    @staticmethod
    def key_out(r) -> dict:
        return {"id": r["id"], "name": r["name"], "hosts": json.loads(r["hosts"]), "place": r["place"],
                "field": r["field"], "hint": r["hint"], "ask": r["ask"], "agents": json.loads(r["agents"]),
                "created": r["created"], "updated": r["updated"], "last_used": r["last_used"]}

    @staticmethod
    def clean_hosts(hosts: list) -> list:
        out = []
        for h in hosts or []:
            h = str(h).strip().lower()
            if "://" in h:
                h = urllib.parse.urlsplit(h).hostname or ""
            h = h.strip("/").split("/")[0]
            if not HOST_RE.match(h):
                raise VaultError(f"'{h or '(empty)'}' isn't a website address. Type it like graph.facebook.com.")
            if h not in out:
                out.append(h)
        if not out or len(out) > 5:
            raise VaultError("Give the website this key is for (up to 5).")
        return out

    def mine(self, owner: str) -> list:
        with self.store.ro() as c:
            return [self.key_out(r) for r in c.execute("SELECT * FROM vault_keys WHERE owner=? ORDER BY name", (owner,))]

    def add(self, owner: str, k: KeyIn) -> dict:
        name = k.name.strip().lower()
        if not NAME_RE.match(name):
            raise VaultError("Name it with letters and digits, like instagram or youtube.")
        if k.place not in PLACES or k.ask not in ASK:
            raise VaultError("place is bearer, header or query; ask is changes, always or never.")
        field = (k.field or "").strip() or None
        if k.place != "bearer" and not (field and FIELD_RE.match(field)):
            raise VaultError("Say which header or query parameter the website reads the key from.")
        if k.place == "header" and field.lower() in BLOCKED_HEADERS - {"authorization"}:
            raise VaultError(f"The key can't go in the {field} header.")
        secret = k.secret.strip()
        if not 4 <= len(secret) <= 8192 or any(ch in secret for ch in "\r\n"):
            raise VaultError("Paste the key itself (one line).")
        hosts = self.clean_hosts(k.hosts)
        kid = "k_" + secrets.token_hex(6)
        now = self.now()
        with self.store.tx() as c:
            if c.execute("SELECT 1 FROM vault_keys WHERE owner=? AND name=?", (owner, name)).fetchone():
                raise VaultError(f"You already have a key called {name}. Replace it there, or pick another name.")
            c.execute("INSERT INTO vault_keys(id,owner,name,hosts,place,field,sealed,hint,ask,agents,created)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (kid, owner, name, json.dumps(hosts), k.place, field if k.place != "bearer" else None,
                       self._seal(kid, owner, secret), secret[-4:] if len(secret) >= 12 else None, k.ask,
                       json.dumps(sorted(set(k.agents))), now))
            return self.key_out(c.execute("SELECT * FROM vault_keys WHERE id=?", (kid,)).fetchone())

    def _own(self, c, owner: str, kid: str):
        r = c.execute("SELECT * FROM vault_keys WHERE id=? AND owner=?", (kid, owner)).fetchone()
        if not r:
            raise HTTPException(404, "no such key")
        return r

    def edit(self, owner: str, kid: str, e: KeyEdit) -> dict:
        with self.store.tx() as c:
            r = self._own(c, owner, kid)
            if e.ask is not None:
                if e.ask not in ASK:
                    raise VaultError("ask is changes, always or never.")
                c.execute("UPDATE vault_keys SET ask=? WHERE id=?", (e.ask, kid))
            if e.hosts is not None:
                c.execute("UPDATE vault_keys SET hosts=? WHERE id=?", (json.dumps(self.clean_hosts(e.hosts)), kid))
            if e.agents is not None:
                c.execute("UPDATE vault_keys SET agents=? WHERE id=?", (json.dumps(sorted(set(e.agents))), kid))
            if e.secret is not None:
                s = e.secret.strip()
                if not 4 <= len(s) <= 8192 or any(ch in s for ch in "\r\n"):
                    raise VaultError("Paste the key itself (one line).")
                c.execute("UPDATE vault_keys SET sealed=?, hint=? WHERE id=?",
                          (self._seal(kid, owner, s), s[-4:] if len(s) >= 12 else None, kid))
            c.execute("UPDATE vault_keys SET updated=? WHERE id=?", (self.now(), kid))
            return self.key_out(c.execute("SELECT * FROM vault_keys WHERE id=?", (r["id"],)).fetchone())

    def remove(self, owner: str, kid: str) -> None:
        with self.store.tx() as c:
            self._own(c, owner, kid)
            c.execute("DELETE FROM vault_keys WHERE id=?", (kid,))
            c.execute("UPDATE vault_calls SET status='expired', decided=? WHERE key_id=? AND status='waiting'",
                      (self.now(), kid))

    # ------------------------------------------------------------ which keys an agent may use
    def person_of(self, c, agent: str) -> Optional[str]:
        """The person an agent works for: the one who connected it, or the hub's owner for the hub's own."""
        a = c.execute("SELECT person, role FROM agents WHERE handle=?", (agent,)).fetchone()
        if not a or a["role"] != "agent":
            return None
        if a["person"]:
            return a["person"]
        o = c.execute("SELECT handle FROM agents WHERE owner=1 ORDER BY created LIMIT 1").fetchone()
        return o["handle"] if o else None

    def usable(self, agent: str) -> list:
        with self.store.ro() as c:
            person = self.person_of(c, agent)
            if not person:
                return []
            rows = c.execute("SELECT * FROM vault_keys WHERE owner=? ORDER BY name", (person,)).fetchall()
        return [r for r in rows if not json.loads(r["agents"]) or agent in json.loads(r["agents"])]

    # ------------------------------------------------------------ a call
    def prepare(self, agent: str, args: dict) -> tuple:
        """Check an agent's request. Returns (key row, method, url, headers, body, why)."""
        name = str(args.get("key") or "").strip().lower()
        keys = {r["name"]: r for r in self.usable(agent)}
        if name not in keys:
            have = ", ".join(sorted(keys)) or "none yet"
            raise VaultError(f"No saved key called '{name}' that you may use (yours: {have}). "
                             f"Your user adds keys in Relay: Me, Keys.")
        k = keys[name]
        method = str(args.get("method") or "GET").upper()
        if method not in METHODS:
            raise VaultError(f"method is one of {', '.join(METHODS)}.")
        url = str(args.get("url") or "").strip()
        p = urllib.parse.urlsplit(url)
        hosts = json.loads(k["hosts"])
        if p.scheme != "https" or not p.hostname or p.username or p.password or p.port not in (None, 443):
            raise VaultError("url must be a plain https:// address.")
        if p.hostname.lower() not in hosts:
            raise VaultError(f"The '{name}' key only goes to {', '.join(hosts)}, not {p.hostname}.")
        if k["place"] == "query" and k["field"] in dict(urllib.parse.parse_qsl(p.query, keep_blank_values=True)):
            raise VaultError(f"Leave {k['field']} out of the url: the hub adds the key there.")
        headers = {}
        for hk, hv in (args.get("headers") or {}).items():
            hk, hv = str(hk).strip(), str(hv)
            if not FIELD_RE.match(hk) or any(ch in hv for ch in "\r\n") or len(hv) > 2000:
                raise VaultError(f"Header {hk!r} isn't valid.")
            if hk.lower() in BLOCKED_HEADERS or (k["place"] == "header" and hk.lower() == k["field"].lower()):
                raise VaultError(f"Don't set {hk}: the hub sets the key and the connection headers itself.")
            headers[hk] = hv
        if len(headers) > 12:
            raise VaultError("At most 12 headers.")
        body = None
        if args.get("json") is None and isinstance(args.get("body"), (dict, list)):
            args = {**args, "json": args["body"]}       # an object in `body` is JSON: never send Python's text of it
        if args.get("json") is not None:
            body = json.dumps(args["json"]).encode()
            headers.setdefault("Content-Type", "application/json")
        elif args.get("body") is not None:
            body = str(args["body"]).encode()
        if body is not None and len(body) > MAX_BODY:
            raise VaultError("The body is over 1 MB. Send big files as a link instead (get_files gives one).")
        if body is not None and method == "GET":
            raise VaultError("A GET has no body; put the values in the url.")
        why = str(args.get("why") or "").strip()[:300] or None
        return k, method, url, headers, body, why

    def needs_ok(self, k, method: str) -> bool:
        return k["ask"] == "always" or (k["ask"] == "changes" and method in CHANGES)

    def record(self, k, agent: str, method: str, url: str, headers: dict, body: Optional[bytes], why, status: str,
               anchor: Optional[int] = None) -> str:
        cid = "c_" + secrets.token_hex(8)
        with self.store.tx() as c:
            hour = c.execute("SELECT COUNT(*) n FROM vault_calls WHERE key_id=? AND agent=? AND created>?",
                             (k["id"], agent, self.now() - 3600)).fetchone()["n"]
            if hour >= CALLS_PER_HOUR:
                raise VaultError(f"That's {CALLS_PER_HOUR} calls with '{k['name']}' this hour. Try again later.")
            c.execute("INSERT INTO vault_calls(id,key_id,owner,agent,anchor,method,url,headers,body,why,status,created)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (cid, k["id"], k["owner"], agent, anchor, method, url, json.dumps(headers), body, why, status,
                       self.now()))
        if status == "waiting" and self.on_waiting:          # tell the owner's phone: it needs their OK
            try:
                self.on_waiting(k["owner"], cid, agent, method, url, k["name"])
            except Exception:
                pass
        return cid

    def set_anchor(self, cid: str, anchor: int) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE vault_calls SET anchor=? WHERE id=?", (anchor, cid))

    def run(self, cid: str) -> dict:
        """Make the stored request, exactly as recorded, with the key put in; store and return the result."""
        with self.store.ro() as c:
            call = c.execute("SELECT * FROM vault_calls WHERE id=?", (cid,)).fetchone()
            k = c.execute("SELECT * FROM vault_keys WHERE id=?", (call["key_id"],)).fetchone()
        try:
            if not k:
                raise VaultError("The key was removed before this call ran.")
            secret = self._open(k)
            url, headers = call["url"], json.loads(call["headers"])
            host = urllib.parse.urlsplit(url).hostname
            if host not in json.loads(k["hosts"]):          # the owner changed the websites since
                raise VaultError(f"The '{k['name']}' key no longer goes to {host}.")
            if not _public(host, self.resolve):
                raise VaultError(f"{host} doesn't point at a public address, so the key isn't sent there.")
            if k["place"] == "bearer":
                headers["Authorization"] = f"Bearer {secret}"
            elif k["place"] == "header":
                headers[k["field"]] = secret
            else:
                p = urllib.parse.urlsplit(url)
                q = (p.query + "&" if p.query else "") + urllib.parse.urlencode({k["field"]: secret})
                url = urllib.parse.urlunsplit(p._replace(query=q))
            headers.setdefault("User-Agent", "relay-hub")
            status, rh, raw = self.send(call["method"], url, headers, call["body"])
            text = self.redact(raw.decode("utf-8", "replace"), secret)
            ctype = str({k_.lower(): v for k_, v in rh.items()}.get("content-type", ""))[:100]
            where = {k_.lower(): v for k_, v in rh.items()}.get("location")
            if where and 300 <= status < 400:
                text = f"(redirect to {self.redact(where, secret)}; the hub doesn't follow redirects)\n" + text
            out = {"status": "done", "result_status": status, "result_type": ctype, "result": text[:MAX_RESULT * 5],
                   "error": None}
        except VaultError as e:
            out = {"status": "failed", "result_status": None, "result_type": None, "result": None, "error": str(e)}
        with self.store.tx() as c:
            c.execute("UPDATE vault_calls SET status=?, result_status=?, result_type=?, result=?, error=?, finished=?"
                      " WHERE id=?", (out["status"], out["result_status"], out["result_type"], out["result"],
                                      out["error"], self.now(), cid))
            if k:
                c.execute("UPDATE vault_keys SET last_used=? WHERE id=?", (self.now(), k["id"]))
        return self.call(cid)

    @staticmethod
    def redact(text: str, secret: str) -> str:
        forms = {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret),
                 base64.b64encode(secret.encode()).decode(), json.dumps(secret)[1:-1]}
        for f in sorted(forms, key=len, reverse=True):
            if len(f) >= 4:
                text = text.replace(f, "[key hidden]")
        return text

    # ------------------------------------------------------------ approvals
    def call(self, cid: str) -> dict:
        with self.store.ro() as c:
            r = c.execute("SELECT v.*, k.name AS key_name FROM vault_calls v LEFT JOIN vault_keys k ON k.id=v.key_id"
                          " WHERE v.id=?", (cid,)).fetchone()
        if not r:
            raise HTTPException(404, "no such request")
        status = r["status"]
        if status == "waiting" and self.now() - r["created"] > APPROVAL_SECONDS:
            status = "expired"
        body = bytes(r["body"]).decode("utf-8", "replace") if r["body"] is not None else None
        return {"id": r["id"], "key": r["key_name"], "key_id": r["key_id"], "owner": r["owner"], "agent": r["agent"],
                "method": r["method"], "url": r["url"], "headers": json.loads(r["headers"]), "body": body,
                "why": r["why"], "status": status, "decided_by": r["decided_by"], "created": r["created"],
                "decided": r["decided"], "finished": r["finished"], "anchor": r["anchor"],
                "result_status": r["result_status"], "result_type": r["result_type"], "result": r["result"],
                "error": r["error"]}

    def waiting(self, owner: str) -> list:
        with self.store.ro() as c:
            ids = [r["id"] for r in c.execute("SELECT id FROM vault_calls WHERE owner=? AND status='waiting' AND created>?"
                                               " ORDER BY created DESC LIMIT 50", (owner, self.now() - APPROVAL_SECONDS))]
        return [self.call(i) for i in ids]

    def history(self, owner: str, kid: str, limit: int = 30) -> list:
        with self.store.ro() as c:
            self._own(c, owner, kid)
            ids = [r["id"] for r in c.execute("SELECT id FROM vault_calls WHERE key_id=? ORDER BY created DESC LIMIT ?",
                                               (kid, limit))]
        return [self.call(i) for i in ids]

    def decide(self, owner: str, cid: str, ok: bool) -> dict:
        """The owner's answer. Taken once: a second press, or one after it expired, changes nothing."""
        now = self.now()
        with self.store.tx() as c:
            r = c.execute("SELECT * FROM vault_calls WHERE id=? AND owner=?", (cid, owner)).fetchone()
            if not r:
                raise HTTPException(404, "no such request")
            if r["status"] != "waiting" or now - r["created"] > APPROVAL_SECONDS:
                raise HTTPException(409, "This request was already decided, or it expired.")
            c.execute("UPDATE vault_calls SET status=?, decided_by=?, decided=? WHERE id=? AND status='waiting'",
                      ("approved" if ok else "declined", owner, now, cid))
        return self.run(cid) if ok else self.call(cid)

    def wait(self, cid: str, seconds: Optional[float] = None) -> dict:
        end = time.monotonic() + (WAIT_SECONDS if seconds is None else seconds)
        while True:
            got = self.call(cid)
            if got["status"] not in ("waiting", "approved") or time.monotonic() >= end:
                return got
            time.sleep(0.5)


def summary(call: dict) -> str:
    """What an agent reads about a finished call."""
    if call["status"] == "declined":
        return f"{call['decided_by']} said no to {call['method']} {call['url']} with the '{call['key']}' key. Nothing was sent."
    if call["status"] == "expired":
        return f"Nobody approved {call['method']} {call['url']} in time, so nothing was sent. Ask again if it's still needed."
    if call["status"] == "failed":
        return f"{call['method']} {call['url']} didn't go through: {call['error']}"
    text = call["result"] or ""
    more = len(text) > MAX_RESULT
    return (f"{call['method']} {call['url']} with the '{call['key']}' key"
            + (f", approved by {call['decided_by']}" if call["decided_by"] else "")
            + f": HTTP {call['result_status']}" + (f" ({call['result_type']})" if call["result_type"] else "")
            + f"\n{text[:MAX_RESULT]}" + ("\n[cut: the answer was longer]" if more else ""))


def mount(app, hub, vault: Vault) -> None:
    auth = hub.auth

    def person(me: dict) -> str:
        if me["role"] != "human":
            raise HTTPException(403, "Keys belong to people. Sign in as yourself.")
        return me["handle"]

    def fix(fn, *a):
        try:
            return fn(*a)
        except VaultError as e:
            raise HTTPException(400, str(e)) from None

    @app.get("/keys")
    def list_keys(me: dict = Depends(auth)):
        return vault.mine(person(me))

    @app.post("/keys")
    def add_key(body: KeyIn, me: dict = Depends(auth)):
        return fix(vault.add, person(me), body)

    @app.put("/keys/{kid}")
    def edit_key(kid: str, body: KeyEdit, me: dict = Depends(auth)):
        return fix(vault.edit, person(me), kid, body)

    @app.delete("/keys/{kid}")
    def remove_key(kid: str, me: dict = Depends(auth)):
        vault.remove(person(me), kid)
        return {"removed": kid}

    @app.get("/keys/{kid}/calls")
    def key_calls(kid: str, me: dict = Depends(auth)):
        return vault.history(person(me), kid)

    @app.get("/approvals")
    def approvals(me: dict = Depends(auth)):
        return vault.waiting(person(me))

    @app.get("/approvals/{cid}")
    def approval(cid: str, me: dict = Depends(auth)):
        got = vault.call(cid)
        if got["owner"] != person(me):
            raise HTTPException(404, "no such request")
        return got

    def answer(cid: str, me: dict, ok: bool) -> dict:
        got = vault.decide(person(me), cid, ok)
        if got["anchor"] and cid not in vault.watching:            # the result goes back to the chat that asked, in its conversation
            with hub.store.tx() as c:
                if c.execute("SELECT 1 FROM messages WHERE id=?", (got["anchor"],)).fetchone():
                    hub.post(c, hub.SYSTEM, ("🔑 " if ok else "✋ ") + summary(got)[:4000],
                             reply_to=got["anchor"], mentions=[got["agent"]])
        return got

    @app.post("/approvals/{cid}/approve")
    def approve(cid: str, me: dict = Depends(auth)):
        return answer(cid, me, True)

    @app.post("/approvals/{cid}/decline")
    def decline(cid: str, me: dict = Depends(auth)):
        return answer(cid, me, False)
