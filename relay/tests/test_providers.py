"""One table of apps for the connector, the Allow page and the web app."""
from fastapi.testclient import TestClient

from relay import providers as P
from relay.connector import provider_from_client
from relay.hub import create_app


def test_apps_are_recognised_by_whole_words_most_specific_first():
    cases = {"Claude": "claude", "claude-ai": "claude", "claude-code": "claude", "ChatGPT": "openai",
             "OpenAI Codex": "codex", "codex-mcp-client": "codex", "Grok": "grok", "xAI Grok": "grok",
             "Meta AI": "muse", "Muse": "muse", "Gemini CLI": "gemini", "Cursor": "cursor",
             "Visual Studio Code": "copilot", "Claude via MetaMCP": "claude",
             "MetaMCP": None, "Metabase": None, "museum-bot": None, "mcp-remote": None, "": None}
    for name, want in cases.items():
        assert P.detect(name) == want, name
        assert provider_from_client(name) == want, name


def test_every_logo_ships_and_the_web_app_gets_the_same_table(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token="adm"))
    table = c.get("/providers").json()
    assert [p["id"] for p in table] == [p[0] for p in P.PROVIDERS]
    assert {p["id"]: p["label"] for p in table}["openai"] == "ChatGPT"
    for p in P.LOGOS:
        r = c.get(f"/logos/{p}.svg")
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml"), p
