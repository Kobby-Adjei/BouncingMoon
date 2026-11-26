"""Told when something breaks: a website turning a saved key away, a backup that didn't happen, the disk
filling up. Each person sees their own; the owner also sees the hub's. A problem taps you once a day."""
import socket

import pytest
from fastapi.testclient import TestClient

from relay import media as M
from relay import vault as V
from relay.hub import create_app
from relay.tests.test_push import ADMIN, H, Phone, PushService, on, sent


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "WAIT_SECONDS", 0.2)
    monkeypatch.setenv("RELAY_LEGAL_EMAIL", "hello@relay.example")
    svc, site = PushService(), {"status": 200}
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, push_opener=svc, caption_threads=False, media_threads=False,
                     vault_send=lambda *a, **k: (site["status"], {}, b'{"error":"bad token"}'),
                     vault_resolve=lambda host: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    inv = c.post("/invites", headers=H(kobby), json={"handle": "ama", "role": "human"}).json()
    ama = c.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    return c, app, svc, site, {"kobby": kobby, "claude": claude, "ama": ama}


def use_key(c, tok):
    return c.post("/mcp", headers=H(tok), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "call_api", "arguments": {"key": "instagram", "method": "GET", "url": "https://graph.facebook.com/v21.0/me",
                                          "why": "Check the account"}}}).json()


def checks(c, tok):
    return {x["id"]: x for x in c.get("/me/health", headers=H(tok)).json()["checks"]}


def test_a_key_turned_away_taps_its_owner_at_once_and_once(hub):
    c, app, svc, site, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    key = c.post("/keys", headers=H(t["kobby"]), json={"name": "instagram", "hosts": ["graph.facebook.com"], "secret": "IGQV-x"}).json()
    use_key(c, t["claude"])
    assert checks(c, t["kobby"])["keys"]["ok"]
    site["status"] = 401
    use_key(c, t["claude"])
    bad = checks(c, t["kobby"])[f"key:{key['id']}"]
    assert not bad["ok"] and bad["title"] == "Instagram turned away your instagram key" and bad["url"] == f"me/keys/{key['id']}"
    got = [phone.read(s["body"]) for s in sent(app, svc)]
    assert [g["title"] for g in got] == ["Instagram turned away your instagram key"]
    assert got[0]["url"] == f"/app#me/keys/{key['id']}"
    use_key(c, t["claude"])                                      # still refused: no second tap the same day
    assert len(sent(app, svc)) == 1
    site["status"] = 200
    use_key(c, t["claude"])                                      # a new key works: fixed
    assert checks(c, t["kobby"])["keys"]["ok"]
    assert "IGQV" not in str(c.get("/me/health", headers=H(t["kobby"])).json())


def test_the_owner_sees_the_hubs_health_and_others_only_their_own(hub, monkeypatch):
    c, app, svc, site, t = hub
    mine = checks(c, t["kobby"])
    assert mine["storage"]["ok"] and mine["backup"]["ok"]
    assert checks(c, t["ama"]) == {}                              # no keys, not the owner: nothing to show
    assert c.get("/me/health", headers=H(t["claude"])).status_code == 403
    monkeypatch.setattr(M, "disk", lambda path: (5, 100))         # 95% full
    phone = Phone()
    on(c, t["kobby"], phone)
    assert not checks(c, t["kobby"])["storage"]["ok"]
    assert app.state.alert_health() == 1
    assert app.state.alert_health() == 0                          # once a day
    [got] = [phone.read(s["body"]) for s in sent(app, svc)]
    assert got["title"] == "Storage is 95% full" and got["url"] == "/app#me/health"
    monkeypatch.setattr(M, "disk", lambda path: (60, 100))
    app.state.alert_health()
    with app.state.store.ro() as db:                              # fixed: forgotten, so the next break taps again
        assert db.execute("SELECT COUNT(*) FROM prefs WHERE key LIKE 'alert:%'").fetchone()[0] == 0
