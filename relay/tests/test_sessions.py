"""Answers go back to the exact chat that asked.

One identity (say `claude`) is used from many chats at once. A reply must reach the chat that sent
the request, be held for it while it is closed, and never start or wake a different chat."""
import logging
import time

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


class FakeResp:
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


class Recorder:
    """Stands in for the network: records every wake-up the hub fires."""
    def __init__(self):
        self.urls = []
    def __call__(self, req):
        self.urls.append(req.full_url)
        return FakeResp()
    def count(self, handle):
        return sum(u.endswith("/" + handle) for u in self.urls)


@pytest.fixture
def hub(tmp_path):
    rec = Recorder()
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, wake_opener=rec,
                              wake_check_url=False, wake_cooldown=0.1))
    toks = {}
    for h in ("claude", "muse"):
        r = c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": h})
        toks[h] = r.json()["token"]
        w = c.put(f"/agents/{h}/wake", headers={"Authorization": f"Bearer {ADMIN}"},
                  json={"kind": "webhook", "url": f"https://wake.example/{h}"})
        assert w.status_code == 200, w.text
    return c, rec, toks


def call(c, tok, name, args=None, session=None):
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json, text/event-stream"}
    if session:
        headers["X-Relay-Session"] = session
    r = c.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                              "params": {"name": name, "arguments": args or {}}})
    assert r.status_code == 200, r.text
    res = r.json()["result"]
    assert not res["isError"], res
    return res["structuredContent"]


def settle():
    time.sleep(1.6)          # wake-ups fire up to ~1.3 s after the message is written; wait past that


def fired(rec, handle, n=1, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end and rec.count(handle) < n:
        time.sleep(0.05)
    return rec.count(handle)


def ask(c, toks, session, text, to="muse"):
    return call(c, toks["claude"] if to == "muse" else toks["muse"], "ask",
                {"to": to, "message": text, "wait_seconds": 0}, session)


def test_only_the_asking_chat_gets_the_answer_and_no_chat_is_woken(hub):
    c, rec, t = hub
    asked = ask(c, t, "chat-A", "what's trending on reels?")
    assert fired(rec, "muse") == 1                         # a new request: muse is woken once
    got = call(c, t["muse"], "check_replies", session="muse-1")["messages"]
    assert [m["id"] for m in got] == [asked["asked"]]
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "dance edits"}, "muse-1")
    settle()
    assert rec.count("claude") == 0                       # an answer never wakes or starts a chat
    assert call(c, t["claude"], "check_replies", session="chat-B")["messages"] == []
    a = call(c, t["claude"], "check_replies", session="chat-A")["messages"]
    assert [m["text"] for m in a] == ["dance edits"]
    assert a[0]["thread"] == asked["asked"] and a[0]["reply_to"] == asked["asked"]     # same thread
    assert "origin_session" not in a[0]                   # the session id is never shown
    assert call(c, t["claude"], "check_replies", session="chat-B")["messages"] == []   # still not B's


def test_an_answer_waits_for_a_closed_chat(hub):
    c, rec, t = hub
    asked = ask(c, t, "chat-A", "review my hook")
    call(c, t["muse"], "check_replies", session="muse-1")
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "cut the first second"}, "muse-1")
    settle()                                              # chat A is closed all this time
    assert rec.count("claude") == 0
    for other in ("chat-B", "fresh-chat"):                # nothing else takes it while A is away
        assert call(c, t["claude"], "check_replies", session=other)["messages"] == []
    lis = c.get("/inbox", params={"unclaimed": "true"}, headers={"Authorization": f"Bearer {t['claude']}"})
    assert lis.json()["messages"] == []                   # a listener would not start a chat for it either
    back = call(c, t["claude"], "check_replies", session="chat-A")["messages"]
    assert [m["text"] for m in back] == ["cut the first second"]


def test_a_new_request_wakes_one_chat_and_its_answer_returns_to_the_sender(hub):
    c, rec, t = hub
    call(c, t["claude"], "check_replies", session="chat-A")          # chat A is open, idle
    asked = ask(c, t, "muse-1", "can you caption this clip?", to="claude")
    assert fired(rec, "claude") == 1                       # one wake for a new request
    woken = call(c, t["claude"], "check_replies", session="woken")["messages"]
    assert [m["id"] for m in woken] == [asked["asked"]]
    assert call(c, t["claude"], "check_replies", session="chat-A")["messages"] == []   # taken by the woken chat
    call(c, t["claude"], "reply", {"message_id": asked["asked"], "text": "on it"}, "woken")
    settle()
    assert rec.count("muse") == 0
    assert call(c, t["muse"], "check_replies", session="muse-2")["messages"] == []
    assert [m["text"] for m in call(c, t["muse"], "check_replies", session="muse-1")["messages"]] == ["on it"]


def test_a_follow_up_reaches_the_muse_chat_that_answered(hub):
    c, rec, t = hub
    asked = ask(c, t, "chat-A", "three hook ideas?")
    call(c, t["muse"], "check_replies", session="muse-1")
    ans = call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "1, 2, 3"}, "muse-1")
    call(c, t["claude"], "check_replies", session="chat-A")
    fired(rec, "muse")
    settle()
    before = rec.count("muse")
    # a reply to muse's answer, then one more message in the thread (a reply to chat A's own ask)
    call(c, t["claude"], "reply", {"message_id": ans["id"], "text": "expand on 2"}, "chat-A")
    call(c, t["claude"], "reply", {"message_id": asked["asked"], "text": "and keep it under 10 s"}, "chat-A")
    settle()
    assert rec.count("muse") == before == 1              # no fresh muse chat
    assert call(c, t["muse"], "check_replies", session="muse-2")["messages"] == []
    got = call(c, t["muse"], "check_replies", session="muse-1")["messages"]
    assert [m["text"] for m in got] == ["expand on 2", "and keep it under 10 s"]


def test_old_clients_without_a_session_work_as_before_and_are_warned_about(hub, caplog):
    c, rec, t = hub
    with caplog.at_level(logging.WARNING, logger="relay"):
        asked = ask(c, t, None, "old client here")
    assert any("without a session id" in r.getMessage() for r in caplog.records)
    call(c, t["muse"], "check_replies")
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "heard you"})
    assert fired(rec, "claude") == 1                       # today's behaviour: the identity is woken
    assert [m["text"] for m in call(c, t["claude"], "check_replies")["messages"]] == ["heard you"]


def test_mcp_clients_get_a_session_id_on_initialize_and_it_routes(hub):
    c, rec, t = hub
    init = c.post("/mcp", headers={"Authorization": f"Bearer {t['claude']}"},
                  json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "t", "version": "1"}}})
    sid = init.headers.get("mcp-session-id")
    assert sid and sid.startswith("rs_")

    def mcp(name, args, s):
        r = c.post("/mcp", headers={"Authorization": f"Bearer {t['claude']}", "Mcp-Session-Id": s},
                   json={"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}})
        return r.json()["result"]["structuredContent"]

    asked = mcp("ask", {"to": "muse", "message": "hi", "wait_seconds": 0}, sid)
    call(c, t["muse"], "check_replies", session="m")
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "hello"}, "m")
    assert mcp("check_replies", {}, "rs_someoneelse")["messages"] == []
    assert [m["text"] for m in mcp("check_replies", {}, sid)["messages"]] == ["hello"]


def test_rest_clients_route_by_header_too(hub):
    c, rec, t = hub
    A = {"Authorization": f"Bearer {t['claude']}", "X-Relay-Session": "term-A"}
    B = {"Authorization": f"Bearer {t['claude']}", "X-Relay-Session": "term-B"}
    M = {"Authorization": f"Bearer {t['muse']}", "X-Relay-Session": "m"}
    sent = c.post("/messages", headers=A, json={"channel": "direct", "text": "q", "mentions": ["muse"]}).json()
    assert "origin_session" not in sent
    c.post("/messages", headers=M, json={"reply_to": sent["id"], "text": "a", "mentions": ["claude"]})
    assert c.get("/inbox", headers=B).json()["messages"] == []
    box = c.get("/inbox", headers=A).json()
    assert [m["text"] for m in box["messages"]] == ["a"]
    c.post("/inbox/ack", headers=A, json={"cursor": box["cursor"]})
    assert c.get("/inbox", headers=A).json()["messages"] == []
    assert c.post("/messages", headers={**A, "X-Relay-Session": "bad id\n"},
                  json={"channel": "x", "text": "y"}).status_code in (400, 422)
