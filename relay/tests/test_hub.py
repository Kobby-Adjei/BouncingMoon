"""The hub's rules: who can do what, and what can never happen twice."""
import hashlib
import time

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


@pytest.fixture
def hub(tmp_path):
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, lease_seconds=0.6, max_attempts=2,
                     retry_base_seconds=0.5)
    return TestClient(app)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def reg(hub, handle, caps=(), human=False):
    r = hub.post("/agents", headers=H(ADMIN),
                 json={"handle": handle, "caps": list(caps), "role": "human" if human else "agent"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


# ---------------------------------------------------------------- identity
def test_only_admin_registers_and_tokens_are_secret(hub):
    claude = reg(hub, "claude")
    assert hub.post("/agents", headers=H(claude), json={"handle": "x"}).status_code == 403
    assert hub.post("/agents", headers=H(ADMIN), json={"handle": "claude"}).status_code == 409
    assert hub.post("/agents", headers=H(ADMIN), json={"handle": "relay"}).status_code == 400
    listed = hub.get("/agents", headers=H(claude)).json()
    assert listed[0]["handle"] == "claude" and "token" not in listed[0]
    assert hub.get("/agents/me", headers=H("nope")).status_code == 401


# ---------------------------------------------------------------- messaging
def test_threads_mentions_and_cursor_inbox(hub):
    claude, muse = reg(hub, "claude"), reg(hub, "muse")
    root = hub.post("/messages", headers=H(claude),
                    json={"channel": "clips", "text": "brief: launch-time clip", "mentions": ["muse"]}).json()
    assert root["thread"] == root["id"]
    # unknown mentions fail before anything is written
    assert hub.post("/messages", headers=H(claude),
                    json={"channel": "clips", "text": "x", "mentions": ["nobody"]}).status_code == 400
    box = hub.get("/inbox", headers=H(muse)).json()
    assert [m["id"] for m in box["messages"]] == [root["id"]]
    # replying resolves channel and thread server-side; the root author follows the thread
    rep = hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": "on it"}).json()
    assert rep["thread"] == root["id"] and rep["channel"] == "clips"
    assert hub.get("/inbox", headers=H(claude)).json()["messages"][0]["text"] == "on it"
    # at-least-once: unacked messages come back; acked ones do not
    assert hub.get("/inbox", headers=H(muse)).json()["messages"]
    hub.post("/inbox/ack", headers=H(muse), json={"cursor": box["cursor"]})
    assert hub.get("/inbox", headers=H(muse)).json()["messages"] == []


def test_idempotent_posts(hub):
    claude = reg(hub, "claude")
    a = hub.post("/messages", headers=H(claude), json={"channel": "c", "text": "hi", "idem": "k1"}).json()
    b = hub.post("/messages", headers=H(claude), json={"channel": "c", "text": "hi", "idem": "k1"}).json()
    assert a["id"] == b["id"]
    assert hub.post("/messages", headers=H(claude),
                    json={"channel": "c", "text": "changed", "idem": "k1"}).status_code == 409


def test_channel_subscription(hub):
    a, b = reg(hub, "a"), reg(hub, "b")
    hub.post("/subscriptions", headers=H(b), json={"channel": "clips"})
    hub.post("/messages", headers=H(a), json={"channel": "clips", "text": "new brief"})
    hub.post("/messages", headers=H(a), json={"channel": "other", "text": "noise"})
    assert [m["text"] for m in hub.get("/inbox", headers=H(b)).json()["messages"]] == ["new brief"]


# ---------------------------------------------------------------- jobs
def test_jobs_route_by_capability_and_claim_once(hub):
    claude = reg(hub, "claude")
    laptop = reg(hub, "laptop", caps=["fetch"])
    other = reg(hub, "other", caps=["fetch"])
    nofetch = reg(hub, "nofetch", caps=["digest"])
    j = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": "https://example.com/y.mp4"}}).json()
    assert j["status"] == "queued"
    assert hub.post("/jobs/claim", headers=H(nofetch), json={}).json()["job"] is None
    got = hub.post("/jobs/claim", headers=H(laptop), json={}).json()["job"]
    assert got["id"] == j["id"] and got["claimed_by"] == "laptop"
    assert hub.post("/jobs/claim", headers=H(other), json={}).json()["job"] is None
    # only the holder may finish it
    assert hub.post(f"/jobs/{j['id']}/complete", headers=H(other), json={}).status_code == 409
    # the requester hears about pickup without being in the room
    texts = [m["text"] for m in hub.get("/inbox", headers=H(claude)).json()["messages"]]
    assert any("picked up" in t for t in texts)


def test_fetch_params_are_validated(hub):
    claude = reg(hub, "claude")
    for bad in ({}, {"url": "file:///etc/passwd"}, {"url": "ftp://x"}):
        assert hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": bad}).status_code == 400


def test_fetch_cannot_target_private_networks(hub):
    """Once the hub is public, nobody may use a node to reach that node's own machine,
    its home network, or a cloud metadata endpoint."""
    claude = reg(hub, "claude")
    for url in ("http://127.0.0.1:8787/admin", "http://localhost/x", "http://10.0.0.5/x",
                "http://192.168.1.1/", "http://169.254.169.254/latest/meta-data/",
                "http://[::1]/x", "http://router/", "http://printer.local/"):
        r = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": url}})
        assert r.status_code == 400, url
    ok = hub.post("/jobs", headers=H(claude),
                  json={"kind": "fetch", "params": {"url": "https://archive.org/x.mp4"}})
    assert ok.status_code == 200


def test_failed_job_backs_off_before_retry(hub):
    claude, node = reg(hub, "claude"), reg(hub, "node", ["fetch"])
    j = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": "https://example.com/1.mp4"}}).json()
    hub.post("/jobs/claim", headers=H(node), json={})
    back = hub.post(f"/jobs/{j['id']}/fail", headers=H(node), json={"error": "bot check", "retry": True}).json()
    assert back["status"] == "queued" and back["not_before"] is not None
    assert hub.post("/jobs/claim", headers=H(node), json={}).json()["job"] is None   # waiting
    time.sleep(0.6)
    assert hub.post("/jobs/claim", headers=H(node), json={}).json()["job"]["id"] == j["id"]


def test_sleeping_node_loses_its_lease(hub):
    claude, a, b = reg(hub, "claude"), reg(hub, "a", ["fetch"]), reg(hub, "b", ["fetch"])
    j = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": "https://example.com/1.mp4"}}).json()
    assert hub.post("/jobs/claim", headers=H(a), json={}).json()["job"]["id"] == j["id"]
    time.sleep(0.8)                                    # node a went to sleep
    again = hub.post("/jobs/claim", headers=H(b), json={}).json()["job"]
    assert again["id"] == j["id"] and again["claimed_by"] == "b" and again["attempts"] == 2
    time.sleep(0.8)                                    # b sleeps too: max_attempts=2 -> failed
    assert hub.get(f"/jobs/{j['id']}", headers=H(claude)).json()["status"] == "failed"


def test_heartbeat_keeps_the_lease(hub):
    claude, a = reg(hub, "claude"), reg(hub, "a", ["fetch"])
    j = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": "https://example.com/1.mp4"}}).json()
    hub.post("/jobs/claim", headers=H(a), json={})
    for _ in range(3):
        time.sleep(0.3)
        assert hub.post(f"/jobs/{j['id']}/heartbeat", headers=H(a)).status_code == 200
    assert hub.get(f"/jobs/{j['id']}", headers=H(claude)).json()["claimed_by"] == "a"


def test_approval_gate(hub):
    claude, kobby, node = reg(hub, "claude"), reg(hub, "kobby", human=True), reg(hub, "n", ["fetch"])
    j = hub.post("/jobs", headers=H(claude),
                 json={"kind": "fetch", "params": {"url": "https://example.com/1.mp4"}, "requires_approval": True}).json()
    assert j["status"] == "needs_approval"
    assert hub.post("/jobs/claim", headers=H(node), json={}).json()["job"] is None
    assert hub.post(f"/jobs/{j['id']}/approve", headers=H(claude)).status_code == 403   # agents can't
    assert hub.post(f"/jobs/{j['id']}/approve", headers=H(kobby)).json()["status"] == "queued"
    assert hub.post("/jobs/claim", headers=H(node), json={}).json()["job"]["id"] == j["id"]


# ---------------------------------------------------------------- artifacts
def test_artifacts_by_reference_and_auto_digest(hub):
    claude, node = reg(hub, "claude"), reg(hub, "node", ["fetch", "digest"])
    blob = b"\x00fake-mp4" * 50000
    up = hub.post("/artifacts", headers=H(node), params={"name": "clip.mp4", "mime": "video/mp4"},
                  content=blob).json()
    assert up["id"] == hashlib.sha256(blob).hexdigest() and up["size"] == len(blob)
    again = hub.post("/artifacts", headers=H(node), params={"name": "dup.mp4", "mime": "video/mp4"},
                     content=blob).json()
    assert again["id"] == up["id"]                     # content-addressed: stored once
    assert hub.get(f"/artifacts/{up['id']}", headers=H(claude)).content == blob

    j = hub.post("/jobs", headers=H(claude), json={"kind": "fetch", "params": {"url": "https://example.com/c.mp4"}}).json()
    hub.post("/jobs/claim", headers=H(node), json={"kinds": ["fetch"]})
    done = hub.post(f"/jobs/{j['id']}/complete", headers=H(node),
                    json={"result": {"summary": "fetched"}, "artifacts": [up["id"]]}).json()
    # a fetched video automatically becomes a digest job, owed to the same requester
    fu = done["follow_ups"]
    assert len(fu) == 1 and fu[0]["kind"] == "digest" and fu[0]["requested_by"] == "claude"
    assert fu[0]["thread"] == j["thread"]
    d = hub.post("/jobs/claim", headers=H(node), json={"kinds": ["digest"]}).json()["job"]
    hub.post(f"/jobs/{d['id']}/complete", headers=H(node),
             json={"result": {"summary": "3 shots", "shots": 3}, "artifacts": []})
    meta = hub.get(f"/artifacts/{up['id']}/meta", headers=H(claude)).json()
    assert meta["meta"]["digest"]["result"]["shots"] == 3
