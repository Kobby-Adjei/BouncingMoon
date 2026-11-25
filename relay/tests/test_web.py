"""The website: landing page, app, static files, and becoming the hub's owner."""
import glob
import json
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
    assert "Feel connected." in hub.get("/").text and hub.get("/classic").status_code == 404
    assert hub.get("/welcome", follow_redirects=False).headers["location"] == "/"
    for path, kind in [("/app", "text/html"), ("/", "text/html"), ("/static/app.js", "text/javascript"),
                       ("/static/landing.js", "text/javascript"), ("/static/relay.css", "text/css"),
                       ("/static/mark.svg", "image/svg+xml"), ("/static/icon-512.png", "image/png")]:
        r = hub.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "media-src 'self'" in csp and "default-src 'none'" in csp, path
        assert r.headers["x-content-type-options"] == "nosniff", path
    for bad in ["/static/../hub.py", "/static/hub.py", "/static/nope.js", "/static/specials.js"]:
        assert hub.get(bad).status_code == 404, bad


def test_media_files_stream_in_byte_ranges(hub):
    """Safari and iOS will not play a video unless the server answers range requests: every media file
    the site ships is served that way (the icon stands in for them here)."""
    full = hub.get("/static/icon-512.png")
    assert full.headers.get("accept-ranges") == "bytes" and "max-age" in full.headers["cache-control"]
    r = hub.get("/static/icon-512.png", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.content == full.content[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(full.content)}"
    assert "media-src 'self'" in r.headers["content-security-policy"]


def test_the_landing_demo_is_honest_and_light():
    page = open(os.path.join(WEB, "landing.html")).read()
    js = open(os.path.join(WEB, "landing.js")).read()
    # it says it's a demo, plays once (no loop, no autoplay video) and ships no video at all
    assert "A demo · nothing is sent" in page and "nothing was sent" in js
    assert "loop" not in (page + js).lower() and "autoplay" not in (page + js).lower()
    assert not glob.glob(os.path.join(WEB, "*.mp4")) and "hero-run" not in page
    # no promise the product doesn't keep: strangers are asked to ask for an invite, and no "No keys"
    assert "Ask for an invite" in page and "No keys" not in page
    # the brand team's never-say words stay out of the landing page
    for words in ("working as one", "It just happens", "any MCP app", "instantly", "seamless", "open source", "any AI"):
        assert words.lower() not in (page + js).lower(), words
    # the demo's agent that answers by itself is one that can (Codex with the helper), not a chat app
    assert 'from: "codex", text: "Watched it.' in js and 'from: "muse"' not in js
    for name in ("ChatGPT", "Grok", "Muse", "Claude"):
        assert name in page, name


def test_no_script_writes_html():
    for path in glob.glob(os.path.join(WEB, "*.js")):
        src = open(path).read()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert bad not in src, f"{bad} in {os.path.basename(path)}"


def test_landing_page_never_calls_the_api():
    src = "".join(open(os.path.join(WEB, f)).read() for f in ("landing.js", "landing.html"))
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


def test_a_shared_link_shows_relays_card_and_phones_can_add_it_to_the_home_screen(hub):
    page = hub.get("/").text
    assert '<meta property="og:image" content="http://testserver/og.png">' in page
    assert '<meta name="twitter:card" content="summary_large_image">' in page and "{{origin}}" not in page
    img = hub.get("/og.png")
    assert img.status_code == 200 and img.headers["content-type"] == "image/png" and img.content[:4] == b"\x89PNG"
    m = hub.get("/manifest.webmanifest")
    assert m.headers["content-type"].startswith("application/manifest+json")
    j = m.json()
    assert j["name"] == "Relay" and j["start_url"] == "/app" and {i["sizes"] for i in j["icons"]} >= {"192x192", "512x512"}
    assert "manifest-src 'self'" in hub.get("/app").headers["content-security-policy"]
