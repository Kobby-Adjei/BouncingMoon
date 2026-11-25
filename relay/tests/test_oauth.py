"""Connecting an app like Gmail: discovery, register, sign in, Allow, tokens; passkeys.
Includes the official MCP client doing the same OAuth handshake Claude and ChatGPT do."""
import asyncio
import base64
import hashlib
import json
import os
import re
import socket
import struct
import threading
import time
import urllib.parse

import pytest
import uvicorn
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from relay import oauth as O
from relay import webauthn as W
from relay.hub import create_app

ADMIN = "oauth-admin"
CB = "https://claude.ai/api/mcp/auth_callback"


@pytest.fixture
def hub(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False))
    owner = c.post("/owner", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "kobby"}).json()["token"]
    return c, owner


def pkce():
    v = base64.urlsafe_b64encode(os.urandom(40)).rstrip(b"=").decode()
    return v, base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


def register(c, uris=(CB,), name="Claude"):
    r = c.post("/oauth/register", json={"client_name": name, "redirect_uris": list(uris)})
    assert r.status_code == 201, r.text
    return r.json()["client_id"]


def start(c, cid, challenge, redirect=CB, state="st-1"):
    q = urllib.parse.urlencode({"response_type": "code", "client_id": cid, "redirect_uri": redirect, "state": state,
                                "code_challenge": challenge, "code_challenge_method": "S256", "scope": "relay"})
    return c.get("/oauth/authorize?" + q, follow_redirects=False)


def rid_of(page):
    return json.loads(re.search(r"const BOOT=(\{.*?\});", page.text).group(1))["request"]


def connect(c, who, agent=None, new_agent=None, name="Claude"):
    """The whole dance: register, the Allow page, the code, the tokens. Returns (tokens, client_id)."""
    cid = register(c, name=name)
    v, ch = pkce()
    rid = rid_of(start(c, cid, ch))
    body = {"request": rid, **({"new_agent": new_agent} if new_agent else {"agent": agent})}
    d = c.post("/oauth/decide", headers={"Authorization": f"Bearer {who}"}, json=body)
    assert d.status_code == 200, d.text
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(d.json()["redirect"]).query))
    assert q["state"] == "st-1" and q["iss"] == "http://testserver"
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": q["code"], "code_verifier": v,
                                       "client_id": cid, "redirect_uri": CB})
    assert tok.status_code == 200, tok.text
    return tok.json(), cid


def mcp(c, token, name="contacts"):
    return c.post("/mcp", headers={"Authorization": f"Bearer {token}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": {}}})


# ------------------------------------------------------------------ discovery
def test_an_app_finds_where_to_sign_in_by_itself(hub):
    c, _ = hub
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code == 401
    assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource"' in r.headers["www-authenticate"]
    prm = c.get("/.well-known/oauth-protected-resource").json()
    assert prm["resource"] == "http://testserver/mcp" and prm["authorization_servers"] == ["http://testserver"]
    meta = c.get("/.well-known/oauth-authorization-server").json()
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["registration_endpoint"].endswith("/oauth/register")


# ------------------------------------------------------------------ the whole flow
def test_paste_url_sign_in_allow_and_the_app_is_connected_as_a_new_agent(hub):
    c, owner = hub
    tokens, _ = connect(c, owner, new_agent="claude")
    assert tokens["token_type"] == "Bearer" and tokens["access_token"].startswith("rly_oat_")
    res = mcp(c, tokens["access_token"]).json()["result"]
    assert "You are claude on this Relay hub." in res["content"][0]["text"]
    me = c.get("/agents/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}).json()
    assert me["handle"] == "claude" and me["role"] == "agent"
    agent = next(a for a in c.get("/agents", headers={"Authorization": f"Bearer {ADMIN}"}).json() if a["handle"] == "claude")
    assert agent["provider"] == "claude"                                       # known from the app's name


def test_an_app_that_just_connected_shows_as_here_not_as_not_connected_yet(hub):
    c, owner = hub
    connect(c, owner, new_agent="claude")
    agent = next(a for a in c.get("/agents", headers={"Authorization": f"Bearer {owner}"}).json() if a["handle"] == "claude")
    assert agent["last_seen"] and time.time() - agent["last_seen"] < 30


def test_the_allow_page_is_safe_and_mobile_ready(hub):
    c, _ = hub
    cid = register(c)
    page = start(c, cid, pkce()[1])
    assert page.status_code == 200 and "Connect Claude to Relay" in page.text and "claude.ai" in page.text
    csp = page.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "nonce-" in csp and "width=device-width" in page.text


def test_nothing_is_ever_sent_to_an_address_the_app_did_not_register(hub):
    c, _ = hub
    cid = register(c)
    bad = start(c, cid, pkce()[1], redirect="https://evil.example/cb")
    assert bad.status_code == 400 and "location" not in bad.headers
    assert start(c, "rc_nope", pkce()[1]).status_code == 400
    plain = c.get("/oauth/authorize?" + urllib.parse.urlencode({"response_type": "code", "client_id": cid,
                  "redirect_uri": CB, "code_challenge": "x" * 43, "code_challenge_method": "plain"}), follow_redirects=False)
    assert plain.status_code == 302 and "error=invalid_request" in plain.headers["location"]   # PKCE S256 required
    assert c.post("/oauth/register", json={"redirect_uris": ["javascript:alert(1)"]}).status_code == 400
    assert c.post("/oauth/register", json={"redirect_uris": ["http://evil.example/cb"]}).status_code == 400


def test_desktop_apps_on_localhost_may_come_back_on_any_port(hub):
    c, owner = hub
    cid = register(c, uris=("http://127.0.0.1/callback",))
    v, ch = pkce()
    assert start(c, cid, ch, redirect="http://127.0.0.1:53111/callback").status_code == 200


def test_a_wrong_verifier_or_a_reused_code_gets_nothing(hub):
    c, owner = hub
    cid = register(c)
    v, ch = pkce()
    rid = rid_of(start(c, cid, ch))
    d = c.post("/oauth/decide", headers={"Authorization": f"Bearer {owner}"}, json={"request": rid, "new_agent": "claude"})
    code = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(d.json()["redirect"]).query))["code"]
    wrong = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": "x" * 50, "client_id": cid})
    assert wrong.json()["error"] == "invalid_grant"
    good = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": v, "client_id": cid})
    assert good.status_code == 200
    again = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": code, "code_verifier": v, "client_id": cid})
    assert again.json()["error"] == "invalid_grant"
    assert mcp(c, good.json()["access_token"]).status_code == 401            # a copied code ends what it gave
    assert c.post("/oauth/decide", headers={"Authorization": f"Bearer {owner}"},
                  json={"request": rid, "new_agent": "x2"}).status_code == 410  # the page was used up


def test_a_retried_refresh_keeps_the_person_signed_in(hub):
    c, owner = hub
    tokens, cid = connect(c, owner, new_agent="claude")
    body = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid}
    first = c.post("/oauth/token", data=body).json()
    retry = c.post("/oauth/token", data=body).json()         # the app retried, or two requests raced
    assert "access_token" in retry and retry["access_token"] != first["access_token"]
    assert mcp(c, first["access_token"]).status_code == 200 and mcp(c, retry["access_token"]).status_code == 200
    conns = c.get("/oauth/connections", headers={"Authorization": f"Bearer {owner}"}).json()
    c.delete(f"/oauth/connections/{conns[0]['id']}", headers={"Authorization": f"Bearer {owner}"})
    assert c.post("/oauth/token", data=body).json()["error"] == "invalid_grant"   # ended by the person: no grace


def test_tokens_renew_and_a_copied_refresh_token_ends_the_connection(hub, monkeypatch):
    monkeypatch.setattr(O, "REFRESH_GRACE", 0)               # long after the swap: a copy, not a retry
    c, owner = hub
    tokens, cid = connect(c, owner, new_agent="claude")
    new = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid}).json()
    assert new["access_token"] != tokens["access_token"] and mcp(c, new["access_token"]).status_code == 200
    stolen = c.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": cid})
    assert stolen.json()["error"] == "invalid_grant"
    assert mcp(c, new["access_token"]).status_code == 401                    # the whole connection ended


def test_people_connect_only_their_own_agents_and_can_end_connections(hub):
    c, owner = hub
    connect(c, owner, new_agent="claude")
    c.post("/people", headers={"Authorization": f"Bearer {owner}"}, json={"handle": "ama", "email": "ama@example.com"})
    ama = c.post("/owner", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "ama"})   # a key for the test
    ama_key = ama.json()["token"]
    c.post("/owner", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "kobby"})   # (owner stays kobby)
    import sqlite3
    db = sqlite3.connect(c.app.state.store.db_path)
    db.execute("UPDATE agents SET owner=0 WHERE handle='ama'"); db.commit(); db.close()
    cid = register(c)
    rid = rid_of(start(c, cid, pkce()[1]))
    info = c.get(f"/oauth/request/{rid}", headers={"Authorization": f"Bearer {ama_key}"}).json()
    assert info["agents"] == [] and info["suggest"] == "claude-2"            # kobby's claude isn't hers
    assert c.post("/oauth/decide", headers={"Authorization": f"Bearer {ama_key}"},
                  json={"request": rid, "agent": "claude"}).status_code == 403
    tokens, _ = connect(c, ama_key, new_agent="ama-claude")
    conns = c.get("/oauth/connections", headers={"Authorization": f"Bearer {ama_key}"}).json()
    assert [x["agent"] for x in conns] == ["ama-claude"] and conns[0]["app"] == "Claude"
    c.delete(f"/oauth/connections/{conns[0]['id']}", headers={"Authorization": f"Bearer {ama_key}"})
    assert mcp(c, tokens["access_token"]).status_code == 401
    agent_key = c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "bot"}).json()["token"]
    assert c.get(f"/oauth/request/{rid_of(start(c, cid, pkce()[1]))}",
                 headers={"Authorization": f"Bearer {agent_key}"}).status_code == 403   # agents can't Allow


def test_old_keys_keep_working(hub):
    c, _ = hub
    key = c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "muse"}).json()["token"]
    assert mcp(c, key).status_code == 200


# ------------------------------------------------------------------ passkeys
class Authenticator:
    """A software passkey: what a phone's secure chip does, in a few lines."""
    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.cred = os.urandom(16)
        self.count = 0

    @staticmethod
    def cbor(v):
        def hdr(m, a):
            return bytes([m << 5 | a]) if a < 24 else bytes([m << 5 | 24, a]) if a < 256 else bytes([m << 5 | 25]) + a.to_bytes(2, "big")
        if isinstance(v, int):
            return hdr(0, v) if v >= 0 else hdr(1, -1 - v)
        if isinstance(v, bytes):
            return hdr(2, len(v)) + v
        if isinstance(v, str):
            return hdr(3, len(v.encode())) + v.encode()
        return hdr(5, len(v)) + b"".join(Authenticator.cbor(k) + Authenticator.cbor(x) for k, x in v.items())

    def create(self, opts, origin):
        n = self.key.public_key().public_numbers()
        cose = self.cbor({1: 2, 3: -7, -1: 1, -2: n.x.to_bytes(32, "big"), -3: n.y.to_bytes(32, "big")})
        ad = hashlib.sha256(opts["rp"]["id"].encode()).digest() + bytes([0x45]) + struct.pack(">I", 0) + b"\0" * 16 \
            + struct.pack(">H", len(self.cred)) + self.cred + cose
        cd = json.dumps({"type": "webauthn.create", "challenge": opts["challenge"], "origin": origin}).encode()
        return {"id": W.b64u(self.cred), "response": {"clientDataJSON": W.b64u(cd),
                "attestationObject": W.b64u(self.cbor({"fmt": "none", "attStmt": {}, "authData": ad}))}}

    def get(self, opts, origin, user, flags=0x05):
        self.count += 1
        ad = hashlib.sha256(opts["rpId"].encode()).digest() + bytes([flags]) + struct.pack(">I", self.count)
        cd = json.dumps({"type": "webauthn.get", "challenge": opts["challenge"], "origin": origin}).encode()
        sig = self.key.sign(ad + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": W.b64u(self.cred), "response": {"clientDataJSON": W.b64u(cd), "authenticatorData": W.b64u(ad),
                "signature": W.b64u(sig), "userHandle": W.b64u(user.encode())}}


def test_a_passkey_signs_a_person_in_without_any_email(hub):
    c, owner = hub
    phone = Authenticator()
    H = {"Authorization": f"Bearer {owner}"}
    opts = c.post("/auth/passkey/register/options", headers=H).json()
    assert opts["rp"]["id"] == "testserver" and opts["authenticatorSelection"]["residentKey"] == "required"
    assert c.post("/auth/passkey/register", headers=H, json=phone.create(opts, "http://testserver")).json() == {"added": True}
    o = c.post("/auth/passkey/options").json()
    s = c.post("/auth/passkey/signin", json=phone.get(o, "http://testserver", "kobby")).json()
    assert s["handle"] == "kobby" and s["owner"] and s["token"].startswith("rly_sess_")
    tokens, _ = connect(c, s["token"], new_agent="claude")                  # passkey -> Allow -> connected
    assert mcp(c, tokens["access_token"]).status_code == 200
    o2 = c.post("/auth/passkey/options").json()
    assert c.post("/auth/passkey/signin", json=phone.get(o2, "https://evil.example", "kobby")).status_code == 401
    replay = phone.get(c.post("/auth/passkey/options").json(), "http://testserver", "kobby")
    assert c.post("/auth/passkey/signin", json=replay).status_code == 200
    assert c.post("/auth/passkey/signin", json=replay).status_code == 400  # a challenge works once
    agent_key = c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "bot"}).json()["token"]
    assert c.post("/auth/passkey/register/options", headers={"Authorization": f"Bearer {agent_key}"}).status_code == 403


# ------------------------------------------------------------------ the official MCP client, like Claude
def test_the_official_mcp_client_connects_through_oauth(tmp_path):
    from mcp import ClientSession
    from mcp.client.auth import OAuthClientProvider, TokenStorage
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared.auth import AuthorizationCodeResult, OAuthClientMetadata
    httpx = pytest.importorskip("httpx2")           # the HTTP library the MCP client is built on

    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"
    owner = httpx.post(base + "/owner", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "kobby"}).json()["token"]
    got = {}

    class Memory(TokenStorage):
        def __init__(self): self.t = self.c = None
        async def get_tokens(self): return self.t
        async def set_tokens(self, t): self.t = t
        async def get_client_info(self): return self.c
        async def set_client_info(self, c): self.c = c

    async def person_presses_allow(url):          # what a person does in the browser window
        async with httpx.AsyncClient() as h:
            page = await h.get(url)
            rid = json.loads(re.search(r"const BOOT=(\{.*?\});", page.text).group(1))["request"]
            d = await h.post(base + "/oauth/decide", headers={"Authorization": f"Bearer {owner}"},
                             json={"request": rid, "new_agent": "claude"})
        got["q"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(d.json()["redirect"]).query))

    async def back_in_the_app():
        return AuthorizationCodeResult(code=got["q"]["code"], state=got["q"].get("state"), iss=got["q"].get("iss"))

    async def run():
        auth = OAuthClientProvider(base + "/mcp", OAuthClientMetadata(client_name="Claude", redirect_uris=["http://localhost:3030/callback"]),
                                   Memory(), redirect_handler=person_presses_allow, callback_handler=back_in_the_app)
        async with httpx.AsyncClient(auth=auth, timeout=30) as h:
            async with streamable_http_client(base + "/mcp", http_client=h) as (r, w, *_):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = {t.name for t in (await session.list_tools()).tools}
                    res = await session.call_tool("contacts", {})
                    return tools, res.content[0].text

    tools, text = asyncio.run(run())
    server.should_exit = True
    assert {"ask", "reply", "get_files"} <= tools and "You are claude on this Relay hub." in text


def test_claiming_the_hub_then_a_passkey_means_no_key_ever_again(hub):
    """The web app's first run: the admin key once makes the owner account; then a session and a passkey."""
    c, _ = hub
    r = c.post("/owner", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": "kobby"}).json()
    s = c.post("/auth/session/owner", headers={"Authorization": f"Bearer {r['token']}"}).json()
    assert s["token"].startswith("rly_sess_") and s["owner"]
    phone = Authenticator()
    H = {"Authorization": f"Bearer {s['token']}"}
    c.post("/auth/passkey/register", headers=H, json=phone.create(c.post("/auth/passkey/register/options", headers=H).json(), "http://testserver"))
    back = c.post("/auth/passkey/signin", json=phone.get(c.post("/auth/passkey/options").json(), "http://testserver", "kobby")).json()
    assert back["handle"] == "kobby" and back["owner"]
    assert c.post("/auth/session/owner", headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 403   # people only


def test_an_app_cant_break_out_of_the_allow_page_with_its_name(hub):
    c, owner = hub
    evil = '</script><meta http-equiv=refresh content="0;url=//evil.example">'
    cid = register(c, name=evil)
    v, ch = pkce()
    page = start(c, cid, ch)
    assert page.status_code == 200
    body = page.text
    assert body.count("</script>") == 1                       # only the page's own script ends a script
    assert "<meta http-equiv=refresh" not in body and "evil.example\">" not in body
    boot = json.loads(re.search(r"const BOOT=(\{.*?\});", body).group(1))
    assert boot["app"] == evil                                 # the name itself is kept, as plain text


def test_a_passkey_must_check_who_it_is_and_the_last_one_cant_be_removed(hub):
    c, owner = hub
    phone, laptop = Authenticator(), Authenticator()
    H = {"Authorization": f"Bearer {owner}"}
    assert c.post("/auth/passkey/options").json()["userVerification"] == "required"
    for dev in (phone, laptop):
        opts = c.post("/auth/passkey/register/options", headers=H).json()
        assert opts["authenticatorSelection"]["userVerification"] == "required"
        c.post("/auth/passkey/register", headers=H, json={**dev.create(opts, "http://testserver"), "label": "iPhone"})
    # a tap with no Face ID, fingerprint or PIN behind it isn't enough
    o = c.post("/auth/passkey/options").json()
    assert c.post("/auth/passkey/signin", json=phone.get(o, "http://testserver", "kobby", flags=0x01)).status_code == 401
    keys = c.get("/auth/passkeys", headers=H).json()
    assert [k["name"] for k in keys] == ["iPhone", "iPhone"]
    assert c.delete(f"/auth/passkeys/{keys[0]['id']}", headers=H).status_code == 200
    # with no email sign-in on this hub, the last passkey is the only way back in: it stays
    last = c.delete(f"/auth/passkeys/{keys[1]['id']}", headers=H)
    assert last.status_code == 409 and "only way" in last.json()["detail"]
    assert len(c.get("/auth/passkeys", headers=H).json()) == 1
