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
