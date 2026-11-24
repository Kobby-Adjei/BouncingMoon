"""Connect any app to Relay the way you connect Gmail: paste the URL, sign in, press Allow.

Relay is its own OAuth 2.1 authorization server, the way the MCP spec expects a remote MCP server
to be. Claude, ChatGPT, Cursor or any MCP client finds it by itself:

  /mcp answers 401 with   WWW-Authenticate: Bearer resource_metadata=".../.well-known/oauth-protected-resource"
  /.well-known/oauth-protected-resource     names this hub as the authorization server (RFC 9728)
  /.well-known/oauth-authorization-server   the endpoints (RFC 8414)
  /oauth/register                           the app registers itself (RFC 7591), or its client_id is
                                            a URL to its own metadata (client ID metadata documents)
  /oauth/authorize                          a page: sign in (passkey, email code, or a Relay key),
                                            then "Allow <app> to use Relay as <agent>"
  /oauth/token                              code + PKCE (S256 only) -> an access token and a refresh
                                            token; refresh tokens rotate, and a reused one ends the
                                            whole connection (it was copied)

No company has to approve any of it: sign-in is a passkey (Face ID, a fingerprint, a PIN) or an
email code, both checked here. The person never sees or carries a key. The first time an app
connects, the agent can be made on the spot, and it belongs to the person who allowed it.
Every connection can be ended from /oauth/connections. Keys that already work keep working.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from . import providers as P
from . import webauthn as W

ACCESS_SECONDS = 24 * 3600
REFRESH_SECONDS = 90 * 86400
CODE_SECONDS = 600
REQUEST_SECONDS = 1800             # how long the sign-in page stays good
CHALLENGE_SECONDS = 300
MAX_CLIENTS = 2000
SCOPE = "relay"
BAD_SCHEMES = {"javascript", "data", "file", "vbscript", "about", "blob", "http"}
LOOPBACK = ("localhost", "127.0.0.1", "[::1]", "::1")

SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients(
  client_id TEXT PRIMARY KEY, name TEXT, uri TEXT, redirect_uris TEXT NOT NULL, secret_hash TEXT,
  created REAL, fetched REAL);
CREATE TABLE IF NOT EXISTS oauth_requests(
  id TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
  state TEXT, scope TEXT, resource TEXT, created REAL, expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes(
  code_hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL, challenge TEXT NOT NULL,
  scope TEXT, handle TEXT NOT NULL, person TEXT, created REAL, expires REAL NOT NULL, used REAL);
CREATE TABLE IF NOT EXISTS oauth_tokens(
  token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, handle TEXT NOT NULL, person TEXT, client_id TEXT NOT NULL,
  scope TEXT, family TEXT NOT NULL, created REAL, expires REAL NOT NULL, revoked REAL);
CREATE INDEX IF NOT EXISTS oauth_tokens_family ON oauth_tokens(family);
CREATE TABLE IF NOT EXISTS passkeys(
  cred_id TEXT PRIMARY KEY, handle TEXT NOT NULL, cose BLOB NOT NULL, count INTEGER NOT NULL DEFAULT 0,
  name TEXT, created REAL, last_used REAL);
CREATE TABLE IF NOT EXISTS webauthn_challenges(
  challenge TEXT PRIMARY KEY, kind TEXT NOT NULL, handle TEXT, created REAL, expires REAL NOT NULL);
"""


def _h(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def app_word(name: str) -> Optional[str]:
    """The provider an app's registered name points to; see providers.py."""
    return P.detect(name)


def redirect_ok(uri: str) -> bool:
    """https anywhere, http only to this computer (desktop apps), or an app's own scheme (cursor://)."""
    try:
        u = urllib.parse.urlsplit(uri)
    except ValueError:
        return False
    if u.fragment or not u.scheme:
        return False
    if u.scheme == "https":
        return bool(u.hostname)
    if u.scheme == "http":
        return (u.hostname or "") in LOOPBACK
    return u.scheme not in BAD_SCHEMES and bool(re.fullmatch(r"[a-z][a-z0-9+.-]{1,40}", u.scheme))


def redirect_matches(given: str, registered: list) -> bool:
    """Exact match; for this computer (localhost) the port may differ, as RFC 8252 asks."""
    if given in registered:
        return True
    g = urllib.parse.urlsplit(given)
    if g.scheme != "http" or (g.hostname or "") not in LOOPBACK:
        return False
    for r in registered:
        u = urllib.parse.urlsplit(r)
        if u.scheme == "http" and u.hostname == g.hostname and u.path == g.path and u.query == g.query:
            return True
    return False


def with_query(uri: str, params: dict) -> str:
    u = urllib.parse.urlsplit(uri)
    q = urllib.parse.parse_qsl(u.query, keep_blank_values=True) + [(k, v) for k, v in params.items() if v is not None]
    return urllib.parse.urlunsplit((u.scheme, u.netloc, u.path, urllib.parse.urlencode(q), ""))


class ClientIn(BaseModel):
    redirect_uris: list[str]
    client_name: Optional[str] = None
    client_uri: Optional[str] = None
    token_endpoint_auth_method: Optional[str] = None
    grant_types: Optional[list[str]] = None
    response_types: Optional[list[str]] = None
    scope: Optional[str] = None


class DecideIn(BaseModel):
    request: str
    agent: Optional[str] = None
    new_agent: Optional[str] = None


class DenyIn(BaseModel):
    request: str


def mount(app, hub) -> None:
    store, auth, now = hub.store, hub.auth, hub.now
    with store.tx() as c:
        for stmt in SCHEMA.split(";"):
            if stmt.strip():
                c.execute(stmt)
        if "person" not in {r["name"] for r in c.execute("PRAGMA table_info(agents)")}:
            c.execute("ALTER TABLE agents ADD COLUMN person TEXT")      # the person an agent belongs to

    def issuer(request: Request) -> str:
        return hub.origin(request)

    def cors(resp: Response) -> Response:
        resp.headers["Access-Control-Allow-Origin"] = "*"           # MCP clients in a browser read these
        resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, MCP-Protocol-Version"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        return resp

    def js(data, status: int = 200, extra: Optional[dict] = None) -> JSONResponse:
        return cors(JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store", **(extra or {})}))

    def oauth_error(err: str, desc: str, status: int = 400) -> JSONResponse:
        return js({"error": err, "error_description": desc}, status)

    # ------------------------------------------------------------ discovery
    def resource_meta(request: Request):
        iss = issuer(request)
        return js({"resource": iss + "/mcp", "authorization_servers": [iss], "scopes_supported": [SCOPE],
                   "bearer_methods_supported": ["header"], "resource_name": "Relay"})

    def server_meta(request: Request):
        iss = issuer(request)
        return js({"issuer": iss, "authorization_endpoint": iss + "/oauth/authorize",
                   "token_endpoint": iss + "/oauth/token", "registration_endpoint": iss + "/oauth/register",
                   "revocation_endpoint": iss + "/oauth/revoke", "scopes_supported": [SCOPE],
                   "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
                   "code_challenge_methods_supported": ["S256"],
                   "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
                   "client_id_metadata_document_supported": True,
                   "authorization_response_iss_parameter_supported": True})

    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        app.add_api_route(path, resource_meta, methods=["GET"], include_in_schema=False)
    for path in ("/.well-known/oauth-authorization-server", "/.well-known/oauth-authorization-server/mcp",
                 "/.well-known/openid-configuration"):
        app.add_api_route(path, server_meta, methods=["GET"], include_in_schema=False)

    @app.options("/oauth/{rest:path}", include_in_schema=False)
    def oauth_preflight(rest: str):
        return cors(Response(status_code=204))

    # ------------------------------------------------------------ app registration
    @app.post("/oauth/register", include_in_schema=False)
    def register(body: ClientIn):
        uris = [u.strip() for u in body.redirect_uris if u and u.strip()]
        if not uris or len(uris) > 10 or not all(redirect_ok(u) and len(u) <= 500 for u in uris):
            return oauth_error("invalid_redirect_uri", "redirect URIs must be https, http://localhost, or an app scheme")
        name = re.sub(r"[\x00-\x1f]", "", (body.client_name or "An app"))[:80]
        method = body.token_endpoint_auth_method or "none"
        if method not in ("none", "client_secret_post", "client_secret_basic"):
            return oauth_error("invalid_client_metadata", "unsupported token_endpoint_auth_method")
        cid = "rc_" + secrets.token_urlsafe(18)
        secret = "rcs_" + secrets.token_urlsafe(32) if method != "none" else None
        with store.tx() as c:
            if c.execute("SELECT COUNT(*) AS n FROM oauth_clients").fetchone()["n"] >= MAX_CLIENTS:
                return oauth_error("temporarily_unavailable", "too many registered apps", 503)
            c.execute("INSERT INTO oauth_clients(client_id,name,uri,redirect_uris,secret_hash,created) VALUES (?,?,?,?,?,?)",
                      (cid, name, (body.client_uri or "")[:300] or None, json.dumps(uris), _h(secret) if secret else None, now()))
        out = {"client_id": cid, "client_name": name, "redirect_uris": uris, "token_endpoint_auth_method": method,
               "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
               "client_id_issued_at": int(now())}
        if secret:
            out["client_secret"], out["client_secret_expires_at"] = secret, 0
        return js(out, 201)

    def fetch_metadata(url: str) -> dict:
        """A client whose id is a URL publishes its own metadata there (MCP client ID metadata documents)."""
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" or not u.hostname or hub.private_host(u.hostname) or u.fragment:
            raise ValueError("client_id must be a public https URL")
        from .captions import guarded_opener
        opener = guarded_opener(hub.private_host)
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Relay/1.0 (oauth)"})
        with opener.open(req, timeout=6) as r:
            raw = r.read(65537)
        if len(raw) > 65536:
            raise ValueError("client metadata too large")
        doc = json.loads(raw)
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            raise ValueError("client metadata doesn't name itself")
        uris = [x for x in doc.get("redirect_uris") or [] if isinstance(x, str) and redirect_ok(x)]
        if not uris:
            raise ValueError("client metadata has no usable redirect URIs")
        return {"name": str(doc.get("client_name") or u.hostname)[:80], "uri": doc.get("client_uri"), "redirect_uris": uris}

    def client(cid: str):
        with store.ro() as c:
            r = c.execute("SELECT * FROM oauth_clients WHERE client_id=?", (cid,)).fetchone()
        if r and not (cid.startswith("https://") and (r["fetched"] or 0) < now() - 86400):
            return r
        if not cid.startswith("https://"):
            return None
        try:
            meta = fetch_metadata(cid)
        except Exception:
            return r                                    # keep what we had; None if never seen
        with store.tx() as c:
            c.execute("INSERT INTO oauth_clients(client_id,name,uri,redirect_uris,created,fetched) VALUES (?,?,?,?,?,?)"
                      " ON CONFLICT(client_id) DO UPDATE SET name=excluded.name, uri=excluded.uri,"
                      " redirect_uris=excluded.redirect_uris, fetched=excluded.fetched",
                      (cid, meta["name"], meta["uri"], json.dumps(meta["redirect_uris"]), now(), now()))
            return c.execute("SELECT * FROM oauth_clients WHERE client_id=?", (cid,)).fetchone()

    # ------------------------------------------------------------ the sign-in and Allow page
    def error_page(msg: str, status: int = 400) -> HTMLResponse:
        return HTMLResponse(page("Can't connect", f"<h1>Can't connect</h1><p class=muted>{html.escape(msg)}</p>", ""),
                            status_code=status, headers=page_headers(None))

    @app.get("/oauth/authorize", include_in_schema=False)
    def authorize(request: Request):
        q = request.query_params
        cid, redirect = q.get("client_id", ""), q.get("redirect_uri")
        cl = client(cid) if cid else None
        if not cl:
            return error_page("This app isn't registered with Relay. Remove the connector and add it again.")
        regs = json.loads(cl["redirect_uris"])
        if not redirect and len(regs) == 1:
            redirect = regs[0]
        if not redirect or not redirect_matches(redirect, regs):        # never send anything to an unknown address
            return error_page("This app asked to come back to an address it didn't register.")
        state = q.get("state")

        def back(err: str, desc: str):
            return Response(status_code=302, headers={"Location": with_query(
                redirect, {"error": err, "error_description": desc, "state": state, "iss": issuer(request)})})
        if q.get("response_type") != "code":
            return back("unsupported_response_type", "only response_type=code")
        challenge, method = q.get("code_challenge", ""), q.get("code_challenge_method", "")
        if method != "S256" or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", challenge):
            return back("invalid_request", "PKCE with S256 is required")
        resource = q.get("resource")
        if resource and resource.rstrip("/") not in (issuer(request) + "/mcp", issuer(request)):
            return back("invalid_target", "this server only issues tokens for its own /mcp")
        rid = secrets.token_urlsafe(18)
        with store.tx() as c:
            c.execute("DELETE FROM oauth_requests WHERE expires<?", (now(),))
            c.execute("INSERT INTO oauth_requests(id,client_id,redirect_uri,challenge,state,scope,resource,created,expires)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (rid, cl["client_id"], redirect, challenge, state,
                                                     q.get("scope") or SCOPE, resource, now(), now() + REQUEST_SECONDS))
        nonce = secrets.token_urlsafe(12)
        host = urllib.parse.urlsplit(redirect).hostname or urllib.parse.urlsplit(redirect).scheme
        word = app_word(cl["name"]) or ""
        logo = word if word in P.LOGOS else None
        boot = {"request": rid, "app": cl["name"], "host": host, "email": hub.email_on, "hub": issuer(request)}
        left = (f"<img src='/logos/{logo}.svg' alt=''>" if logo else f"<b>{html.escape((cl['name'] or '?')[:1].upper())}</b>")
        body = (f"<div class=glow aria-hidden=true></div><div class=shell>"
                f"<div class=orb id=orb aria-hidden=true><span class='half l'>{left}</span>"
                f"<span class='half r' id=rhalf><i class=mk></i></span><span class=seal id=seal></span></div>"
                f"<h1 class=title id=title>Connect {html.escape(cl['name'])} to Relay</h1>"
                f"<p class=lede id=lede>Pick who {html.escape(cl['name'] or 'this app')} will be on Relay.</p>"
                f"<section class=step id=step aria-live=polite></section>"
                f"<p class=foot id=foot>Returns to {html.escape(host)}</p></div>"
                f"<script nonce='{nonce}'>const BOOT={json.dumps(boot)};{PAGE_JS}</script>")
        return HTMLResponse(page(f"Connect {cl['name']}", body, nonce), headers=page_headers(nonce))

    def who(authorization: str) -> dict:
        me = auth(authorization)
        if me["role"] == "agent":
            raise HTTPException(403, "Sign in as yourself (a person), not with an agent's key.")
        with store.ro() as c:
            r = c.execute("SELECT owner FROM agents WHERE handle=?", (me["handle"],)).fetchone() if me["role"] != "admin" else None
        me["manages_all"] = me["role"] == "admin" or bool(r and r["owner"])
        return me

    def pending(rid: str):
        with store.ro() as c:
            r = c.execute("SELECT * FROM oauth_requests WHERE id=? AND expires>?", (rid, now())).fetchone()
        if not r:
            raise HTTPException(410, "This sign-in page has expired. Go back to the app and connect again.")
        return r

    def choosable(c, me: dict) -> list:
        if me["manages_all"]:
            rows = c.execute("SELECT handle, provider, last_seen, person FROM agents WHERE role='agent' ORDER BY handle")
        else:
            rows = c.execute("SELECT handle, provider, last_seen, person FROM agents WHERE role='agent' AND person=?"
                             " ORDER BY handle", (me["handle"],))
        return [dict(r) for r in rows]

    @app.get("/oauth/request/{rid}", include_in_schema=False)
    def request_info(rid: str, request: Request):
        me = who(request.headers.get("authorization", ""))
        r = pending(rid)
        cl = client(r["client_id"])
        kind = app_word(cl["name"] if cl else "")
        word = P.handle_for(kind)
        with store.ro() as c:
            agents = choosable(c, me)
            taken = {x["handle"] for x in c.execute("SELECT handle FROM agents")}
        suggest = word if word not in taken else next(f"{word}-{i}" for i in range(2, 999) if f"{word}-{i}" not in taken)
        # the app's own agent: one named for it, else the one on the same app used most recently
        mine = [a for a in agents if a["handle"] == word] or \
            sorted([a for a in agents if kind and a["provider"] == kind], key=lambda a: -(a["last_seen"] or 0))
        return {"you": me["handle"], "app": cl["name"] if cl else "An app", "agents": agents, "provider": kind,
                "app_label": P.label(kind) if kind else (cl["name"] if cl else "This app"),
                "preselect": mine[0]["handle"] if mine else None, "suggest": suggest, "role": me["role"]}

    @app.post("/oauth/decide", include_in_schema=False)
    def decide(body: DecideIn, request: Request):
        me = who(request.headers.get("authorization", ""))
        r = pending(body.request)
        cl = client(r["client_id"])
        with store.tx() as c:
            if body.new_agent:
                handle = body.new_agent.strip().lower()
                hub.check_new_agent(handle, "agent")
                if c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone():
                    raise HTTPException(409, f"{handle} is already taken; pick another name")
                provider = app_word(cl["name"] if cl else "")
                c.execute("INSERT INTO agents(handle,token_hash,caps,role,created,provider,provider_source,person)"
                          " VALUES (?,?,?,?,?,?,?,?)", (handle, _h("rly_" + secrets.token_urlsafe(32)), "[]", "agent",
                                                        now(), provider, "detected" if provider else None,
                                                        None if me["role"] == "admin" else me["handle"]))
                c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (handle,))
            else:
                handle = (body.agent or "").strip()
                if handle not in {a["handle"] for a in choosable(c, me)}:
                    raise HTTPException(403, "You can only connect agents that are yours.")
            code = "roc_" + secrets.token_urlsafe(32)
            c.execute("INSERT INTO oauth_codes(code_hash,client_id,redirect_uri,challenge,scope,handle,person,created,expires)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (_h(code), r["client_id"], r["redirect_uri"], r["challenge"],
                                                     r["scope"], handle, None if me["role"] == "admin" else me["handle"],
                                                     now(), now() + CODE_SECONDS))
            c.execute("DELETE FROM oauth_requests WHERE id=?", (r["id"],))
        return {"redirect": with_query(r["redirect_uri"], {"code": code, "state": r["state"], "iss": issuer(request)}),
                "agent": handle}

    @app.post("/oauth/deny", include_in_schema=False)
    def deny(body: DenyIn, request: Request):
        r = pending(body.request)
        with store.tx() as c:
            c.execute("DELETE FROM oauth_requests WHERE id=?", (r["id"],))
        return {"redirect": with_query(r["redirect_uri"], {"error": "access_denied", "error_description": "You said no.",
                                                           "state": r["state"], "iss": issuer(request)})}

    # ------------------------------------------------------------ tokens
    def issue(c, handle: str, person: Optional[str], client_id: str, scope: str, family: Optional[str] = None) -> dict:
        access, refresh = "rly_oat_" + secrets.token_urlsafe(32), "rly_ort_" + secrets.token_urlsafe(32)
        family = family or "f_" + secrets.token_urlsafe(12)
        t = now()
        c.execute("INSERT INTO oauth_tokens(token_hash,kind,handle,person,client_id,scope,family,created,expires)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", (_h(access), "access", handle, person, client_id, scope, family, t, t + ACCESS_SECONDS))
        c.execute("INSERT INTO oauth_tokens(token_hash,kind,handle,person,client_id,scope,family,created,expires)"
                  " VALUES (?,?,?,?,?,?,?,?,?)", (_h(refresh), "refresh", handle, person, client_id, scope, family, t, t + REFRESH_SECONDS))
        return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_SECONDS,
                "refresh_token": refresh, "scope": scope}

    async def form(request: Request) -> dict:
        raw = (await request.body())[:20000].decode("utf-8", "replace")
        if "json" in request.headers.get("content-type", ""):
            try:
                d = json.loads(raw)
                return {k: str(v) for k, v in d.items()} if isinstance(d, dict) else {}
            except ValueError:
                return {}
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def client_auth(request: Request, f: dict):
        """(client row, error). Public apps send only client_id; apps with a secret must prove it."""
        cid, secret = f.get("client_id"), f.get("client_secret")
        basic = request.headers.get("authorization", "")
        if basic.lower().startswith("basic "):
            try:
                pair = base64.b64decode(basic[6:]).decode()
                cid, secret = [urllib.parse.unquote(x) for x in pair.split(":", 1)]
            except (ValueError, UnicodeDecodeError):
                return None, oauth_error("invalid_client", "bad Basic credentials", 401)
        cl = client(cid or "")
        if not cl:
            return None, oauth_error("invalid_client", "unknown client", 401)
        if cl["secret_hash"] and not (secret and secrets.compare_digest(_h(secret), cl["secret_hash"])):
            return None, oauth_error("invalid_client", "wrong client secret", 401)
        return cl, None

    @app.post("/oauth/token", include_in_schema=False)
    async def token(request: Request):
        f = await form(request)
        cl, err = client_auth(request, f)
        if err:
            return err
        grant = f.get("grant_type")
        with store.tx() as c:
            if grant == "authorization_code":
                r = c.execute("SELECT * FROM oauth_codes WHERE code_hash=?", (_h(f.get("code", "")),)).fetchone()
                if not r or r["client_id"] != cl["client_id"]:
                    return oauth_error("invalid_grant", "unknown code")
                if r["used"]:                       # a code used twice: someone copied it. End what it gave.
                    c.execute("UPDATE oauth_tokens SET revoked=? WHERE client_id=? AND handle=? AND created>=? AND revoked IS NULL",
                              (now(), r["client_id"], r["handle"], r["created"]))
                    return oauth_error("invalid_grant", "code already used")
                if r["expires"] < now():
                    return oauth_error("invalid_grant", "code expired")
                if f.get("redirect_uri") and f["redirect_uri"] != r["redirect_uri"]:
                    return oauth_error("invalid_grant", "redirect_uri doesn't match")
                if s256(f.get("code_verifier", "")) != r["challenge"]:
                    return oauth_error("invalid_grant", "PKCE verifier doesn't match")
                if not c.execute("SELECT 1 FROM agents WHERE handle=?", (r["handle"],)).fetchone():
                    return oauth_error("invalid_grant", "that agent no longer exists")
                c.execute("UPDATE oauth_codes SET used=? WHERE code_hash=?", (now(), r["code_hash"]))
                return js(issue(c, r["handle"], r["person"], cl["client_id"], r["scope"] or SCOPE))
            if grant == "refresh_token":
                r = c.execute("SELECT * FROM oauth_tokens WHERE token_hash=? AND kind='refresh'",
                              (_h(f.get("refresh_token", "")),)).fetchone()
                if not r or r["client_id"] != cl["client_id"]:
                    return oauth_error("invalid_grant", "unknown refresh token")
                if r["revoked"]:                    # an old refresh token again: it was copied. End the connection.
                    c.execute("UPDATE oauth_tokens SET revoked=? WHERE family=? AND revoked IS NULL", (now(), r["family"]))
                    return oauth_error("invalid_grant", "refresh token already used; connect again")
                if r["expires"] < now():
                    return oauth_error("invalid_grant", "refresh token expired; connect again")
                if not c.execute("SELECT 1 FROM agents WHERE handle=?", (r["handle"],)).fetchone():
                    return oauth_error("invalid_grant", "that agent no longer exists")
                c.execute("UPDATE oauth_tokens SET revoked=? WHERE token_hash=?", (now(), r["token_hash"]))
                return js(issue(c, r["handle"], r["person"], cl["client_id"], r["scope"] or SCOPE, r["family"]))
        return oauth_error("unsupported_grant_type", "authorization_code or refresh_token")

    @app.post("/oauth/revoke", include_in_schema=False)
    async def revoke(request: Request):
        f = await form(request)
        with store.tx() as c:
            r = c.execute("SELECT family FROM oauth_tokens WHERE token_hash=?", (_h(f.get("token", "")),)).fetchone()
            if r:
                c.execute("UPDATE oauth_tokens SET revoked=? WHERE family=? AND revoked IS NULL", (now(), r["family"]))
        return js({})

    # ------------------------------------------------------------ connected apps (people end them here)
    @app.get("/oauth/connections")
    def connections(me: dict = Depends(auth)):
        if me["role"] == "agent":
            raise HTTPException(403, "people manage connections")
        owner = me["role"] == "admin" or me.get("owner")
        with store.ro() as c:
            rows = c.execute(
                "SELECT t.family, t.handle, t.client_id, MIN(t.created) AS since, MAX(t.created) AS last, k.name AS app"
                " FROM oauth_tokens t LEFT JOIN oauth_clients k ON k.client_id=t.client_id"
                " WHERE t.revoked IS NULL AND t.expires>? AND (? OR t.person=?) GROUP BY t.family ORDER BY last DESC",
                (now(), 1 if owner else 0, me["handle"])).fetchall()
        return [{"id": r["family"], "app": r["app"], "agent": r["handle"], "since": r["since"], "last_active": r["last"]}
                for r in rows]

    @app.delete("/oauth/connections/{family}")
    def end_connection(family: str, me: dict = Depends(auth)):
        if me["role"] == "agent":
            raise HTTPException(403, "people manage connections")
        owner = me["role"] == "admin" or me.get("owner")
        with store.tx() as c:
            n = c.execute("UPDATE oauth_tokens SET revoked=? WHERE family=? AND revoked IS NULL AND (? OR person=?)",
                          (now(), family, 1 if owner else 0, me["handle"])).rowcount
        if not n:
            raise HTTPException(404, "no such connection")
        return {"ended": family}

    # ------------------------------------------------------------ passkeys
    def site(request: Request) -> tuple:
        iss = issuer(request)
        return iss, urllib.parse.urlsplit(iss).hostname

    def new_challenge(kind: str, handle: Optional[str]) -> str:
        ch = W.b64u(secrets.token_bytes(32))
        with store.tx() as c:
            c.execute("DELETE FROM webauthn_challenges WHERE expires<?", (now(),))
            c.execute("INSERT INTO webauthn_challenges(challenge,kind,handle,created,expires) VALUES (?,?,?,?,?)",
                      (ch, kind, handle, now(), now() + CHALLENGE_SECONDS))
        return ch

    def take_challenge(body: dict, kind: str) -> Optional[str]:
        """The challenge the browser signed, used up now whatever happens next. Returns its owner."""
        try:
            cd = json.loads(W.unb64u(((body.get("response") or {}).get("clientDataJSON", ""))))
        except (ValueError, W.PasskeyError):
            raise HTTPException(400, "That passkey answer didn't check out.")
        ch = str(cd.get("challenge", ""))
        with store.tx() as c:
            r = c.execute("SELECT * FROM webauthn_challenges WHERE challenge=? AND kind=?", (ch, kind)).fetchone()
            c.execute("DELETE FROM webauthn_challenges WHERE challenge=?", (ch,))
        if not r or r["expires"] < now():
            raise HTTPException(400, "That took too long. Try again.")
        return ch, r["handle"]

    def person_of(me: dict) -> str:
        if me["role"] != "human":
            raise HTTPException(403, "Passkeys belong to people. Sign in as yourself first.")
        return me["handle"]

    @app.post("/auth/passkey/register/options", include_in_schema=False)
    def passkey_register_options(request: Request, me: dict = Depends(auth)):
        handle = person_of(me)
        iss, rp = site(request)
        with store.ro() as c:
            have = [r["cred_id"] for r in c.execute("SELECT cred_id FROM passkeys WHERE handle=?", (handle,))]
        return {"challenge": new_challenge("create", handle), "rp": {"id": rp, "name": "Relay"},
                "user": {"id": W.b64u(handle.encode()), "name": handle, "displayName": handle},
                "pubKeyCredParams": [{"type": "public-key", "alg": a} for a in W.ALGS],
                "authenticatorSelection": {"residentKey": "required", "userVerification": "preferred"},
                "excludeCredentials": [{"type": "public-key", "id": x} for x in have],
                "timeout": CHALLENGE_SECONDS * 1000, "attestation": "none"}

    @app.post("/auth/passkey/register", include_in_schema=False)
    async def passkey_register(request: Request, me: dict = Depends(auth)):
        handle = person_of(me)
        body = await request.json()
        ch, owner = take_challenge(body, "create")
        if owner != handle:
            raise HTTPException(400, "That passkey answer was for someone else.")
        iss, rp = site(request)
        try:
            got = W.registration(body, ch, iss, rp)
        except W.PasskeyError as e:
            hub.log.warning("passkey registration refused for @%s: %s", handle, e)
            raise HTTPException(400, "That passkey couldn't be added. Try again.") from None
        with store.tx() as c:
            c.execute("INSERT OR REPLACE INTO passkeys(cred_id,handle,cose,count,name,created) VALUES (?,?,?,?,?,?)",
                      (got["cred_id"], handle, got["cose"], got["count"],
                       re.sub(r"[\x00-\x1f]", "", str(body.get("label") or "passkey"))[:60], now()))
        return {"added": True}

    @app.post("/auth/passkey/options", include_in_schema=False)
    def passkey_options(request: Request):
        iss, rp = site(request)
        return {"challenge": new_challenge("get", None), "rpId": rp, "userVerification": "preferred",
                "timeout": CHALLENGE_SECONDS * 1000}

    @app.post("/auth/passkey/signin", include_in_schema=False)
    async def passkey_signin(request: Request):
        body = await request.json()
        ch, _ = take_challenge(body, "get")
        iss, rp = site(request)
        with store.ro() as c:
            k = c.execute("SELECT * FROM passkeys WHERE cred_id=?", (str(body.get("id", "")),)).fetchone()
        nope = HTTPException(401, "That passkey isn't one this hub knows. Sign in another way, then add it.")
        if not k:
            raise nope
        uh = W.user_handle(body)
        if uh and uh != k["handle"]:
            raise nope
        try:
            count = W.assertion(body, ch, iss, rp, k["cose"], k["count"])
        except W.PasskeyError as e:
            hub.log.warning("passkey sign-in refused for @%s: %s", k["handle"], e)
            raise nope from None
        with store.tx() as c:
            c.execute("UPDATE passkeys SET count=?, last_used=? WHERE cred_id=?", (count, now(), k["cred_id"]))
        return hub.new_session(k["handle"])

    @app.post("/auth/session/owner", include_in_schema=False)
    def session_from_key(me: dict = Depends(auth)):
        """A person who just made the hub theirs (or used their backup key) gets a normal sign-in session,
        so the browser keeps a session that expires, not the key itself."""
        return hub.new_session(person_of(me))

    @app.get("/auth/passkeys")
    def list_passkeys(me: dict = Depends(auth)):
        handle = person_of(me)
        with store.ro() as c:
            return [{"id": r["cred_id"], "name": r["name"], "created": r["created"], "last_used": r["last_used"]}
                    for r in c.execute("SELECT * FROM passkeys WHERE handle=? ORDER BY created", (handle,))]

    @app.delete("/auth/passkeys/{cred_id}")
    def delete_passkey(cred_id: str, me: dict = Depends(auth)):
        handle = person_of(me)
        with store.tx() as c:
            if not c.execute("DELETE FROM passkeys WHERE cred_id=? AND handle=?", (cred_id, handle)).rowcount:
                raise HTTPException(404, "no such passkey")
        return {"removed": cred_id}


# --------------------------------------------------------------------------- the page
def page_headers(nonce: Optional[str]) -> dict:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return {"Content-Security-Policy": (f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; "
                                        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'; "
                                        "frame-ancestors 'none'"),
            "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def page(title: str, body: str, nonce: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><link rel=icon href='/icon.svg' type='image/svg+xml'>"
            f"<link rel=apple-touch-icon href='/apple-touch-icon.png'><title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


GRAIN = ("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'>"
         "<feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3' stitchTiles='stitch'/><feColorMatrix "
         "values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 .55 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>")

PAGE_CSS = """
:root{--bg:#f6f4ef;--card:#fff;--ink:#18171d;--ink2:#6b6972;--ink3:#c4c1c9;--fill:rgba(24,23,29,.05);--accent:#4f44e0;--accent-hover:#4338d0;
 --on-accent:#fff;--accent-soft:#e6e4fb;--glow:rgba(79,68,224,.16);--red:#c42838;--green:#22a565;--grain:.07;
 --ease:cubic-bezier(.22,.8,.24,1);--spring:cubic-bezier(.34,1.5,.55,1);--glide:cubic-bezier(.32,.72,0,1);
 --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
 --display:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",system-ui,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--bg:#111014;--card:#201f26;--ink:#f4f3f6;--ink2:#a3a1ab;--ink3:#45434c;--fill:rgba(255,255,255,.07);
 --accent:#9a93ff;--accent-hover:#aba5ff;--on-accent:#0e0d12;--accent-soft:#2b2944;--glow:rgba(154,147,255,.14);--red:#ff7076;--green:#46d58b;--grain:.05}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--ink);min-height:100%}
body{font:16px/1.4 var(--font);-webkit-font-smoothing:antialiased;letter-spacing:-.012em;overflow-x:hidden}
body:before{content:"";position:fixed;inset:0;pointer-events:none;opacity:var(--grain);background:url("GRAIN_URL");z-index:0}
.glow{position:fixed;left:50%;top:-140px;width:720px;height:560px;margin-left:-360px;pointer-events:none;
 background:radial-gradient(closest-side,var(--glow),transparent);z-index:0}
main{position:relative;z-index:1;min-height:100vh;display:flex;justify-content:center;
 padding:max(48px,env(safe-area-inset-top)) max(22px,env(safe-area-inset-right)) 36px max(22px,env(safe-area-inset-left))}
.shell{width:100%;max-width:380px;display:flex;flex-direction:column;align-items:center;text-align:center}
@keyframes inL{from{transform:translateX(-46px) rotate(-14deg);opacity:0}to{transform:none;opacity:1}}
@keyframes inR{from{transform:translateX(46px) rotate(14deg);opacity:0}to{transform:none;opacity:1}}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes flipin{from{transform:perspective(500px) rotateY(80deg);opacity:0}to{transform:none;opacity:1}}
@keyframes fuse{0%{transform:none}55%{transform:rotate(200deg) scale(.92)}100%{transform:rotate(360deg)}}
@keyframes seal{0%{opacity:0;transform:scale(.2)}65%{opacity:1;transform:scale(1.12)}100%{opacity:1;transform:none}}
@keyframes nudge{0%,100%{transform:none}40%{transform:translateY(-5px) scale(1.06)}}
.rise{animation:rise .65s var(--ease) both;animation-delay:calc(var(--i,0)*60ms + .25s)}
/* the orb: two halves of Relay's mark. Left is the app; right is who it will be */
.orb{position:relative;width:176px;height:176px;display:flex;gap:7px;margin-bottom:30px;transition:gap .5s var(--glide)}
.half{flex:1;display:grid;place-items:center;overflow:hidden;position:relative;
 box-shadow:0 1px 0 rgba(255,255,255,.6) inset,0 18px 40px -18px rgba(24,23,29,.35),0 0 0 .5px rgba(24,23,29,.08)}
.half.l{border-radius:999px 0 0 999px;background:var(--card);animation:inL .9s var(--spring) both}
.half.r{border-radius:0 999px 999px 0;background:#18171d;animation:inR .9s var(--spring) .08s both}
.half img{width:52px;height:52px}.half.l img{margin-left:22px}.half.r img,.half.r b,.half.r .mk{margin-right:22px}
.half b{font:700 44px/1 var(--display);color:var(--ink2)}.half.l b{margin-left:22px}
.half.r.agent{background:var(--card)}.half.r.agent b{color:var(--accent)}.half.r.new{background:var(--accent-soft)}
.half.r .mk{width:34px;height:68px;border-radius:0 68px 68px 0;background:#f4f3f6;display:block;margin-right:34px}
.half.r .face{display:grid;place-items:center;animation:flipin .55s var(--spring) backwards}
.half.r.new .face svg{width:54px;height:54px;color:var(--accent);margin-right:20px}
.seal{position:absolute;left:50%;top:50%;width:64px;height:64px;margin:-32px 0 0 -32px;border-radius:50%;background:var(--green);color:#fff;
 display:grid;place-items:center;opacity:0;box-shadow:0 0 0 6px var(--bg),0 14px 30px -10px rgba(34,165,101,.6)}
.seal svg{width:30px;height:30px}
.orb.fused{gap:0;animation:fuse 1s var(--glide) .1s both}.half>*{transition:opacity .35s var(--ease) .55s}.orb.fused .half>*{opacity:0}.orb.fused .seal{animation:seal .6s var(--spring) .85s both}
.title{font:700 30px/1.08 var(--display);letter-spacing:-.04em;margin:0;text-wrap:balance;animation:rise .7s var(--ease) .15s both}
.lede{color:var(--ink2);font-size:16px;margin:10px 0 30px;animation:rise .7s var(--ease) .22s both}
.step{width:100%;display:flex;flex-direction:column;align-items:center;gap:14px}
.foot{color:var(--ink2);font-size:12.5px;margin:26px 0 0;opacity:.7}
/* coins: one per agent. Tap one and it becomes the orb's right half */
.coins{display:flex;flex-wrap:wrap;justify-content:center;gap:16px 6px;margin:0 -14px}
.coin{position:relative;display:flex;flex-direction:column;align-items:center;gap:8px;width:70px;cursor:pointer}
.coin input{position:absolute;opacity:0;pointer-events:none}
.disc{width:58px;height:58px;border-radius:50%;background:var(--card);display:grid;place-items:center;
 box-shadow:0 0 0 .5px rgba(24,23,29,.1),0 6px 16px -8px rgba(24,23,29,.3);transition:transform .45s var(--spring),box-shadow .3s var(--ease)}
.disc img{width:28px;height:28px}.disc b{font:650 22px/1 var(--display);color:var(--ink2)}
.disc.new{background:transparent;box-shadow:none;color:var(--accent)}.disc.new svg{width:58px;height:58px}
.coin .nm{font-size:12.5px;color:var(--ink2);max-width:70px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:color .2s}
.coin.on .disc{transform:translateY(-3px) scale(1.06);box-shadow:0 0 0 3px var(--bg),0 0 0 5.5px var(--accent),0 12px 22px -10px var(--accent)}
.coin.on .nm{color:var(--ink);font-weight:600}.coin.tap .disc{animation:nudge .45s var(--spring)}
.coin:has(input:focus-visible) .disc{box-shadow:0 0 0 3px var(--bg),0 0 0 5.5px var(--ring,var(--accent))}
.newname{display:grid;grid-template-rows:0fr;width:100%;transition:grid-template-rows .45s var(--glide),margin .45s var(--glide)}
.newname>div{overflow:hidden;display:flex;justify-content:center;padding:6px 8px 4px}.newname.open{grid-template-rows:1fr;margin-top:2px}
.newname:not(.open)>div{padding-block:0;visibility:hidden}
.warn{display:none;margin:2px 0 0;padding:10px 16px;border-radius:18px;background:var(--accent-soft);color:var(--ink);font-size:13.5px;line-height:1.4;max-width:34ch}
.warn.on{display:block;animation:rise .4s var(--ease) both}
@media (max-width:440px){main{padding-top:max(26px,env(safe-area-inset-top))}.orb{width:132px;height:132px;gap:5px;margin-bottom:20px}
 .title{font-size:26px}.coins{gap:12px 4px}.coin{width:64px}.coin .nm{max-width:64px;font-size:12px}
 .half img{width:40px;height:40px}.face svg{width:42px;height:42px;margin-right:14px}
 .half.l img,.half.l b{margin-left:4px}.half.r img,.half.r b,.half.r .mk{margin-right:4px}.half b{font-size:34px}}
@media (max-height:700px){main{padding-top:max(16px,env(safe-area-inset-top))}.orb{width:104px;height:104px;margin-bottom:14px}
 .title{font-size:23px}.step{gap:10px}.coin .disc{transform:scale(.88)}.coins{gap:6px 2px}
 .half img{width:32px;height:32px}.face svg{width:34px;height:34px;margin-right:10px}
 .half.l img,.half.l b{margin-left:2px}.half.r img,.half.r b,.half.r .mk{margin-right:2px}.half b{font-size:28px}}
.btn{appearance:none;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;height:56px;padding:0 22px;border:0;border-radius:999px;
 background:var(--accent);color:var(--on-accent);font:600 17px var(--font);letter-spacing:-.015em;cursor:pointer;margin-top:10px;
 box-shadow:0 1px 0 rgba(255,255,255,.22) inset,0 12px 26px -12px var(--accent);transition:transform .45s var(--spring),background-color .2s,opacity .2s}
.btn:hover{background:var(--accent-hover)}.btn:active{transform:scale(.96);transition:transform .12s var(--ease)}.btn:disabled{opacity:.5}
.btn.soft{background:var(--fill);color:var(--ink);box-shadow:none;margin-top:0}
.btn svg{width:22px;height:22px}
.tl{appearance:none;background:none;border:0;color:var(--ink2);font:500 14.5px var(--font);cursor:pointer;padding:6px 8px;border-radius:8px}
.tl.accent{color:var(--accent)}.tl:active{opacity:.5}
.pill{width:220px;height:46px;border:0;border-radius:999px;text-align:center;background:var(--card);color:var(--ink);font:600 16px var(--font);
 box-shadow:0 0 0 1.5px var(--accent),0 8px 20px -12px var(--accent)}
.pill:focus{outline:none;box-shadow:0 0 0 2px var(--accent),0 0 0 6px var(--glow)}
input.field{width:100%;height:56px;border:0;border-radius:999px;padding:0 22px;text-align:center;background:var(--card);color:var(--ink);font:17px var(--font);
 box-shadow:0 0 0 .5px rgba(24,23,29,.12),0 6px 16px -10px rgba(24,23,29,.25)}
input.field:focus{outline:none;box-shadow:0 0 0 2px var(--accent),0 0 0 6px var(--glow)}
.me{font-size:13px;color:var(--ink2);display:flex;gap:4px;align-items:center;justify-content:center;margin-top:2px}.me b{color:var(--ink);font-weight:600}
.me .tl{font-size:13px;padding:2px 4px;color:var(--accent)}
.keep{display:flex;gap:8px;align-items:center;font-size:13.5px;color:var(--ink2)}.keep input{accent-color:var(--accent);width:17px;height:17px;margin:0}
.err{color:var(--red);font-size:14px;margin:0}.err:empty{display:none}
.sub{color:var(--ink2);font-size:14px;margin:-4px 0 4px}
@media (prefers-reduced-motion:reduce){*,*:before{animation-duration:.01ms!important;animation-delay:0s!important;transition-duration:.01ms!important}}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
""".replace("GRAIN_URL", GRAIN)

PAGE_JS = r"""
const $=(t,a={},...k)=>{const e=document.createElement(t);for(const[n,v]of Object.entries(a)){if(n==='on')for(const[ev,f]of Object.entries(v))e.addEventListener(ev,f);else if(n==='text')e.textContent=v;else if(n==='html')e.innerHTML=v;else e.setAttribute(n,v)}e.append(...k.filter(x=>x!=null));return e};
const step=document.getElementById('step'),rhalf=document.getElementById('rhalf'),orb=document.getElementById('orb');
const show=(...k)=>{const kids=k.filter(x=>x!=null);kids.forEach((el,i)=>{el.classList.add('rise');el.style.setProperty('--i',i)});step.replaceChildren(...kids)};
const store={get(){try{return localStorage.getItem('relay_key')||sessionStorage.getItem('relay_key')}catch{return null}},
 set(k,keep){try{(keep?localStorage:sessionStorage).setItem('relay_key',k)}catch{}},clear(){try{localStorage.removeItem('relay_key');sessionStorage.removeItem('relay_key')}catch{}}};
let cred=store.get(),viaEmail=false;
async function api(m,p,b,a){const h={'Content-Type':'application/json'};if(a)h.Authorization='Bearer '+a;
 const r=await fetch(p,{method:m,headers:h,body:b===undefined?undefined:JSON.stringify(b)});const d=await r.json().catch(()=>({}));
 if(!r.ok){const e=new Error(d.detail||('error '+r.status));e.status=r.status;throw e}return d}
const enc=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,'-').replace(/\//g,'_').replace(/=+$/,'');
const dec=s=>Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
const canPasskey=!!(window.PublicKeyCredential&&navigator.credentials);
const G={print:'<svg viewBox="0 0 26 26" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M6.5 15.5a6.5 6.5 0 0 1 13 0"/><path d="M9.3 17.5v-2a3.7 3.7 0 0 1 7.4 0v3.5"/><path d="M13 15.3v6"/><path d="M4 11a9.6 9.6 0 0 1 18 0" opacity=".55"/></svg>',
 add:'<svg viewBox="0 0 58 58" fill="none"><path d="M27 8a21 21 0 0 0 0 42" stroke="currentColor" stroke-width="1.6" stroke-dasharray="3 3.4"/><path d="M31 8a21 21 0 0 1 0 42z" fill="currentColor" opacity=".14"/><path d="M36 29h-7M32.5 25.5v7" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>',
 tick:'<svg viewBox="0 0 30 30" fill="none"><path d="M8 15.5l4.6 4.6L22.5 10" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>'};
const LOGOS=['claude','codex','gemini','copilot','muse','openai','grok','cursor'];
const mark=(p,name)=>LOGOS.includes(p)?$('img',{src:'/logos/'+p+'.svg',alt:''}):$('b',{text:(name||'?')[0].toUpperCase()});
function right(kind,p,name){rhalf.className='half r '+kind;rhalf.replaceChildren(kind==='new'?$('span',{class:'face',html:G.add}):$('span',{class:'face'},mark(p,name)))}
function keep(on){return $('label',{class:'keep'},$('input',{type:'checkbox',id:'keep',...(on?{checked:''}:{})}),'Keep me signed in')}
function signedIn(tok,stay){cred=tok;const k=document.getElementById('keep');store.set(tok,k?k.checked:stay!==false);consent()}
function signIn(msg){const err=$('p',{class:'err',role:'alert',text:msg||''});const parts=[];
 if(canPasskey)parts.push($('button',{class:'btn',html:G.print+'<span>Sign in with a passkey</span>',on:{click:()=>passkey(err)}}));
 if(BOOT.email){const em=$('input',{class:'field',type:'email',id:'email',placeholder:'Email',autocomplete:'email','aria-label':'Email'});
  parts.push(em,$('button',{class:canPasskey?'btn soft':'btn',on:{click:()=>emailCode(em.value.trim(),err)}},'Email me a code'))}
 show(...parts,err,$('button',{class:'tl',on:{click:keyForm}},'Use a Relay key instead'))}
async function passkey(err){try{const o=await api('POST','/auth/passkey/options',{});
  const c=await navigator.credentials.get({publicKey:{challenge:dec(o.challenge),rpId:o.rpId,userVerification:o.userVerification,timeout:o.timeout,allowCredentials:[]}});
  const r=c.response;const s=await api('POST','/auth/passkey/signin',{id:c.id,rawId:enc(c.rawId),type:c.type,response:{clientDataJSON:enc(r.clientDataJSON),authenticatorData:enc(r.authenticatorData),signature:enc(r.signature),userHandle:r.userHandle?enc(r.userHandle):null}});
  signedIn(s.token,true)}catch(e){err.textContent=e.name==='NotAllowedError'?'No Relay passkey on this device yet.':e.message}}
async function emailCode(email,err){if(!email.includes('@')){err.textContent='Type your email first.';return}
 try{await api('POST','/auth/email',{email})}catch(e){err.textContent=e.message;return}
 const code=$('input',{class:'field',type:'text',id:'code',inputmode:'numeric',autocomplete:'one-time-code',placeholder:'Code from the email','aria-label':'Code from the email'});const e2=$('p',{class:'err',role:'alert'});
 show($('p',{class:'sub',text:'Sent to '+email}),code,$('button',{class:'btn',on:{click:async()=>{try{const s=await api('POST','/auth/verify',{email,code:code.value});viaEmail=true;signedIn(s.token,true)}catch(x){e2.textContent=x.message}}}},'Sign in'),e2,
  $('button',{class:'tl',on:{click:()=>signIn()}},'Back'));code.focus()}
function keyForm(){const k=$('input',{class:'field',type:'password',id:'key',placeholder:'Relay key','aria-label':'Your Relay key',autocomplete:'current-password'});
 show(k,$('button',{class:'btn',on:{click:()=>{if(k.value.trim())signedIn(k.value.trim())}}},'Continue'),keep(false),$('button',{class:'tl',on:{click:()=>signIn()}},'Back'));k.focus()}
async function consent(){let d;try{d=await api('GET','/oauth/request/'+BOOT.request,undefined,cred)}catch(e){
  if(e.status===401||e.status===403){store.clear();cred=null;signIn(e.status===403?e.message:'')}else show($('p',{class:'err',text:e.message}));return}
 const nn=$('input',{class:'pill',type:'text',id:'newname',value:d.suggest,'aria-label':'Name for the new agent',autocapitalize:'off',spellcheck:'false'});
 const box=$('div',{class:'newname'},$('div',{},nn));
 const coin=(value,on,disc,name)=>$('label',{class:'coin'+(on?' on':''),title:name},$('input',{type:'radio',name:'who',value,'aria-label':name,...(on?{checked:''}:{})}),disc,$('span',{class:'nm',text:name}));
 const coins=d.agents.map(a=>coin(a.handle,a.handle===d.preselect,$('span',{class:'disc'},mark(a.provider,a.handle)),a.handle));
 coins.push(coin('__new__',!d.preselect,$('span',{class:'disc new',html:G.add}),'New'));
 const group=$('div',{class:'coins',role:'radiogroup','aria-label':'Allow '+BOOT.app+' to use Relay as'},...coins);
 const pick=()=>{const c=coins.find(x=>x.querySelector('input').checked);coins.forEach(x=>x.classList.toggle('on',x===c));
  const v=c.querySelector('input').value,a=d.agents.find(x=>x.handle===v);box.classList.toggle('open',v==='__new__');
  if(v==='__new__')right('new');else right('agent',a.provider,a.handle);
  // picking another app's agent makes this app speak as it: say so, and how to reach it instead
  const other=a&&a.provider&&d.provider&&a.provider!==d.provider;
  warn.textContent=other?d.app_label+' will send and read messages as '+a.handle+'. To talk to '+a.handle+', pick '+((d.agents.find(x=>x.provider===d.provider)||{}).handle||'New')+', then ask '+a.handle+' from '+d.app_label+'.':'';
  warn.classList.toggle('on',!!other)};
 group.addEventListener('change',e=>{const c=e.target.closest('.coin');c.classList.remove('tap');void c.offsetWidth;c.classList.add('tap');pick()});
 const err=$('p',{class:'err',role:'alert'});
 const warn=$('p',{class:'warn',role:'status'});
 const me=$('div',{class:'me'},$('span',{html:'<b></b>'}),$('span',{text:'·'}),$('button',{class:'tl',on:{click:()=>{store.clear();cred=null;right('','');rhalf.replaceChildren($('i',{class:'mk'}));signIn()}}},'Switch'));
 me.querySelector('b').textContent=d.you;
 show(group,box,warn,err,$('button',{class:'btn',id:'allow',on:{click:()=>allow(nn,err)}},'Allow'),me,$('button',{class:'tl',on:{click:deny}},'Cancel'));pick()}
async function allow(nn,err){const v=(document.querySelector('input[name=who]:checked')||{}).value;
 const body=v==='__new__'?{request:BOOT.request,new_agent:nn.value.trim()}:{request:BOOT.request,agent:v};
 const b=document.getElementById('allow');b.disabled=true;err.textContent='';
 try{const r=await api('POST','/oauth/decide',body,cred);await offerPasskey();
  document.getElementById('seal').innerHTML=G.tick;orb.classList.add('fused');
  document.getElementById('title').textContent='Connected';document.getElementById('lede').textContent=BOOT.app+' is '+r.agent+' on Relay.';
  step.replaceChildren();document.getElementById('foot').textContent='Back to '+BOOT.host+'…';setTimeout(()=>{location.href=r.redirect},1700)}
 catch(e){err.textContent=e.message;b.disabled=false}}
async function deny(){try{const r=await api('POST','/oauth/deny',{request:BOOT.request});location.href=r.redirect}catch(e){show($('p',{class:'err',text:e.message}))}}
async function offerPasskey(){if(!viaEmail||!canPasskey)return;
 return new Promise(done=>{const err=$('p',{class:'err',role:'alert'});
  show($('p',{class:'sub',text:'Skip the email next time?'}),
   $('button',{class:'btn',html:G.print+'<span>Add a passkey</span>',on:{click:async()=>{try{const o=await api('POST','/auth/passkey/register/options',{},cred);
     const c=await navigator.credentials.create({publicKey:{...o,challenge:dec(o.challenge),user:{...o.user,id:dec(o.user.id)},excludeCredentials:o.excludeCredentials.map(x=>({...x,id:dec(x.id)}))}});
     await api('POST','/auth/passkey/register',{id:c.id,rawId:enc(c.rawId),type:c.type,label:navigator.platform||'passkey',response:{clientDataJSON:enc(c.response.clientDataJSON),attestationObject:enc(c.response.attestationObject)}},cred);done()}
    catch(e){err.textContent=e.name==='NotAllowedError'?'Skipped.':e.message;setTimeout(done,900)}}}}),
   $('button',{class:'tl',on:{click:()=>done()}},'Not now'),err)})}
if(cred)consent();else signIn();
"""
