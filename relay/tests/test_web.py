"""The website: landing page, app, static files, and becoming the hub's owner."""
import glob
import os

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"
WEB = os.path.join(os.path.dirname(__file__), "..", "web")


@pytest.fixture
def hub(tmp_path):
    return TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN))


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_pages_and_static_files_are_served_with_a_strict_policy(hub):
    assert "Your agents" in hub.get("/").text and hub.get("/classic").status_code == 200
    assert hub.get("/welcome", follow_redirects=False).headers["location"] == "/"
    for path, kind in [("/app", "text/html"), ("/", "text/html"), ("/static/app.js", "text/javascript"),
                       ("/static/landing.js", "text/javascript"), ("/static/relay.css", "text/css"),
                       ("/static/mark.svg", "image/svg+xml"), ("/static/specials.js", "text/javascript"),
                       ("/static/specials.css", "text/css")]:
        r = hub.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        assert "script-src 'self'" in r.headers["content-security-policy"], path
    for bad in ["/static/../hub.py", "/static/hub.py", "/static/nope.js"]:
        assert hub.get(bad).status_code == 404, bad


def test_no_script_writes_html():
    for path in glob.glob(os.path.join(WEB, "*.js")) + [os.path.join(WEB, "..", "ui.js")]:
        src = open(path).read()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert bad not in src, f"{bad} in {os.path.basename(path)}"


def test_landing_page_never_calls_the_api():
    src = "".join(open(os.path.join(WEB, f)).read() for f in ("landing.js", "landing.html", "specials.js"))
    assert "fetch(" not in src and "XMLHttpRequest" not in src


def test_becoming_the_owner(hub):
    agent = hub.post("/agents", headers=H(ADMIN), json={"handle": "codex"}).json()["token"]
    # only the admin token can make an owner, and only a person's name
    assert hub.post("/owner", headers=H(agent), json={"handle": "kobby"}).status_code == 403
    assert hub.post("/owner", headers=H(ADMIN), json={"handle": "codex"}).status_code == 409
    assert hub.post("/owner", headers=H(ADMIN), json={"handle": "admin"}).status_code == 400
    r = hub.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()
    me = hub.get("/agents/me", headers=H(r["token"])).json()
    assert me["handle"] == "kobby" and me["owner"] is True and me["role"] == "human"
    # the owner can run the hub as themselves...
    assert hub.post("/agents", headers=H(r["token"]), json={"handle": "muse", "provider": "muse"}).status_code == 200
    assert hub.post("/invites", headers=H(r["token"]), json={"handle": "gemini"}).status_code == 200
    assert hub.post("/messages", headers=H(r["token"]), json={"channel": "direct", "text": "hi", "mentions": ["muse"]}).status_code == 200
    # ...and an ordinary agent still can't
    assert hub.post("/agents", headers=H(agent), json={"handle": "x"}).status_code == 403
    listed = {a["handle"]: a for a in hub.get("/agents", headers=H(agent)).json()}
    assert listed["kobby"]["owner"] is True and listed["codex"]["owner"] is False
    # running it again for the same person gives a fresh key and retires the old one
    r2 = hub.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()
    assert hub.get("/agents/me", headers=H(r["token"])).status_code == 401
    assert hub.get("/agents/me", headers=H(r2["token"])).json()["owner"] is True
