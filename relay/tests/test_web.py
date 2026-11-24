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
                       ("/static/mark.svg", "image/svg+xml"), ("/static/hero-run.js", "text/javascript"),
                       ("/static/hero-run.json", "application/json"), ("/static/hero-sheet.webp", "image/webp"),
                       ("/static/hero-clip1.mp4", "video/mp4"), ("/static/hero-clip1.jpg", "image/jpeg")]:
        r = hub.get(path)
        assert r.status_code == 200 and r.headers["content-type"].startswith(kind), path
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "media-src 'self'" in csp and "default-src 'none'" in csp, path
        assert r.headers["x-content-type-options"] == "nosniff", path
    for bad in ["/static/../hub.py", "/static/hub.py", "/static/nope.js", "/static/specials.js"]:
        assert hub.get(bad).status_code == 404, bad


def test_videos_stream_in_byte_ranges(hub):
    """Safari and iOS will not play a video unless the server answers range requests."""
    full = hub.get("/static/hero-clip2.mp4")
    assert full.headers.get("accept-ranges") == "bytes" and "max-age" in full.headers["cache-control"]
    r = hub.get("/static/hero-clip2.mp4", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.content) == 100 and r.content == full.content[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(full.content)}"
    assert "media-src 'self'" in r.headers["content-security-policy"]


def test_the_hero_replays_a_real_recorded_run():
    run = json.load(open(os.path.join(WEB, "hero-run.json")))
    js = open(os.path.join(WEB, "hero-run.js")).read()
    embedded = js[js.index("Object.freeze(") + len("Object.freeze("):js.rindex(");\n})();")]
    assert json.loads(embedded) == run                     # the page replays exactly the recorded file
    # every file it names shipped, the clips are real vertical H.264 and the whole thing stays light
    files = [run["digest"]["sheet"]] + [f for c in run["clips"] for f in (c["file"], c["poster"])]
    for f in files + [f for m in run["messages"] for f in m["files"]]:
        assert os.path.exists(os.path.join(WEB, f)), f
    for c in run["clips"]:
        head = open(os.path.join(WEB, c["file"]), "rb").read(64)
        assert b"ftyp" in head and os.path.getsize(os.path.join(WEB, c["file"])) == c["bytes"] < 3_000_000
    assert sum(os.path.getsize(os.path.join(WEB, f)) for f in set(files)) < 8_000_000
    # the timeline holds together: jobs finish after they start, replies follow their jobs
    assert [j["kind"] for j in run["jobs"]] == ["digest", "clip"]
    times = [m["t"] for m in run["messages"]]
    assert times == sorted(times) and times[0] == 0
    for j in run["jobs"]:
        assert 0 < j["seconds"] and j["queued"] < j["done"]
    said = " ".join(m["text"] for m in run["messages"])
    assert f"{run['digest']['shots']} shots" in said and "Post clip 2 at 9:00?" in said
    assert "Blender Foundation" in run["source"]["credit"]
    # and the landing page uses no video it did not record, and never loops
    page = open(os.path.join(WEB, "landing.html")).read() + open(os.path.join(WEB, "landing.js")).read()
    assert "loop" not in page.lower() and "autoplay" not in page.lower()


def test_no_script_writes_html():
    for path in glob.glob(os.path.join(WEB, "*.js")):
        src = open(path).read()
        for bad in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
            assert bad not in src, f"{bad} in {os.path.basename(path)}"


def test_landing_page_never_calls_the_api():
    src = "".join(open(os.path.join(WEB, f)).read() for f in ("landing.js", "landing.html", "hero-run.js"))
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
