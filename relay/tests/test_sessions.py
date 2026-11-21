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


# ------------------------------------------------------------------ chats that go away
@pytest.fixture
def quick(tmp_path):
    """A hub where a chat counts as idle after 0.5 s and the sweep runs every 0.2 s."""
    rec = Recorder()
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, wake_opener=rec,
                              wake_check_url=False, wake_cooldown=0.1,
                              session_idle_seconds=0.5, session_sweep_seconds=0.2))
    toks = {}
    for h in ("claude", "muse"):
        toks[h] = c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": h}).json()["token"]
        c.put(f"/agents/{h}/wake", headers={"Authorization": f"Bearer {ADMIN}"},
              json={"kind": "webhook", "url": f"https://wake.example/{h}"})
    return c, rec, toks


def answered(c, t):
    """Chat A asks; a one-shot Muse run (muse-1) answers; chat A reads the answer."""
    asked = ask(c, t, "chat-A", "three hook ideas?")
    call(c, t["muse"], "check_replies", session="muse-1")
    ans = call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "1, 2, 3"}, "muse-1")
    call(c, t["claude"], "check_replies", session="chat-A")
    return asked, ans


def test_a_follow_up_to_an_answering_chat_that_went_quiet_goes_to_a_new_chat(quick):
    c, rec, t = quick
    asked, ans = answered(c, t)
    assert fired(rec, "muse") == 1
    time.sleep(0.7)                                       # muse-1 has been quiet past the idle limit
    call(c, t["claude"], "reply", {"message_id": ans["id"], "text": "expand on 2"}, "chat-A")
    assert fired(rec, "muse", 2) == 2                     # a new muse chat is woken for it
    got = call(c, t["muse"], "check_replies", session="muse-2")["messages"]
    assert [m["text"] for m in got] == ["expand on 2"]
    assert [x["text"] for x in got[0]["context"]] == ["three hook ideas?", "1, 2, 3"]   # with the thread
    later = call(c, t["muse"], "reply", {"message_id": got[0]["id"], "text": "2, longer"}, "muse-2")
    back = call(c, t["claude"], "check_replies", session="chat-A")["messages"]
    assert [m["id"] for m in back] == [later["id"]]       # and the answer still comes back to chat A


def test_a_follow_up_held_for_a_chat_that_then_goes_quiet_is_handed_over(quick):
    c, rec, t = quick
    asked, ans = answered(c, t)
    assert fired(rec, "muse") == 1
    call(c, t["claude"], "reply", {"message_id": ans["id"], "text": "expand on 2"}, "chat-A")   # muse-1 still fresh
    assert fired(rec, "muse", 2) == 2                     # muse-1 went quiet: the sweep handed it over
    got = call(c, t["muse"], "check_replies", session="muse-2")["messages"]
    assert [m["text"] for m in got] == ["expand on 2"]


def test_the_helper_saying_a_run_ended_hands_over_at_once(hub):
    c, rec, t = hub                                       # idle limit 10 min: only the end signal can do it
    asked, ans = answered(c, t)
    assert fired(rec, "muse") == 1
    call(c, t["claude"], "reply", {"message_id": ans["id"], "text": "expand on 2"}, "chat-A")
    settle()
    assert rec.count("muse") == 1                         # held for muse-1, which is still "alive"
    r = c.post("/sessions/end", headers={"Authorization": f"Bearer {t['muse']}", "X-Relay-Session": "muse-1"})
    assert r.status_code == 200 and r.json()["released"] == 1
    assert fired(rec, "muse", 2) == 2
    assert [m["text"] for m in call(c, t["muse"], "check_replies", session="muse-2")["messages"]] == ["expand on 2"]
    call(c, t["claude"], "reply", {"message_id": ans["id"], "text": "and 3?"}, "chat-A")
    assert [m["text"] for m in call(c, t["muse"], "check_replies", session="muse-2")["messages"]] == ["and 3?"]


def test_an_idle_asking_chat_keeps_its_answer_however_long_it_is_away(quick):
    c, rec, t = quick
    asked = ask(c, t, "chat-A", "review my hook")
    call(c, t["muse"], "check_replies", session="muse-1")
    time.sleep(0.7)                                       # chat A is idle, well past the limit
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "cut the first second"}, "muse-1")
    time.sleep(0.8)                                       # several sweeps
    assert rec.count("claude") == 0
    assert call(c, t["claude"], "check_replies", session="chat-B")["messages"] == []
    assert [m["text"] for m in call(c, t["claude"], "check_replies", session="chat-A")["messages"]] == ["cut the first second"]


# ------------------------------------------------------------------ answering with ask
def test_ask_refuses_to_answer_a_waiting_message_and_points_to_reply(hub):
    c, rec, t = hub
    req = ask(c, t, "muse-1", "can you caption this clip?", to="claude")
    call(c, t["claude"], "check_replies", session="chat-A")
    r = c.post("/mcp", headers={"Authorization": f"Bearer {t['claude']}", "X-Relay-Session": "chat-A"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ask", "arguments": {"to": "muse", "message": "done, here it is", "wait_seconds": 0}}})
    res = r.json()["result"]
    assert res["isError"] and f"reply with message_id {req['asked']}" in res["content"][0]["text"]
    assert call(c, t["muse"], "check_replies", session="muse-1")["messages"] == []      # nothing was sent
    assert call(c, t["claude"], "ask", {"to": "muse", "message": "unrelated: lunch?", "wait_seconds": 0,
                                        "new_topic": True}, "chat-A")["status"] == "pending"
    assert call(c, t["claude"], "ask", {"to": "muse", "message": "x", "wait_seconds": 0}, "chat-B")["status"] == "pending"
    call(c, t["claude"], "reply", {"message_id": req["asked"], "text": "done"}, "chat-A")
    assert call(c, t["claude"], "ask", {"to": "muse", "message": "next?", "wait_seconds": 0}, "chat-A")["status"] == "pending"


# ------------------------------------------------------------------ taking a conversation back
def test_a_chat_can_take_a_conversation_over_by_its_id(hub):
    c, rec, t = hub
    asked = ask(c, t, "chat-A", "three hook ideas?")      # then chat A's id is lost (the app reconnected)
    call(c, t["muse"], "check_replies", session="muse-1")
    call(c, t["muse"], "reply", {"message_id": asked["asked"], "text": "1, 2, 3"}, "muse-1")
    assert call(c, t["claude"], "check_replies", session="chat-A2")["messages"] == []
    got = call(c, t["claude"], "check_replies", {"thread": asked["asked"]}, "chat-A2")["messages"]
    assert [m["text"] for m in got] == ["1, 2, 3"]
    assert call(c, t["claude"], "check_replies", session="chat-A2")["messages"] == []   # not handed twice
    assert call(c, t["claude"], "check_replies", session="chat-A")["messages"] == []    # moved, not copied
    call(c, t["muse"], "reply", {"message_id": got[0]["id"], "text": "and a 4th"}, "muse-1")
    assert [m["text"] for m in call(c, t["claude"], "check_replies", session="chat-A2")["messages"]] == ["and a 4th"]
    r = c.post("/mcp", headers={"Authorization": f"Bearer {t['muse']}", "X-Relay-Session": "x"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "check_replies", "arguments": {"thread": 999999}}})
    assert r.json()["result"]["isError"]


# ------------------------------------------------------------------ no old backlog for new chats
def test_new_chats_do_not_get_messages_from_before_session_routing(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    tok = {h: c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": h}).json()["token"]
           for h in ("claude", "muse")}
    for i in range(3):                                    # unread, from before the upgrade
        c.post("/messages", headers={"Authorization": f"Bearer {tok['muse']}"},
               json={"channel": "direct", "text": f"old {i}", "mentions": ["claude"]})
    import sqlite3
    db = sqlite3.connect(str(tmp_path / "relay.db"))
    db.execute("DELETE FROM meta"); db.commit(); db.close()      # as if routing arrived only now
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    c.post("/messages", headers={"Authorization": f"Bearer {tok['muse']}"},
           json={"channel": "direct", "text": "new", "mentions": ["claude"]})
    assert [m["text"] for m in call(c, tok["claude"], "check_replies", session="fresh")["messages"]] == ["new"]
    legacy = [m["text"] for m in call(c, tok["claude"], "check_replies")["messages"]]
    assert legacy[:3] == ["old 0", "old 1", "old 2"]      # identity-wide readers still get them, as before


def test_chats_unheard_of_for_30_days_are_forgotten_unless_something_waits_for_them(hub):
    c, rec, t = hub
    ask(c, t, "old-done", "q1")                           # its answer was read: nothing waits
    ask(c, t, "old-waiting", "q2")                        # an answer still waits for it
    call(c, t["muse"], "check_replies", session="m")
    msgs = c.get("/messages", params={"channel": "direct"}, headers={"Authorization": f"Bearer {t['muse']}"}).json()
    for m in msgs:
        if m["from"] == "claude":
            call(c, t["muse"], "reply", {"message_id": m["id"], "text": "a"}, "m")
    call(c, t["claude"], "check_replies", session="old-done")
    import sqlite3
    db = sqlite3.connect(c.app.state.store.db_path)
    db.execute("UPDATE session_cursors SET seen=0 WHERE session LIKE 'old-%'"); db.commit()
    c.app.state.sweep()
    left = {r[0] for r in db.execute("SELECT session FROM session_cursors WHERE handle='claude'")}
    db.close()
    assert left == {"old-waiting"}
    assert [m["text"] for m in call(c, t["claude"], "check_replies", session="old-waiting")["messages"]] == ["a"]
