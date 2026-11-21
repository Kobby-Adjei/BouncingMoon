"""Relay as a connector: the MCP endpoint and contacts. Checked against the official
MCP Python client over real HTTP, the way Claude, Codex and other apps connect."""
import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def reg(client, handle, **kw):
    r = client.post("/agents", headers=H(ADMIN), json={"handle": handle, **kw})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def rpc(client, tok, method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "id": mid, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers={**H(tok), "Accept": "application/json, text/event-stream"}, json=body)


def call(client, tok, name, args=None):
    r = rpc(client, tok, "tools/call", {"name": name, "arguments": args or {}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.fixture
def hub(tmp_path):
    return TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))


def test_handshake_auth_and_tool_list(hub):
    tok = reg(hub, "claude", provider="claude")
    assert hub.post("/mcp", json={}).status_code == 401
    assert "Bearer" in hub.post("/mcp", json={}).headers["www-authenticate"]
    assert rpc(hub, ADMIN, "ping").status_code == 403          # the admin token is not an agent
    # apps that reserve Authorization can send the key as X-Relay-Key instead
    alt = hub.post("/mcp", headers={"X-Relay-Key": tok}, json={"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert alt.status_code == 200 and alt.json()["result"] == {}
    api_key = hub.post("/mcp", headers={"X-API-Key": tok}, json={"jsonrpc": "2.0", "id": 9, "method": "ping"})
    assert api_key.status_code == 200
    assert hub.post("/mcp", headers={"X-Relay-Key": "nope"}, json={"jsonrpc": "2.0", "id": 9, "method": "ping"}).status_code == 401
    init = rpc(hub, tok, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert init["protocolVersion"] == "2025-06-18" and "tools" in init["capabilities"]
    # a newer version than we speak is answered with our newest, as the spec allows
    newer = rpc(hub, tok, "initialize", {"protocolVersion": "2099-01-01", "capabilities": {},
                                          "clientInfo": {"name": "t", "version": "1"}}).json()["result"]
    assert newer["protocolVersion"] == "2025-11-25"
    # notifications get 202 and no body
    r = hub.post("/mcp", headers=H(tok), json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert r.status_code == 202 and r.content == b""
    names = {t["name"] for t in rpc(hub, tok, "tools/list").json()["result"]["tools"]}
    assert names == {"contacts", "ask", "check_replies", "reply", "save_contact", "watch_stream", "stream_moments", "stream_audience",
                     "caption_clip", "clip_status", "view_media", "request_upload"}
    assert rpc(hub, tok, "nope/nope").json()["error"]["code"] == -32601
    assert hub.get("/mcp", headers=H(tok)).status_code == 405


def test_contacts_names_resolve_and_bad_targets_explain_themselves(hub):
    claude = reg(hub, "claude", provider="claude")
    reg(hub, "codex", provider="codex", caps=["fetch"])
    out = call(hub, claude, "contacts")
    assert not out["isError"] and "You are claude" in out["content"][0]["text"]
    people = {p["address"]: p for p in out["structuredContent"]["contacts"]}
    assert set(people) == {"codex"} and people["codex"]["runs_on"] == "codex"
    assert people["codex"]["can_do"] == ["fetch"]
    assert not call(hub, claude, "save_contact", {"name": "Kobby's Codex", "address": "codex"})["isError"]
    assert call(hub, claude, "contacts")["structuredContent"]["contacts"][0]["name"] == "Kobby's Codex"
    # asking by saved name (any case) works; unknown names and yourself are refused with a reason
    sent = call(hub, claude, "ask", {"to": "kobby's codex", "message": "hi", "wait_seconds": 0})
    assert sent["structuredContent"]["status"] == "pending" and sent["structuredContent"]["to"] == "codex"
    bad = call(hub, claude, "ask", {"to": "gemini", "message": "hi", "wait_seconds": 0})
    assert bad["isError"] and "codex" in bad["content"][0]["text"]
    assert call(hub, claude, "ask", {"to": "claude", "message": "hi", "wait_seconds": 0})["isError"]
    assert call(hub, claude, "save_contact", {"name": "x", "address": "nobody"})["isError"]
    # the same contacts over REST, owned per agent
    assert hub.get("/contacts", headers=H(claude)).json()["contacts"][0]["name"] == "Kobby's Codex"
    assert hub.get("/contacts", headers=H(ADMIN)).status_code == 403
    assert hub.delete("/contacts/kobby's codex", headers=H(claude)).status_code == 200


def test_ask_check_reply_round_trip(hub):
    claude, codex = reg(hub, "claude"), reg(hub, "codex")
    sent = call(hub, claude, "ask", {"to": "codex", "message": "Review hub.py?", "wait_seconds": 0})
    mid = sent["structuredContent"]["asked"]
    box = call(hub, codex, "check_replies")
    assert f"[{mid}] from claude" in box["content"][0]["text"]
    assert call(hub, codex, "check_replies")["content"][0]["text"] == "Nothing new."     # marked read
    assert not call(hub, codex, "reply", {"message_id": mid, "text": "Looks fine."})["isError"]
    # an app that sends no session id: the answer is held for the conversation it started, listed
    # (not handed out) by a plain check_replies, and opened with check_replies(thread)
    plain = call(hub, claude, "check_replies")
    assert plain["structuredContent"]["messages"] == []
    assert f"thread {mid}: 1 new from codex" in plain["content"][0]["text"]
    got = call(hub, claude, "check_replies", {"thread": mid})["structuredContent"]["messages"]
    assert [m["text"] for m in got] == ["Looks fine."] and got[0]["thread"] == mid
    assert call(hub, claude, "check_replies")["structuredContent"]["waiting_conversations"] == []
    assert call(hub, codex, "reply", {"message_id": 99999, "text": "x"})["isError"]


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_official_mcp_client_asks_and_gets_the_answer(tmp_path):
    """Two agents connect the way an app would. Claude asks Codex; a stand-in for the woken
    Codex answers through its own connection; Claude's ask returns the answer."""
    mcp = pytest.importorskip("mcp")
    httpx2 = pytest.importorskip("httpx2")
    from mcp.client.streamable_http import streamable_http_client

    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True); t.start()
    for _ in range(100):
        try:
            socket.create_connection(("127.0.0.1", port), 0.1).close(); break
        except OSError:
            time.sleep(0.05)
    url = f"http://127.0.0.1:{port}/mcp"
    admin = TestClient(app)
    claude_tok, codex_tok = reg(admin, "claude", provider="claude"), reg(admin, "codex", provider="codex")

    async def codex_answers():
        async with httpx2.AsyncClient(headers=H(codex_tok), timeout=120) as hc:
            async with streamable_http_client(url, http_client=hc) as (r, w):
                async with mcp.ClientSession(r, w) as s:
                    await s.initialize()
                    for _ in range(50):
                        res = await s.call_tool("check_replies", {})
                        msgs = (res.structured_content or {})["messages"]
                        if msgs:
                            await s.call_tool("reply", {"message_id": msgs[0]["id"],
                                                        "text": f"Codex here. You asked: {msgs[0]['text']}"})
                            return
                        await asyncio.sleep(0.3)

    async def claude_asks():
        async with httpx2.AsyncClient(headers=H(claude_tok), timeout=120) as hc:
            async with streamable_http_client(url, http_client=hc) as (r, w):
                async with mcp.ClientSession(r, w) as s:
                    init = await s.initialize()
                    assert init.server_info.name == "relay"
                    tools = {t.name for t in (await s.list_tools()).tools}
                    assert "ask" in tools
                    return await s.call_tool("ask", {"to": "codex", "message": "ping from claude",
                                                     "wait_seconds": 20})

    async def main():
        ask = asyncio.create_task(claude_asks())
        await asyncio.sleep(0.5)
        await codex_answers()
        return await ask

    try:
        res = asyncio.run(main())
        assert not res.is_error
        assert res.structured_content["status"] == "answered"
        assert "Codex here. You asked: ping from claude" in res.content[0].text
    finally:
        server.should_exit = True; t.join(5)


def test_the_app_an_agent_connects_from_sets_its_logo_unless_the_owner_chose(hub):
    muse = reg(hub, "muse", provider="claude")          # picked wrongly when it was invited
    init = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "Meta Muse", "version": "1"}}
    assert rpc(hub, muse, "initialize", init).status_code == 200
    seen = {a["handle"]: a for a in hub.get("/agents", headers=H(muse)).json()}
    assert seen["muse"]["provider"] == "muse" and seen["muse"]["client"] == "Meta Muse"
    # the owner's own choice is final: a later connection doesn't override it
    hub.put("/agents/muse/provider", headers=H(ADMIN), json={"provider": "claude"})
    rpc(hub, muse, "initialize", init)
    assert {a["handle"]: a for a in hub.get("/agents", headers=H(muse)).json()}["muse"]["provider"] == "claude"
    # an app we don't recognise is remembered but changes nothing
    codex = reg(hub, "codex", provider="codex")
    rpc(hub, codex, "initialize", {**init, "clientInfo": {"name": "some-new-agent"}})
    c = {a["handle"]: a for a in hub.get("/agents", headers=H(codex)).json()}["codex"]
    assert c["provider"] == "codex" and c["client"] == "some-new-agent"


def test_a_woken_agent_gets_the_thread_it_is_answering(hub):
    """Live bug: a routine woke with no memory, got only "@claude do this" and said there was no task."""
    claude, muse = reg(hub, "claude"), reg(hub, "muse")
    root = hub.post("/messages", headers=H(muse), json={"channel": "direct", "mentions": ["claude"],
                    "text": "Please cut three 10 s clips from the trailer, busiest shots first."}).json()
    call(hub, claude, "check_replies")                              # an earlier run read and answered it
    hub.post("/messages", headers=H(claude), json={"reply_to": root["id"], "text": "Which trailer?"})
    long = "the Sintel one " + "x" * 3000
    hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": long})
    ask = hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": "@claude do this", "mentions": ["claude"]}).json()
    out = call(hub, claude, "check_replies")
    msgs = out["structuredContent"]["messages"]
    assert [m["id"] for m in msgs] == [ask["id"] - 1, ask["id"]]    # the two new messages, in order
    first = msgs[0]
    ctx = first["context"]
    assert [x["id"] for x in ctx] == [root["id"], root["id"] + 1]    # earlier ones, oldest first
    assert ctx[0]["from"] == "muse" and "busiest shots" in ctx[0]["text"] and ctx[1]["text"] == "Which trailer?"
    assert "context" not in msgs[1]                                   # its thread's earlier messages are in this batch
    text = out["content"][0]["text"]
    assert "busiest shots" in text and "Which trailer?" in text and text.index("busiest") < text.index("do this")
    # context is not marked read: nothing new is left, and nothing old comes back as new
    assert call(hub, claude, "check_replies")["content"][0]["text"] == "Nothing new."
    # a lone reply gets its context capped: the last six messages, about 2,000 characters, longest trimmed
    for i in range(8):
        hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": f"note {i} " + "y" * 900})
    hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": "and now?", "mentions": ["claude"]})
    call(hub, claude, "check_replies")                               # the notes, read
    hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": "ping", "mentions": ["claude"]})
    m = call(hub, claude, "check_replies")["structuredContent"]["messages"][0]
    assert m["text"] == "ping" and len(m["context"]) == 6 and m["context"][-1]["text"] == "and now?"
    assert sum(len(x["text"]) for x in m["context"]) <= 2000 and m["context"][0]["text"].endswith("…")
    # the REST inbox carries the same context for agents that read without the connector
    hub.post("/messages", headers=H(muse), json={"reply_to": root["id"], "text": "pong", "mentions": ["claude"]})
    box = hub.get("/inbox", headers=H(claude)).json()
    assert box["messages"][0]["text"] == "pong" and box["messages"][0]["context"][-1]["text"] == "ping"
