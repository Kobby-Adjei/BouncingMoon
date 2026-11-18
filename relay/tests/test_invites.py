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
    for path, kind in [("/", "text/html"), ("/join", "text/html"), ("/ui.js", "text/javascript"),
                       ("/cli.py", "text/x-python")]:
        r = hub.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        assert "script-src 'self'" in r.headers["content-security-policy"]
    assert "def join(" in hub.get("/cli.py").text
    for name in ("codex", "claude", "gemini", "copilot", "muse"):
        r = hub.get(f"/logos/{name}.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert hub.get("/logos/..%2Fhub.svg").status_code == 404
    assert hub.get("/logos/nope.svg").status_code == 404
    # the page never writes hub data as HTML
    js = hub.get("/ui.js").text
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js


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
