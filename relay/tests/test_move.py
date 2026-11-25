"""Moving the hub to a new address (RELAY_PUBLIC_URL): people follow, apps keep working on the old one."""
import json

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"
NEW = "https://agentrelayhub.example"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def moved(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_PUBLIC_URL", NEW)
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))      # requests come in on the old address
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    return c, owner


def test_pages_a_person_opens_go_to_the_new_address(moved):
    c, _ = moved
    for path in ("/", "/join", "/welcome", "/up/abc123", "/m/abc.123.xyz?dl=1"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 301 and r.headers["location"] == NEW + path, path
    assert c.get("/app", follow_redirects=False).status_code == 200          # the app itself hands people over


def test_apps_keep_working_on_the_old_address_and_sign_in_there(moved):
    c, _ = moved
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r.status_code == 401 and "http://testserver/.well-known/oauth-protected-resource" in r.headers["www-authenticate"]
    meta = c.get("/.well-known/oauth-protected-resource").json()
    assert meta["resource"] == "http://testserver/mcp"                        # the address the app actually uses
    assert c.get("/.well-known/oauth-authorization-server").json()["issuer"] == "http://testserver"
    assert c.get("/health").status_code == 200 and c.get("/agents", follow_redirects=False).status_code == 401


def test_passkeys_are_asked_for_the_address_the_browser_is_on(moved):
    c, owner = moved
    opts = c.post("/auth/passkey/register/options", headers=H(owner)).json()
    assert opts["rp"]["id"] == "testserver"
    assert c.post("/auth/passkey/options").json()["rpId"] == "testserver"


def test_a_signed_in_person_is_handed_over_once(moved):
    c, owner = moved
    assert c.get("/auth/config").json()["home"] == NEW
    code = c.post("/auth/handoff", headers=H(owner)).json()["code"]
    s = c.post("/auth/handoff/claim", json={"code": code}).json()
    assert s["handle"] == "kobby" and s["token"].startswith("rly_sess_")
    assert c.get("/agents/me", headers=H(s["token"])).json()["handle"] == "kobby"
    assert c.post("/auth/handoff/claim", json={"code": code}).status_code == 400     # once only
    assert c.post("/auth/handoff/claim", json={"code": "ho_madeup"}).status_code == 400
    agent = c.post("/agents", headers=H(owner), json={"handle": "claude"}).json()["token"]
    assert c.post("/auth/handoff", headers=H(agent)).status_code == 403             # people only


def test_a_hub_that_hasnt_moved_redirects_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_PUBLIC_URL", raising=False)
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/auth/config").json()["home"] is None
