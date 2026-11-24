"""Saved keys: agents use them without seeing them; changes wait for the person's OK."""
import json
import socket
import threading
import time

import pytest
from fastapi.testclient import TestClient

from relay import vault as V
from relay.hub import create_app

ADMIN = "admin-test-token"
SECRET = "IGQVJ-super-secret-token-1234"


class FakeWeb:
    """Stands in for the websites: records each request, answers like an API that echoes what it got."""
    def __init__(self):
        self.calls, self.reply = [], None

    def __call__(self, method, url, headers, body):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.reply:
            return self.reply
        echo = {"ok": True, "you_sent": headers.get("Authorization") or url}     # a careless API echoing the key
        return 200, {"Content-Type": "application/json"}, json.dumps(echo).encode()


def public(host):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def H(tok, session=None):
    return {"Authorization": f"Bearer {tok}", **({"X-Relay-Session": session} if session else {})}


def call(c, tok, name, args=None, session="chat-1"):
    r = c.post("/mcp", headers=H(tok, session), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                      "params": {"name": name, "arguments": args or {}}})
    return r.json()["result"]


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "WAIT_SECONDS", 0.2)
    web = FakeWeb()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, vault_send=web, vault_resolve=public,
                     caption_threads=False, media_threads=False, caption_public_url="https://hub.example")
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    muse = c.post("/agents", headers=H(kobby), json={"handle": "muse"}).json()["token"]
    # ama: another person on the hub, with her own agent
    inv = c.post("/invites", headers=H(kobby), json={"handle": "ama", "role": "human"}).json()
    ama = c.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    ama_bot = c.post("/agents", headers=H(kobby), json={"handle": "ama-bot"}).json()["token"]
    with app.state.store.tx() as db:
        db.execute("UPDATE agents SET person='ama' WHERE handle='ama-bot'")
    return c, web, app, {"kobby": kobby, "claude": claude, "muse": muse, "ama": ama, "ama-bot": ama_bot}


def save(c, tok, **kw):
    body = {"name": "instagram", "hosts": ["graph.facebook.com"], "secret": SECRET, **kw}
    r = c.post("/keys", headers=H(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_saved_key_is_never_shown_again_and_is_stored_sealed(hub):
    c, web, app, t = hub
    k = save(c, t["kobby"])
    assert "secret" not in k and k["hint"] == "1234" and k["ask"] == "changes"
    listed = c.get("/keys", headers=H(t["kobby"])).json()
    assert SECRET not in json.dumps(listed)
    with app.state.store.ro() as db:
        dump = "\n".join(str(tuple(r)) for r in db.execute("SELECT * FROM vault_keys"))
    assert SECRET not in dump and "super-secret" not in dump
    # agents can't manage keys; nobody else sees yours
    assert c.post("/keys", headers=H(t["claude"]), json={"name": "x", "hosts": ["a.com"], "secret": "abcd"}).status_code == 403
    assert c.get("/keys", headers=H(t["ama"])).json() == []


def test_a_read_goes_straight_through_with_the_key_put_in_and_taken_back_out(hub):
    c, web, app, t = hub
    save(c, t["kobby"])
    keys = call(c, t["claude"], "saved_keys")
    assert keys["structuredContent"]["keys"] == [{"key": "instagram", "websites": ["graph.facebook.com"], "ask": "changes"}]
    res = call(c, t["claude"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/v21.0/me?fields=id"})
    assert not res["isError"], res
    sent = web.calls[-1]
    assert sent["headers"]["Authorization"] == f"Bearer {SECRET}" and sent["method"] == "GET"
    text = res["content"][0]["text"]
    assert "HTTP 200" in text and SECRET not in text and "[key hidden]" in text     # the echo is scrubbed


def test_the_key_goes_only_where_its_owner_said(hub):
    c, web, app, t = hub
    save(c, t["kobby"])
    bad = [("https://evil.example/steal", "only goes to graph.facebook.com"),
           ("http://graph.facebook.com/me", "plain https"),
           ("https://user:pw@graph.facebook.com/me", "plain https"),
           ("https://graph.facebook.com.evil.example/me", "only goes to")]
    for url, why in bad:
        res = call(c, t["claude"], "call_api", {"key": "instagram", "url": url})
        assert res["isError"] and why in res["content"][0]["text"], (url, res)
    res = call(c, t["claude"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me",
                                            "headers": {"Authorization": "Bearer mine"}})
    assert res["isError"] and "sets the key" in res["content"][0]["text"]
    assert web.calls == []
    # a name that points at a private address never gets the key
    app.state.vault.resolve = lambda host: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
    res = call(c, t["claude"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me"})
    assert "public address" in res["content"][0]["text"] and web.calls == []
    # redirects are answered, not followed
    app.state.vault.resolve = public
    web.reply = (302, {"Location": "https://elsewhere.example/?t=" + SECRET}, b"")
    res = call(c, t["claude"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me"})
    assert "doesn't follow redirects" in res["content"][0]["text"] and SECRET not in res["content"][0]["text"]


def test_only_the_owners_agents_may_use_a_key(hub):
    c, web, app, t = hub
    save(c, t["kobby"], agents=["claude"])
    res = call(c, t["muse"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me"})
    assert res["isError"] and "No saved key called 'instagram'" in res["content"][0]["text"]
    res = call(c, t["ama-bot"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me"})
    assert res["isError"]
    assert call(c, t["ama-bot"], "saved_keys")["structuredContent"]["keys"] == []
    assert web.calls == []


def test_a_post_waits_for_the_ok_and_then_runs_exactly_what_was_shown(hub):
    c, web, app, t = hub
    save(c, t["kobby"])
    body = {"image_url": "https://hub.example/m/x/original/ad.jpg", "caption": "New drop 🔥"}
    res = call(c, t["claude"], "call_api", {"key": "instagram", "method": "POST", "json": body, "why": "Post the ad",
                                            "url": "https://graph.facebook.com/v21.0/1784/media"})
    out = res["structuredContent"]
    assert not res["isError"] and out["status"] == "waiting" and web.calls == []           # nothing sent yet
    assert out["approval_url"] == f"https://hub.example/app#approve/{out['request']}"
    waiting = c.get("/approvals", headers=H(t["kobby"])).json()
    assert len(waiting) == 1 and waiting[0]["why"] == "Post the ad" and json.loads(waiting[0]["body"]) == body
    assert waiting[0]["agent"] == "claude" and waiting[0]["key"] == "instagram"
    # nobody else can answer it
    assert c.post(f"/approvals/{out['request']}/approve", headers=H(t["ama"])).status_code == 404
    assert c.post(f"/approvals/{out['request']}/approve", headers=H(t["claude"])).status_code == 403
    got = c.post(f"/approvals/{out['request']}/approve", headers=H(t["kobby"])).json()
    assert got["status"] == "done" and got["decided_by"] == "kobby" and got["result_status"] == 200
    assert json.loads(web.calls[0]["body"]) == body and web.calls[0]["method"] == "POST"
    # once only
    assert c.post(f"/approvals/{out['request']}/approve", headers=H(t["kobby"])).status_code == 409
    assert len(web.calls) == 1
    # the result comes back to the chat that asked
    other = call(c, t["claude"], "check_replies", session="chat-2")["structuredContent"]["messages"]
    mine = call(c, t["claude"], "check_replies", session="chat-1")["structuredContent"]["messages"]
    assert other == [] and any("approved by kobby: HTTP 200" in m["text"] and SECRET not in m["text"] for m in mine)


def test_a_no_sends_nothing_and_says_so(hub):
    c, web, app, t = hub
    save(c, t["kobby"])
    res = call(c, t["claude"], "call_api", {"key": "instagram", "method": "DELETE",
                                            "url": "https://graph.facebook.com/v21.0/123"})
    cid = res["structuredContent"]["request"]
    assert c.post(f"/approvals/{cid}/decline", headers=H(t["kobby"])).json()["status"] == "declined"
    assert web.calls == []
    msgs = call(c, t["claude"], "check_replies")["structuredContent"]["messages"]
    assert any("kobby said no" in m["text"] and "Nothing was sent" in m["text"] for m in msgs)


def test_an_ok_while_the_chat_waits_comes_back_in_the_same_call(hub, monkeypatch):
    c, web, app, t = hub
    monkeypatch.setattr(V, "WAIT_SECONDS", 5)
    save(c, t["kobby"])

    def approve_soon():
        for _ in range(50):
            w = c.get("/approvals", headers=H(t["kobby"])).json()
            if w:
                c.post(f"/approvals/{w[0]['id']}/approve", headers=H(t["kobby"]))
                return
            time.sleep(0.05)
    th = threading.Thread(target=approve_soon)
    th.start()
    res = call(c, t["claude"], "call_api", {"key": "instagram", "method": "POST", "json": {"a": 1},
                                            "url": "https://graph.facebook.com/v21.0/1/media_publish"})
    th.join()
    assert "approved by kobby: HTTP 200" in res["content"][0]["text"]
    # and the conversation doesn't get the same result a second time
    msgs = call(c, t["claude"], "check_replies")["structuredContent"]["messages"]
    assert not any("HTTP 200" in m["text"] for m in msgs)


def test_keys_can_ride_in_a_header_or_the_query_and_can_ask_every_time_or_never(hub):
    c, web, app, t = hub
    save(c, t["kobby"], name="youtube", hosts=["www.googleapis.com"], place="query", field="key", ask="never")
    res = call(c, t["claude"], "call_api", {"key": "youtube", "method": "POST", "json": {},
                                            "url": "https://www.googleapis.com/youtube/v3/videos?part=id"})
    assert "HTTP 200" in res["content"][0]["text"]                                   # never asks
    assert web.calls[-1]["url"] == f"https://www.googleapis.com/youtube/v3/videos?part=id&key={SECRET}"
    assert SECRET not in res["content"][0]["text"]
    save(c, t["kobby"], name="cms", hosts=["api.cms.example"], place="header", field="X-Api-Key", ask="always")
    res = call(c, t["claude"], "call_api", {"key": "cms", "url": "https://api.cms.example/posts"})
    assert res["structuredContent"]["status"] == "waiting"                          # even a GET asks
    c.post(f"/approvals/{res['structuredContent']['request']}/approve", headers=H(t["kobby"]))
    assert web.calls[-1]["headers"]["X-Api-Key"] == SECRET and "Authorization" not in web.calls[-1]["headers"]


def test_the_owner_can_replace_narrow_or_remove_a_key(hub):
    c, web, app, t = hub
    k = save(c, t["kobby"])
    r = c.put(f"/keys/{k['id']}", headers=H(t["kobby"]), json={"secret": "new-secret-value-9999", "ask": "always"}).json()
    assert r["hint"] == "9999" and r["ask"] == "always"
    res = call(c, t["claude"], "call_api", {"key": "instagram", "url": "https://graph.facebook.com/me"})
    cid = res["structuredContent"]["request"]
    c.post(f"/approvals/{cid}/approve", headers=H(t["kobby"]))
    assert web.calls[-1]["headers"]["Authorization"] == "Bearer new-secret-value-9999"
    hist = c.get(f"/keys/{k['id']}/calls", headers=H(t["kobby"])).json()
    assert hist[0]["id"] == cid and hist[0]["status"] == "done"
    assert c.put(f"/keys/{k['id']}", headers=H(t["ama"]), json={"ask": "never"}).status_code == 404
    assert c.delete(f"/keys/{k['id']}", headers=H(t["kobby"])).status_code == 200
    assert call(c, t["claude"], "saved_keys")["structuredContent"]["keys"] == []


def test_a_different_vault_key_cannot_open_saved_keys(tmp_path):
    store_dir = str(tmp_path)
    app = create_app(data_dir=store_dir, admin_token=ADMIN, vault_send=FakeWeb(), vault_resolve=public)
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    save(c, kobby)
    v2 = V.Vault(app.state.store, time.time, "some-other-secret")
    with app.state.store.ro() as db:
        row = db.execute("SELECT * FROM vault_keys").fetchone()
    with pytest.raises(V.VaultError, match="save it again"):
        v2._open(row)


def test_saved_keys_and_wakeups_move_off_the_admin_token_and_survive_changing_it(tmp_path):
    """Before the hub had its own secret, keys were sealed with the admin token. The first start with
    the new code moves them; after that the admin token can change without losing anything."""
    from relay.wake import Waker, _cipher
    d = str(tmp_path)
    app = create_app(data_dir=d, admin_token="old-admin", vault_send=FakeWeb(), vault_resolve=public)
    c = TestClient(app)
    kobby = c.post("/owner", headers=H("old-admin"), json={"handle": "kobby"}).json()["token"]
    k = save(c, kobby)
    store = app.state.store
    # make the rows look like the old code wrote them: sealed with the admin token
    old_vault = V.Vault(store, time.time, "old-admin")
    old_wake = _cipher("old-admin", None)
    with store.tx() as db:
        db.execute("UPDATE vault_keys SET sealed=? WHERE id=?", (old_vault._seal(k["id"], "kobby", SECRET), k["id"]))
        db.execute("INSERT INTO wakes(handle,kind,url,secret,created) VALUES ('claude','routine','https://api.example/x',?,?)",
                   (old_wake.encrypt(b"routine-token").decode(), time.time()))
    # the next start moves them; then the admin token changes and everything still opens
    create_app(data_dir=d, admin_token="old-admin", vault_send=FakeWeb(), vault_resolve=public)
    app3 = create_app(data_dir=d, admin_token="brand-new-admin", vault_send=FakeWeb(), vault_resolve=public)
    with app3.state.store.ro() as db:
        row = db.execute("SELECT * FROM vault_keys").fetchone()
        wake = db.execute("SELECT secret FROM wakes WHERE handle='claude'").fetchone()
    assert app3.state.vault._open(row) == SECRET
    assert app3.state.waker.fernet.decrypt(wake["secret"].encode()) == b"routine-token"
    import os, stat
    assert stat.S_IMODE(os.stat(os.path.join(d, "hub_secret")).st_mode) == 0o600
