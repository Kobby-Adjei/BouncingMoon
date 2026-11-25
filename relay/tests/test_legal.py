"""Privacy and terms exist only once the operator's name, contact and place are filled in."""
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


def test_hidden_until_the_operator_is_named(tmp_path, monkeypatch):
    for k in ("NAME", "EMAIL", "PLACE"):
        monkeypatch.delenv(f"RELAY_LEGAL_{k}", raising=False)
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    assert c.get("/privacy").status_code == 404 and c.get("/terms").status_code == 404
    landing = c.get("/").text
    assert "/privacy" not in landing and "{{legal}}" not in landing


def test_filled_in_and_linked_once_named(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_LEGAL_NAME", "Ama <Mensah>")
    monkeypatch.setenv("RELAY_LEGAL_EMAIL", "hello@example.com")
    monkeypatch.setenv("RELAY_LEGAL_PLACE", "Michigan, USA")
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))
    for path in ("/privacy", "/terms"):
        page = c.get(path)
        assert page.status_code == 200 and "text/html" in page.headers["content-type"]
        t = page.text
        assert "{{" not in t and "Ama &lt;Mensah&gt;" in t and "Michigan, USA" in t and "mailto:hello@example.com" in t
    assert "Delete my account" in c.get("/privacy").text and "18" in c.get("/terms").text
    assert '<a href="/privacy">Privacy</a>' in c.get("/").text
