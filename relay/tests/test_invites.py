"""Invites, the setup page, and the CLI's `join`: how an agent gets onto the hub
without anyone handling its key."""
import json
import os
import socket
import stat
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from relay import cli
from relay.hub import create_app

ADMIN = "admin-test-token"


@pytest.fixture
def hub(tmp_path):
    return TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def invite(hub, handle, **kw):
    r = hub.post("/invites", headers=H(ADMIN), json={"handle": handle, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_invite_is_single_use_and_carries_role_caps_provider(hub):
    inv = invite(hub, "muse", caps=["fetch", "digest"], provider="openai")
    assert inv["code"].startswith("inv_") and inv["state"] == "pending"
    # listing never shows the code again
    assert "code" not in hub.get("/invites", headers=H(ADMIN)).json()[0]
    # peeking does not use it up
    peek = hub.post("/invites/peek", json={"code": inv["code"]}).json()
    assert peek["handle"] == "muse" and peek["state"] == "pending" and "token" not in peek
    got = hub.post("/invites/claim", json={"code": inv["code"]})
    assert got.status_code == 200
    token = got.json()["token"]
    me = hub.get("/agents/me", headers=H(token)).json()
    assert me["handle"] == "muse" and me["caps"] == ["digest", "fetch"]
    listed = {a["handle"]: a for a in hub.get("/agents", headers=H(token)).json()}
    assert listed["muse"]["provider"] == "openai"
    # second use fails, and says why in plain words
    again = hub.post("/invites/claim", json={"code": inv["code"]})
    assert again.status_code == 404 and "used" in again.json()["detail"]
    assert hub.get("/invites", headers=H(ADMIN)).json()[0]["state"] == "claimed"
    # joining is announced in #general
    msgs = hub.get("/messages", headers=H(token), params={"channel": "general"}).json()
    assert msgs[-1]["text"] == "@muse joined the hub" and msgs[-1]["from"] == "relay"


def test_only_admin_manages_invites(hub):
    tok = hub.post("/agents", headers=H(ADMIN), json={"handle": "claude"}).json()["token"]
    assert hub.post("/invites", headers=H(tok), json={"handle": "x"}).status_code == 403
    assert hub.get("/invites", headers=H(tok)).status_code == 403
    assert hub.post("/invites", json={"handle": "x"}).status_code == 401


def test_bad_names_taken_names_and_duplicate_open_invites(hub):
    hub.post("/agents", headers=H(ADMIN), json={"handle": "claude"})
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "claude"}).status_code == 409
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "Bad Name"}).status_code == 400
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "admin"}).status_code == 400
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "x", "provider": "<b>"}).status_code == 400
    invite(hub, "codex")
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "codex"}).status_code == 409


def test_revoked_and_expired_invites_cannot_be_used(hub):
    inv = invite(hub, "codex")
    assert hub.delete(f"/invites/{inv['id']}", headers=H(ADMIN)).json()["state"] == "revoked"
    assert hub.post("/invites/claim", json={"code": inv["code"]}).status_code == 404
    short = invite(hub, "gemini", hours=0.1)
    store = hub.app.state.store
    with store.tx() as c:                       # move its expiry into the past
        c.execute("UPDATE invites SET expires=? WHERE id=?", (time.time() - 1, short["id"]))
    assert hub.post("/invites/claim", json={"code": short["code"]}).status_code == 404
    assert hub.post("/invites/claim", json={"code": "inv_made_up"}).status_code == 404


def test_handle_taken_between_invite_and_claim(hub):
    inv = invite(hub, "muse")
    hub.post("/agents", headers=H(ADMIN), json={"handle": "muse"})
    assert hub.post("/invites/claim", json={"code": inv["code"]}).status_code == 409


def test_reset_token_kills_the_old_one(hub):
    old = hub.post("/agents", headers=H(ADMIN), json={"handle": "kobby", "role": "human"}).json()["token"]
    assert hub.post("/agents/kobby/token", headers=H(old)).status_code == 403
    new = hub.post("/agents/kobby/token", headers=H(ADMIN)).json()["token"]
    assert hub.get("/agents/me", headers=H(old)).status_code == 401
    assert hub.get("/agents/me", headers=H(new)).json()["handle"] == "kobby"
    assert hub.post("/agents/nobody/token", headers=H(ADMIN)).status_code == 404


def test_pages_are_served_with_a_strict_policy(hub):
    for path, kind in [("/", "text/html"), ("/join", "text/html"), ("/cli.py", "text/x-python")]:
        r = hub.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "def join(" in hub.get("/cli.py").text
    for name in ("codex", "claude", "gemini", "copilot", "muse"):
        r = hub.get(f"/logos/{name}.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert hub.get("/logos/..%2Fhub.svg").status_code == 404
    assert hub.get("/logos/nope.svg").status_code == 404
    assert hub.get("/classic").status_code == 404 and hub.get("/ui.js").status_code == 404     # the old dashboard is gone


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_cli_join_keeps_each_agent_in_its_own_private_file(tmp_path, monkeypatch, capsys):
    port = _free_port()
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close(); break
        except OSError:
            time.sleep(0.05)
    try:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        for k in ("RELAY_URL", "RELAY_TOKEN", "RELAY_CONFIG", "RELAY_AGENT"):
            monkeypatch.delenv(k, raising=False)
        admin = TestClient(app)
        codex = admin.post("/invites", headers=H(ADMIN), json={"handle": "codex"}).json()["code"]
        claude = admin.post("/invites", headers=H(ADMIN), json={"handle": "claude"}).json()["code"]

        assert cli.main(["join", url, codex]) == 0
        f = home / ".relay" / "agents" / "codex.json"
        saved = json.loads(f.read_text())
        assert saved["handle"] == "codex" and saved["url"] == url
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        out = capsys.readouterr().out
        assert "@codex" in out and saved["token"] not in out
        # with one agent on this computer, commands need no flags at all
        assert cli.main(["whoami"]) == 0 and '"handle": "codex"' in capsys.readouterr().out

        # a second agent on the same computer gets its own file; the first key is untouched
        assert cli.main(["join", url, claude]) == 0
        assert json.loads(f.read_text())["token"] == saved["token"]
        capsys.readouterr()
        assert cli.main(["whoami"]) == 1 and "--as" in capsys.readouterr().err
        assert cli.main(["--as", "codex", "whoami"]) == 0 and '"handle": "codex"' in capsys.readouterr().out
        assert cli.main(["--as", "claude", "whoami"]) == 0 and '"handle": "claude"' in capsys.readouterr().out
        assert cli.main(["--as", "nobody", "whoami"]) == 1

        # a used code fails cleanly
        assert cli.main(["join", url, codex]) == 1
        assert "used" in capsys.readouterr().err
    finally:
        server.should_exit = True; t.join(5)


def test_connect_codex_writes_one_table_and_the_helper_prints_the_key(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "config.toml").write_text('model = "x"\n\n[mcp_servers.other]\nurl = "https://o/mcp"\n')
    out = cli.connect_app("codex", "https://hub.example", "codex", "rly_secret")
    assert "rly_secret" not in out
    cli.connect_app("codex", "https://hub.example", "codex", "rly_secret")      # running it again replaces
    import tomllib
    cfg = tomllib.loads((home / ".codex" / "config.toml").read_text())
    assert cfg["model"] == "x" and cfg["mcp_servers"]["other"]["url"] == "https://o/mcp"
    relay_cfg = cfg["mcp_servers"]["relay"]
    assert relay_cfg["url"] == "https://hub.example/mcp"
    assert "mcp-headers" in relay_cfg["http_headers_helper"] and "rly_secret" not in relay_cfg["http_headers_helper"]
    assert (home / ".codex" / "config.toml").read_text().count("[mcp_servers.relay]") == 1
    # the helper Codex runs prints the header, read from the agent's own key file
    monkeypatch.delenv("RELAY_CONFIG", raising=False)
    cli.save_config("https://hub.example", "codex", "rly_secret")
    assert cli.main(["--as", "codex", "mcp-headers"]) == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": "Bearer rly_secret"}


def test_admin_removes_an_agent_and_its_key_dies(hub):
    tok = hub.post("/agents", headers=H(ADMIN), json={"handle": "muse-laptop"}).json()["token"]
    assert hub.delete("/agents/muse-laptop", headers=H(tok)).status_code == 403
    assert hub.delete("/agents/muse-laptop", headers=H(ADMIN)).json() == {"removed": "muse-laptop"}
    assert hub.get("/agents/me", headers=H(tok)).status_code == 401
    assert hub.delete("/agents/muse-laptop", headers=H(ADMIN)).status_code == 404
    # the name is free again
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "muse-laptop"}).status_code == 200


def test_a_rejoin_code_adds_a_key_and_the_old_one_keeps_working(hub):
    """The Relay helper on another computer, for an agent whose key is already in use elsewhere
    (say, Claude's connector): a one-time code that adds a key instead of replacing it."""
    old = hub.post("/agents", headers=H(ADMIN), json={"handle": "claude", "provider": "claude"}).json()["token"]
    person = hub.post("/agents", headers=H(ADMIN), json={"handle": "kobby", "role": "human"}).json()["token"]
    other = hub.post("/agents", headers=H(ADMIN), json={"handle": "codex"}).json()["token"]
    # owner-only, existing agents only, never for people
    assert hub.post("/invites", headers=H(other), json={"handle": "claude", "rejoin": True}).status_code == 403
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "ghost", "rejoin": True}).status_code == 404
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "kobby", "rejoin": True}).status_code == 400
    assert hub.post("/invites", headers=H(ADMIN), json={"handle": "claude"}).status_code == 409   # a plain invite still can't
    first = invite(hub, "claude", rejoin=True)
    inv = invite(hub, "claude", rejoin=True)                       # only the newest code works
    assert inv["rejoin"] and inv["state"] == "pending" and inv["expires"] - inv["created"] == pytest.approx(24 * 3600)
    assert hub.post("/invites/claim", json={"code": first["code"]}).status_code == 404
    got = hub.post("/invites/claim", json={"code": inv["code"]}).json()
    assert got["handle"] == "claude" and got["token"] != old
    assert hub.post("/invites/claim", json={"code": inv["code"]}).status_code == 404       # one time
    for tok in (old, got["token"]):                                # both keys act as the same agent
        assert hub.get("/agents/me", headers=H(tok)).json()["handle"] == "claude"
    assert [a["handle"] for a in hub.get("/agents", headers=H(ADMIN)).json()].count("claude") == 1
    assert not any("claude joined" in m["text"] for m in hub.get("/messages", headers=H(person), params={"channel": "general"}).json())
    # a new key from the agent's page retires every old one
    new = hub.post("/agents/claude/token", headers=H(ADMIN)).json()["token"]
    assert hub.get("/agents/me", headers=H(got["token"])).status_code == 401
    assert hub.get("/agents/me", headers=H(old)).status_code == 401
    assert hub.get("/agents/me", headers=H(new)).status_code == 200
    # and removing the agent removes its extra keys
    again = hub.post("/invites/claim", json={"code": invite(hub, "claude", rejoin=True)["code"]}).json()["token"]
    hub.delete("/agents/claude", headers=H(ADMIN))
    assert hub.get("/agents/me", headers=H(again)).status_code == 401
    late = invite(hub, "codex", rejoin=True)
    hub.delete("/agents/codex", headers=H(ADMIN))
    assert hub.post("/invites/claim", json={"code": late["code"]}).status_code == 404


def test_cli_join_with_a_rejoin_code_over_real_http(tmp_path, monkeypatch, capsys):
    """What the one-line helper install does with a re-join code: join as the existing agent."""
    port = _free_port()
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close(); break
        except OSError:
            time.sleep(0.05)
    try:
        home = tmp_path / "home"
        monkeypatch.setenv("HOME", str(home))
        for k in ("RELAY_URL", "RELAY_TOKEN", "RELAY_CONFIG", "RELAY_AGENT"):
            monkeypatch.delenv(k, raising=False)
        admin = TestClient(app)
        old = admin.post("/agents", headers=H(ADMIN), json={"handle": "claude", "provider": "claude"}).json()["token"]
        code = admin.post("/invites", headers=H(ADMIN), json={"handle": "claude", "rejoin": True}).json()["code"]
        assert cli.main(["join", url, code]) == 0
        saved = json.loads((home / ".relay" / "agents" / "claude.json").read_text())
        assert saved["handle"] == "claude" and saved["token"] != old and saved["token"] not in capsys.readouterr().out
        assert cli.main(["--as", "claude", "whoami"]) == 0 and '"handle": "claude"' in capsys.readouterr().out
        assert admin.get("/agents/me", headers=H(old)).json()["handle"] == "claude"     # the old key still works
        # the installer's "already joined from this computer" check sees it as claimed
        assert admin.post("/invites/peek", json={"code": code}).json()["state"] == "claimed"
    finally:
        server.should_exit = True; t.join(5)


def test_a_person_joins_from_an_invite_link_without_ever_holding_a_key_in_the_browser(hub):
    owner = hub.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    inv = hub.post("/invites", headers=H(owner), json={"handle": "ama", "role": "human", "hours": 72}).json()
    # the link opens the web app; the code rides in the #fragment, so it never reaches the server's logs
    page = hub.get("/join")
    assert page.status_code == 200 and "/static/app.js" in page.text
    peek = hub.post("/invites/peek", json={"code": inv["code"]}).json()
    assert peek == {**peek, "handle": "ama", "role": "human", "state": "pending", "host": "kobby"}
    key = hub.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    # the key buys a session that expires; the page keeps the session, then adds a passkey with it
    sess = hub.post("/auth/session/owner", headers=H(key)).json()["token"]
    me = hub.get("/agents/me", headers=H(sess)).json()
    assert sess.startswith("rly_sess_") and me["handle"] == "ama" and me["role"] == "human" and not me.get("owner")
    assert hub.post("/auth/passkey/register/options", headers=H(sess)).status_code == 200
    # a person can't hand out invites: only the owner brings people in
    assert hub.post("/invites", headers=H(sess), json={"handle": "x"}).status_code == 403


def test_a_stranger_asks_for_an_invite_and_the_owner_answers(hub):
    owner = hub.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    ask = {"name": "Ama Mensah", "contact": "ama@example.com", "note": "Claude and Muse for my shop"}
    assert hub.post("/invite-requests", json=ask).json() == {"asked": True}
    assert hub.post("/invite-requests", json={"name": "", "contact": "x"}).status_code == 400
    # a bot that fills every field is thanked and forgotten
    assert hub.post("/invite-requests", json={**ask, "website": "http://spam.example"}).json() == {"asked": True}
    # only the owner sees who asked; nobody else, not even other people
    waiting = hub.get("/invite-requests", headers=H(owner)).json()
    assert [w["name"] for w in waiting] == ["Ama Mensah"] and waiting[0]["contact"] == "ama@example.com"
    assert hub.get("/invite-requests").status_code == 401
    inv = hub.post("/invites", headers=H(owner), json={"handle": "ama", "role": "human"}).json()
    person = hub.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    assert hub.get("/invite-requests", headers=H(person)).status_code == 403
    assert hub.post(f"/invite-requests/{waiting[0]['id']}/invited", headers=H(owner)).json()["status"] == "invited"
    assert hub.get("/invite-requests", headers=H(owner)).json() == []
    # three asks an hour from one place, then a polite no
    for i in range(2):
        hub.post("/invite-requests", json={**ask, "name": f"n{i}"})
    assert hub.post("/invite-requests", json=ask).status_code == 429


def test_a_made_up_forwarded_address_doesnt_get_round_the_ask_limit(hub):
    ask = {"name": "Spam", "contact": "spam@example.com"}
    codes = [hub.post("/invite-requests", json=ask, headers={"X-Forwarded-For": f"10.0.0.{i}, 203.0.113.7"}).status_code
             for i in range(5)]
    assert codes == [200, 200, 200, 429, 429]       # the address the proxy added counts, not the one the caller wrote
