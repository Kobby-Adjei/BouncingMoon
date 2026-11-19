"""Wake-ups and instant delivery: an agent hears about a message the moment it arrives."""
import hashlib
import hmac
import json
import os
import socket
import sys
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from relay import cli
from relay.hub import create_app

ADMIN = "admin-test-token"
ROUTINE = "https://api.anthropic.com/v1/claude_code/routines/trig_01TESTTESTTESTTESTTEST/fire"


class FakeResp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Recorder:
    def __init__(self):
        self.calls = []
    def __call__(self, req):
        self.calls.append({"url": req.full_url, "headers": {k.lower(): v for k, v in req.header_items()},
                           "body": req.data})
        return FakeResp()


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def reg(c, handle):
    return c.post("/agents", headers=H(ADMIN), json={"handle": handle}).json()["token"]


def wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def setup(tmp_path):
    rec = Recorder()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, wake_opener=rec,
                     wake_check_url=False, wake_cooldown=0.6)
    return TestClient(app), rec


def test_routine_wake_fires_once_per_burst_without_the_message_text(setup):
    c, rec = setup
    claude, codex = reg(c, "claude"), reg(c, "codex")
    bad = c.put("/agents/codex/wake", headers=H(ADMIN), json={"kind": "claude_routine", "url": "https://evil.example/fire", "token": "x" * 30})
    assert bad.status_code == 400
    ok = c.put("/agents/codex/wake", headers=H(ADMIN),
               json={"kind": "claude_routine", "url": ROUTINE, "token": "sk-ant-oat01-" + "x" * 30})
    assert ok.status_code == 200 and "token" not in json.dumps(ok.json()) and "xxxx" not in json.dumps(ok.json())
    for i in range(3):                                   # a burst of three messages
        c.post("/messages", headers=H(claude), json={"channel": "direct", "text": f"secret plan {i}", "mentions": ["codex"]})
    assert wait_for(lambda: len(rec.calls) >= 1)
    first = rec.calls[0]
    assert first["url"] == ROUTINE
    assert first["headers"]["authorization"].startswith("Bearer sk-ant-oat01-")
    assert first["headers"]["anthropic-beta"] == "experimental-cc-routine-2026-04-01"
    body = json.loads(first["body"])
    assert "codex" in body["text"] and "claude" in body["text"] and "secret plan" not in body["text"]
    time.sleep(1.5)
    assert len(rec.calls) <= 2                           # coalesced, not one per message
    status = c.get("/agents/codex/wake", headers=H(ADMIN)).json()
    assert status["last_status"] == "ok 200" and status["fires"] == len(rec.calls)
    # nobody else is woken: claude has no wake-up set
    assert all(call["url"] == ROUTINE for call in rec.calls)


def test_webhook_is_signed_and_only_the_owner_or_admin_can_set_it(setup):
    c, rec = setup
    claude, muse = reg(c, "claude"), reg(c, "muse")
    assert c.put("/agents/muse/wake", headers=H(claude), json={"kind": "webhook", "url": "https://x.example/h"}).status_code == 403
    out = c.put("/agents/me/wake", headers=H(muse), json={"kind": "webhook", "url": "https://hooks.example/relay"}).json()
    secret = out["signing_secret"]                       # shown once
    assert "signing_secret" not in c.get("/agents/me/wake", headers=H(muse)).json()
    c.post("/messages", headers=H(claude), json={"channel": "direct", "text": "hello", "mentions": ["muse"]})
    assert wait_for(lambda: rec.calls)
    call = rec.calls[0]
    ts, sig = call["headers"]["x-relay-timestamp"], call["headers"]["x-relay-signature"]
    expect = hmac.new(secret.encode(), ts.encode() + b"." + call["body"], hashlib.sha256).hexdigest()
    assert sig == f"sha256={expect}"
    assert json.loads(call["body"]) == {"type": "relay.messages", "agent": "muse", "from": ["claude"], "time": int(ts)}
    # test button and removal
    assert c.post("/agents/me/wake/test", headers=H(muse)).json()["status"] == "ok 200"
    assert c.delete("/agents/me/wake", headers=H(muse)).json() == {"removed": True}


def test_webhook_url_must_be_public_https(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    reg(c, "muse")
    for url in ["http://hooks.example/x", "https://localhost/x", "https://127.0.0.1/x", "https://10.0.0.5/x"]:
        r = c.put("/agents/muse/wake", headers=H(ADMIN), json={"kind": "webhook", "url": url})
        assert r.status_code == 400, url


def test_long_poll_inbox_returns_as_soon_as_a_message_lands(setup):
    c, _ = setup
    claude, muse = reg(c, "claude"), reg(c, "muse")
    got = {}
    def poll():
        t0 = time.time()
        got["box"] = c.get("/inbox", headers=H(muse), params={"wait": 10}).json()
        got["took"] = time.time() - t0
    th = threading.Thread(target=poll); th.start()
    time.sleep(0.8)
    c.post("/messages", headers=H(claude), json={"channel": "direct", "text": "now", "mentions": ["muse"]})
    th.join(12)
    assert got["box"]["messages"][0]["text"] == "now" and got["took"] < 3


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_listen_runs_the_wake_command_once_per_new_batch(tmp_path, monkeypatch):
    port = _free_port()
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close(); break
        except OSError:
            time.sleep(0.05)
    try:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        admin = TestClient(app)
        claude, muse = reg(admin, "claude"), reg(admin, "muse")
        hub = cli.Hub(f"http://127.0.0.1:{port}", muse)
        out = tmp_path / "woke.txt"
        cmd = f'{sys.executable} -c "import os; open(r\'{out}\', \'a\').write(os.environ[\'RELAY_NEW_IDS\'] + chr(10))"'
        th = threading.Thread(target=cli.listen, kwargs={"hub": hub, "run": cmd, "once": True, "log": lambda *_: None})
        th.start()
        time.sleep(0.8)
        m = admin.post("/messages", headers=H(claude), json={"channel": "direct", "text": "wake up", "mentions": ["muse"]}).json()
        th.join(15)
        assert out.read_text().strip() == str(m["id"])
        # it remembers what it announced, so a restart doesn't wake again for the same message
        state = tmp_path / "home" / ".relay" / "agents" / "muse.listen"
        assert state.read_text() == str(m["id"])
    finally:
        server.should_exit = True; t.join(5)


# ------------------------------------------------------------ did the woken agent really reach Relay?
import io
import sqlite3
import urllib.error

from relay.wake import READ_WINDOW, wake_problem, wake_state

TOKEN = "sk-ant-oat01-" + "s3cr3t" * 12


class Switch:
    """A fake fire endpoint whose answer the test can change: 200, or an HTTP error with a body."""
    def __init__(self):
        self.code, self.body, self.calls = 200, b"", 0
    def __call__(self, req):
        self.calls += 1
        if self.code == 200:
            return FakeResp()
        raise urllib.error.HTTPError(req.full_url, self.code, "nope", {}, io.BytesIO(self.body))


@pytest.fixture
def switch(tmp_path):
    sw = Switch()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, wake_opener=sw, wake_check_url=False, wake_cooldown=60)
    return TestClient(app), sw, str(tmp_path / "relay.db")


def mcp_check_replies(c, tok):
    r = c.post("/mcp", headers=H(tok), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "check_replies", "arguments": {}}})
    assert r.status_code == 200 and not r.json()["result"]["isError"]


def test_wake_state_is_derived_from_fire_status_and_read_times():
    now = 10_000.0
    assert wake_state(None, None, None, now) == "ready"
    assert wake_state(now - 5, "failed 401", now, now) == "failed"           # a read doesn't hide a failed fire
    assert wake_state(now - 5, "ok 200", None, now) == "waiting"
    assert wake_state(now - 5, "ok 200", now - 60, now) == "waiting"         # a read from before the fire is old news
    assert wake_state(now - 5, "ok 200", now - 1, now) == "ok"
    assert wake_state(now - READ_WINDOW - 1, "ok 200", None, now) == "no_read"
    assert wake_state(now - READ_WINDOW - 1, "ok 200", now - READ_WINDOW - 10, now) == "no_read"
    assert wake_state(now - READ_WINDOW - 1, "ok 200", now - 2, now) == "ok"  # it read late, but it read


def test_fire_failures_become_plain_words_with_the_fix():
    p = lambda st, kind="claude_routine": wake_problem(kind, "failed", st)
    assert p("failed 401")["fix"] == "form" and "token doesn't match this task" in p("failed 401")["text"]
    assert p("failed 403")["code"] == "token"
    assert p("failed 404")["text"].startswith("Claude can't find that task")
    assert "within the hour" in p("failed 429")["text"]
    assert p("failed 400 paused")["text"] == "This task is paused in Claude. Turn it back on."
    assert p("failed 400")["code"] == "rejected" and p("failed 503")["code"] == "server"
    assert p("failed: URLError")["code"] == "unreachable"
    assert "Connectors" in wake_problem("claude_routine", "no_read", "ok 200")["text"]
    assert "Connectors" not in wake_problem("webhook", "no_read", "ok 200")["text"]
    assert p("failed 401", "webhook")["code"] == "refused"
    for st in ("failed 401", "failed 404", "failed 429", "failed 400", "failed: URLError"):
        assert not any(ch.isdigit() for ch in p(st)["text"]), st   # never a bare code as the headline
    assert wake_problem("claude_routine", "ok", "ok 200") is None


def test_a_successful_fire_waits_for_the_agent_to_read_then_says_so(switch):
    c, sw, db = switch
    claude, codex = reg(c, "claude"), reg(c, "codex")
    assert c.put("/agents/claude/wake", headers=H(ADMIN),
                 json={"kind": "claude_routine", "url": ROUTINE, "token": TOKEN}).json()["state"] == "ready"
    t = c.post("/agents/claude/wake/test", headers=H(ADMIN)).json()
    assert t["status"] == "ok 200" and t["state"] == "waiting" and t["last_read"] is None and t["problem"] is None
    # being active is not reading: other calls, another agent's read, and the listener's long-poll don't count
    c.get("/agents", headers=H(claude)); c.get("/agents/me", headers=H(claude))
    c.get("/inbox", headers=H(codex)); mcp_check_replies(c, codex)
    c.get("/inbox", headers=H(claude), params={"wait": 0.1})
    assert c.get("/agents/claude/wake", headers=H(ADMIN)).json()["state"] == "waiting"
    # the woken task reads through the connector (nothing new is still a read)
    mcp_check_replies(c, claude)
    w = c.get("/agents/claude/wake", headers=H(ADMIN)).json()
    assert w["state"] == "ok" and w["last_read"] >= w["last_fired"] and w["problem"] is None
    # a plain GET /inbox counts too
    c.post("/agents/claude/wake/test", headers=H(ADMIN))
    assert c.get("/agents/claude/wake", headers=H(ADMIN)).json()["state"] == "waiting"
    c.get("/inbox", headers=H(claude))
    assert c.get("/agents/claude/wake", headers=H(ADMIN)).json()["state"] == "ok"


def test_woke_but_never_read_is_reported_not_hidden(switch):
    c, sw, db = switch
    reg(c, "claude")
    c.put("/agents/claude/wake", headers=H(ADMIN), json={"kind": "claude_routine", "url": ROUTINE, "token": TOKEN})
    c.post("/agents/claude/wake/test", headers=H(ADMIN))
    with sqlite3.connect(db) as conn:                        # the fire was over three minutes ago
        conn.execute("UPDATE wakes SET last_fired=last_fired-? WHERE handle='claude'", (READ_WINDOW + 5,))
    w = c.get("/agents/claude/wake", headers=H(ADMIN)).json()
    assert w["state"] == "no_read" and w["last_status"] == "ok 200"
    assert w["problem"]["code"] == "no_read" and "turn on Relay under Connectors" in w["problem"]["text"]


def test_failed_fires_and_a_new_setup_start_clean(switch):
    c, sw, db = switch
    claude = reg(c, "claude")
    c.put("/agents/claude/wake", headers=H(ADMIN), json={"kind": "claude_routine", "url": ROUTINE, "token": TOKEN})
    sw.code = 401
    t = c.post("/agents/claude/wake/test", headers=H(ADMIN))
    assert t.status_code == 200                              # a refused fire is not the hub refusing the owner
    t = t.json()
    assert t["status"] == "failed 401" and t["state"] == "failed" and t["problem"]["fix"] == "form"
    mcp_check_replies(c, claude)                             # reading doesn't make a refused fire look fine
    assert c.get("/agents/claude/wake", headers=H(ADMIN)).json()["state"] == "failed"
    sw.code, sw.body = 400, b'{"type":"error","error":{"message":"This routine is paused"}}'
    t = c.post("/agents/claude/wake/test", headers=H(ADMIN)).json()
    assert t["last_status"] == "failed 400 paused" and t["problem"]["code"] == "paused"
    assert "routine is paused" not in json.dumps(t)          # only a yes/no is kept from the body
    sw.body = b'{"error":{"message":"bad request"}}'
    assert c.post("/agents/claude/wake/test", headers=H(ADMIN)).json()["problem"]["code"] == "rejected"
    sw.code = 429
    assert c.post("/agents/claude/wake/test", headers=H(ADMIN)).json()["problem"]["code"] == "rate_limited"
    # pasting a new link and token clears the old result: no stale fire or read can make it look fine
    sw.code = 200
    w = c.put("/agents/claude/wake", headers=H(ADMIN), json={"kind": "claude_routine", "url": ROUTINE, "token": TOKEN}).json()
    assert w["state"] == "ready" and w["last_fired"] is None and w["last_status"] is None


def test_the_token_never_comes_back_in_any_state(switch):
    c, sw, db = switch
    claude = reg(c, "claude")
    seen = [c.put("/agents/claude/wake", headers=H(ADMIN), json={"kind": "claude_routine", "url": ROUTINE, "token": TOKEN})]
    for code in (200, 401, 404, 429, 400):
        sw.code = code
        seen.append(c.post("/agents/claude/wake/test", headers=H(ADMIN)))
        seen.append(c.get("/agents/claude/wake", headers=H(ADMIN)))
        seen.append(c.get("/agents/me/wake", headers=H(claude)))
    for r in seen:
        assert r.status_code == 200
        text = r.text
        assert "s3cr3t" not in text and "sk-ant" not in text
    with sqlite3.connect(db) as conn:                        # and it is encrypted at rest
        assert "s3cr3t" not in conn.execute("SELECT secret FROM wakes").fetchone()[0]


def test_webhook_wakes_get_the_same_read_check(switch):
    c, sw, db = switch
    muse = reg(c, "muse")
    c.put("/agents/me/wake", headers=H(muse), json={"kind": "webhook", "url": "https://hooks.example/relay"})
    assert c.post("/agents/me/wake/test", headers=H(muse)).json()["state"] == "waiting"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE wakes SET last_fired=last_fired-? WHERE handle='muse'", (READ_WINDOW + 5,))
    w = c.get("/agents/me/wake", headers=H(muse)).json()
    assert w["state"] == "no_read" and w["problem"]["fix"] == "test"
    c.get("/inbox", headers=H(muse))
    assert c.get("/agents/me/wake", headers=H(muse)).json()["state"] == "ok"


def test_an_existing_database_gains_the_read_column(tmp_path):
    """The live hub runs on a database made before last_read existed."""
    db = tmp_path / "relay.db"
    with sqlite3.connect(db) as conn:
        conn.executescript("""
          CREATE TABLE agents(handle TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, caps TEXT NOT NULL DEFAULT '[]',
            role TEXT NOT NULL DEFAULT 'agent', created REAL, last_seen REAL);
          CREATE TABLE wakes(handle TEXT PRIMARY KEY, kind TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
            created REAL, last_fired REAL, last_status TEXT, fires INTEGER NOT NULL DEFAULT 0);
          INSERT INTO agents(handle, token_hash, created) VALUES ('claude', 'x', 1);
          INSERT INTO wakes(handle, kind, url, secret, created, last_fired, last_status, fires)
            VALUES ('claude', 'claude_routine', 'https://api.anthropic.com/v1/claude_code/routines/trig_old/fire', 'x', 1, 2, 'ok 200', 1);
        """)
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, wake_opener=Switch(), wake_check_url=False))
    w = c.get("/agents/claude/wake", headers=H(ADMIN)).json()
    # reads were never recorded before, so an old fire proves nothing either way: press Test
    assert w["last_read"] is None and w["state"] == "ready" and w["fires"] == 1 and w["problem"] is None
