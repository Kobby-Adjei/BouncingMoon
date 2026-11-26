"""Quick answers: an agent can offer a few short answers; the person taps one, and it goes back as a reply."""
import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "answers-admin"


def H(tok, session=None):
    return {"Authorization": f"Bearer {tok}", **({"X-Relay-Session": session} if session else {})}


@pytest.fixture
def hub(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False))
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    codex = c.post("/agents", headers=H(kobby), json={"handle": "codex"}).json()["token"]
    return c, kobby, codex


def mcp(c, tok, name, args):
    r = c.post("/mcp", headers=H(tok, "chat-1"), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                         "params": {"name": name, "arguments": args}}).json()
    return r["result"]


def test_an_agent_offers_answers_and_the_tap_comes_back_as_a_reply(hub):
    c, kobby, codex = hub
    first = c.post("/messages", headers=H(kobby), json={"channel": "direct", "text": "@codex post the cut", "mentions": ["codex"]}).json()
    mcp(c, codex, "check_replies", {})
    out = mcp(c, codex, "reply", {"message_id": first["id"], "text": "Ready. When should it go out?",
                                  "options": ["Post now", "Tonight at 8", "post NOW", " ", "x" * 200, "a", "b", "c", "d"]})
    msgs = c.get(f"/messages?thread={first['thread']}", headers=H(kobby)).json()
    q = next(m for m in msgs if m["from"] == "codex")
    assert q["options"] == ["Post now", "Tonight at 8", "x" * 60, "a", "b", "c"]     # once each, short, at most six
    assert "options" not in msgs[0]
    # the tap: an ordinary reply to that message
    c.post("/messages", headers=H(kobby), json={"reply_to": q["id"], "text": "Tonight at 8", "mentions": ["codex"]})
    got = mcp(c, codex, "check_replies", {})["structuredContent"]["messages"]
    assert [m["text"] for m in got] == ["Tonight at 8"] and got[0]["reply_to"] == q["id"]


def test_ask_can_offer_answers_too(hub):
    c, kobby, codex = hub
    r = mcp(c, codex, "ask", {"to": "kobby", "message": "Which thumbnail?", "options": ["The red one", "The blue one"], "wait_seconds": 0})
    t = r["structuredContent"]["asked"]
    msgs = c.get(f"/messages?thread={t}", headers=H(kobby)).json()
    assert msgs[0]["options"] == ["The red one", "The blue one"]


def test_agents_are_told_about_quick_answers_and_short_messages(hub):
    c, kobby, codex = hub
    init = c.post("/mcp", headers=H(codex, "chat-1"), json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}).json()["result"]
    assert "`options`" in init["instructions"] and "short and plain" in init["instructions"]
    tools = {t["name"]: t for t in c.post("/mcp", headers=H(codex, "chat-1"), json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]}
    assert "options" in tools["ask"]["inputSchema"]["properties"] and "options" in tools["reply"]["inputSchema"]["properties"]
    assert "options" in tools["reply"]["description"]


def test_a_newer_mcp_client_is_told_to_fall_back_and_then_gets_its_own_session(hub):
    c, kobby, codex = hub
    modern = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28", "io.modelcontextprotocol/clientInfo": {"name": "x", "version": "1"}}}}
    r = c.post("/mcp", headers={**H(codex), "MCP-Protocol-Version": "2026-07-28"}, json=modern)
    # 400 with a body that is NOT a JSON-RPC error: per the spec, a dual-era client then falls back to initialize
    assert r.status_code == 400 and r.headers["content-type"].startswith("text/plain") and "initialize" in r.text
    r = c.post("/mcp", headers={**H(codex), "MCP-Protocol-Version": "2026-07-28"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}})
    assert r.status_code == 400                                                            # body-only signal too
    init = c.post("/mcp", headers=H(codex), json={"jsonrpc": "2.0", "id": 2, "method": "initialize",
        "params": {"protocolVersion": "2026-07-28", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}})
    assert init.status_code == 200 and init.headers.get("mcp-session-id", "").startswith("rs_")
    assert init.json()["result"]["protocolVersion"] == "2025-11-25"                        # it settles on ours
    ok = c.post("/mcp", headers={**H(codex), "MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": init.headers["mcp-session-id"]},
                json={"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    assert ok.status_code == 200 and ok.json()["result"]["tools"]                         # older versions unchanged
