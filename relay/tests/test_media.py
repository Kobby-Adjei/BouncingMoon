"""Media between agents and people: attach, upload links, already-watched cards, view_media, viewer pages."""
import base64
import functools
import hashlib
import http.server
import os
import shutil
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from relay import media as M
from relay.hub import create_app

ADMIN = "media-admin"
pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="needs ffmpeg")


class FakeTranscriber:
    def words(self, pcm):
        return [{"start": 0.2, "end": 0.6, "word": "Wait"}, {"start": 0.7, "end": 1.0, "word": "for"},
                {"start": 1.1, "end": 1.4, "word": "it"}, {"start": 3.0, "end": 3.5, "word": "THIS"}]

    def release(self):
        pass


@pytest.fixture(scope="module")
def files(tmp_path_factory):
    d = tmp_path_factory.mktemp("media-src")
    ff = lambda *a: subprocess.run(["ffmpeg", "-loglevel", "error", "-y", *a], check=True)
    ff("-f", "lavfi", "-i", "color=red:s=640x360:d=2,format=yuv420p", "-f", "lavfi", "-i", "color=blue:s=640x360:d=2,format=yuv420p",
       "-f", "lavfi", "-i", "testsrc=s=640x360:d=2", "-f", "lavfi", "-i", "sine=f=440:d=6",
       "-filter_complex", "[0][1][2]concat=n=3:v=1:a=0[v]", "-map", "[v]", "-map", "3:a", "-c:v", "libx264", "-c:a", "aac",
       "-shortest", str(d / "clip.mp4"))
    ff("-f", "lavfi", "-i", "testsrc=s=800x600:d=1", "-frames:v", "1", str(d / "plain.jpg"))
    jpg = (d / "plain.jpg").read_bytes()               # a phone photo: location in EXIF (APP1) and a comment (COM)
    exif = b"Exif\x00\x00II*\x00\x08\x00\x00\x00\x00\x00GPS 42.2411,-83.6130"
    com = b"GPS 42.2411,-83.6130 taken at home"
    seg = lambda marker, body: marker + (len(body) + 2).to_bytes(2, "big") + body
    (d / "photo.jpg").write_bytes(jpg[:2] + seg(b"\xff\xe1", exif) + seg(b"\xff\xfe", com) + jpg[2:])
    (d / "page.html").write_text("<script>alert(1)</script>")
    return d


@pytest.fixture
def hub(tmp_path):
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_transcriber=FakeTranscriber(),
                     caption_threads=False, media_threads=False, caption_public_url="https://hub.example",
                     wake_check_url=False, allow_private_fetch=True)
    c = TestClient(app)
    toks = {h: c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": h}).json()["token"]
            for h in ("claude", "muse", "outsider")}
    return c, toks, app.state.media


def call(c, tok, name, args=None, session=None):
    h = {"Authorization": f"Bearer {tok}"}
    if session:
        h["X-Relay-Session"] = session
    r = c.post("/mcp", headers=h, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                         "params": {"name": name, "arguments": args or {}}})
    return r.json()["result"]


def ok(res):
    assert not res["isError"], res["content"][0]["text"]
    return res


def upload(c, tok, path):
    with open(path, "rb") as f:
        r = c.post("/artifacts", params={"name": os.path.basename(path)}, content=f.read(),
                   headers={"Authorization": f"Bearer {tok}"})
    return r.json()["id"]


def path_of(url):
    return url.split("https://hub.example", 1)[1]


def process(media):
    while media.run_once():
        pass


# ------------------------------------------------------------------ the whole loop
def test_a_video_travels_already_watched_and_the_agent_can_look_at_it(hub, files):
    c, t, media = hub
    art = upload(c, t["muse"], files / "clip.mp4")
    sent = ok(call(c, t["muse"], "ask", {"to": "claude", "message": "thoughts on this hook?", "wait_seconds": 0,
                                         "attach": [art]}, "muse-1"))
    first = ok(call(c, t["claude"], "check_replies", session="chat-A"))
    assert "Still being processed" in first["content"][0]["text"]                # it arrives at once
    process(media)
    mid = first["structuredContent"]["messages"][0]["media"][0]["id"]
    res = ok(call(c, t["claude"], "view_media", {"id": mid}, "chat-A"))
    text = res["content"][0]["text"]
    assert "[0:00.2] Wait for it" in text and "[0:03.0] THIS" in text
    assert "Shots start at: 0:00.0, 0:02.0, 0:04.0" in text and "https://hub.example/m/" in text
    pics = [x for x in res["content"] if x["type"] == "image"]
    assert len(pics) == 1 and base64.b64decode(pics[0]["data"])[:3] == b"\xff\xd8\xff"   # the contact sheet
    close = ok(call(c, t["claude"], "view_media", {"id": mid, "at": "0:03"}, "chat-A"))
    assert sum(x["type"] == "image" for x in close["content"]) == 3
    back = ok(call(c, t["claude"], "reply", {"message_id": sent["structuredContent"]["asked"], "text": "strong",
                                             "attach": [mid]}, "chat-A"))
    assert back["structuredContent"]["media"] == [mid]
    got = ok(call(c, t["muse"], "check_replies", session="muse-1"))
    assert "Transcript: [0:00.2] Wait for it" in got["content"][0]["text"]


def test_a_link_is_fetched_and_watched_by_the_hub(hub, files):
    c, t, media = hub
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(files))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/clip.mp4"
        ok(call(c, t["muse"], "ask", {"to": "claude", "message": "see", "wait_seconds": 0, "attach": [url]}, "m"))
        process(media)
    finally:
        srv.shutdown()
    card = ok(call(c, t["claude"], "check_replies", session="A"))["structuredContent"]["messages"][0]["media"][0]
    m = media.get(card["id"])
    assert m["status"] == "ready" and m["kind"] == "video" and m["digest"]["shots"] == [2.0, 4.0]


# ------------------------------------------------------------------ people: upload links
def test_a_person_uploads_from_a_link_and_it_reaches_only_the_chat_that_asked(hub, files):
    c, t, media = hub
    res = ok(call(c, t["claude"], "request_upload", {"note": "the clip from last night"}, "chat-A"))
    url = res["structuredContent"]["upload_url"]
    page = c.get(path_of(url))
    assert page.status_code == 200 and "Send files to claude" in page.text and "the clip from last night" in page.text
    assert "nonce-" in page.headers["content-security-policy"] and page.headers["x-robots-tag"].startswith("noindex")
    data = (files / "clip.mp4").read_bytes()
    base = path_of(url)
    s = c.post(base + "/start", json={"name": "clip.mp4", "size": len(data), "type": "video/mp4"}).json()
    half = len(data) // 2
    assert c.put(f"{base}/{s['file']}?offset=0", content=data[:half]).json()["received"] == half
    again = c.post(base + "/start", json={"name": "clip.mp4", "size": len(data)}).json()   # the connection dropped
    assert again == {**s, "received": half}
    assert c.put(f"{base}/{s['file']}?offset=0", content=data).json()["received"] == half  # wrong place: told where
    c.put(f"{base}/{s['file']}?offset={half}", content=data[half:])
    done = c.post(f"{base}/{s['file']}/done").json()
    process(media)
    assert ok(call(c, t["claude"], "check_replies", session="chat-B"))["structuredContent"]["messages"] == []
    got = ok(call(c, t["claude"], "check_replies", session="chat-A"))
    assert "arrived through your upload link" in got["content"][0]["text"] and done["media"] in got["content"][0]["text"]
    assert "Transcript:" in got["content"][0]["text"]


def test_photos_lose_their_location_data_and_the_original_is_not_kept(hub, files):
    c, t, media = hub
    url = ok(call(c, t["claude"], "request_upload", {}, "A"))["structuredContent"]["upload_url"]
    base, data = path_of(url), (files / "photo.jpg").read_bytes()
    assert b"GPS 42.2411" in data
    s = c.post(base + "/start", json={"name": "photo.jpg", "size": len(data)}).json()
    c.put(f"{base}/{s['file']}?offset=0", content=data)
    mid = c.post(f"{base}/{s['file']}/done").json()["media"]
    process(media)
    r = media.row(mid)
    assert b"GPS 42.2411" not in open(media.store.blob_path(r["artifact"]), "rb").read()
    assert not os.path.exists(media.store.blob_path(hashlib.sha256(data).hexdigest()))
    view = ok(call(c, t["claude"], "view_media", {"id": mid}, "A"))
    assert "location data removed" in view["content"][0]["text"] and view["content"][1]["type"] == "image"


def test_an_app_without_session_ids_gets_uploads_in_its_conversation(hub, files):
    c, t, media = hub
    res = ok(call(c, t["claude"], "request_upload", {}))                     # claude.ai: no session id
    thread, base = res["structuredContent"]["thread"], path_of(res["structuredContent"]["upload_url"])
    data = (files / "photo.jpg").read_bytes()
    s = c.post(base + "/start", json={"name": "p.jpg", "size": len(data)}).json()
    c.put(f"{base}/{s['file']}?offset=0", content=data)
    c.post(f"{base}/{s['file']}/done")
    plain = ok(call(c, t["claude"], "check_replies"))
    assert plain["structuredContent"]["messages"] == [] and plain["structuredContent"]["waiting_conversations"][0]["thread"] == thread
    assert "arrived through your upload link" in ok(call(c, t["claude"], "check_replies", {"thread": thread}))["content"][0]["text"]


# ------------------------------------------------------------------ people: viewer pages
def test_the_viewer_page_plays_on_any_phone_and_links_are_signed_and_expire(hub, files, monkeypatch):
    c, t, media = hub
    m = media.from_artifact("muse", upload(c, t["muse"], files / "clip.mp4"))
    process(media)
    link = path_of(media.link(m["id"]))
    page = c.get(link)
    assert page.status_code == 200 and f"src='{link}/file'" in page.text and "Wait for it" in page.text
    part = c.get(link + "/file", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["content-type"] == "video/mp4"
    assert c.get(link + "/sheet").headers["content-type"] == "image/jpeg"
    assert c.get(link[:-3] + "AAA").status_code == 404                        # tampered
    monkeypatch.setattr(media, "now", lambda: time.time() + M.LINK_SECONDS + 10)
    assert c.get(link).status_code == 410                                     # expired
    monkeypatch.undo()
    media.revoke("muse", m["id"])
    assert c.get(link).status_code == 404                                     # removed by who shared it


def test_a_web_page_is_never_shown_inline(hub, files):
    c, t, media = hub
    m = media.from_artifact("muse", upload(c, t["muse"], files / "page.html"))
    process(media)
    assert media.get(m["id"])["kind"] == "file"
    r = c.get(path_of(media.link(m["id"])) + "/file")
    assert r.headers["content-type"] == "application/octet-stream" and r.headers["content-disposition"].startswith("attachment")


# ------------------------------------------------------------------ who can see what
def test_only_people_in_the_conversation_can_see_or_forward_media(hub, files):
    c, t, media = hub
    art = upload(c, t["muse"], files / "photo.jpg")
    ok(call(c, t["muse"], "ask", {"to": "claude", "message": "pic", "wait_seconds": 0, "attach": [art]}, "m"))
    process(media)
    mid = ok(call(c, t["claude"], "check_replies", session="A"))["structuredContent"]["messages"][0]["media"][0]["id"]
    assert call(c, t["outsider"], "view_media", {"id": mid}, "o")["isError"]
    assert call(c, t["outsider"], "ask", {"to": "claude", "message": "x", "attach": [mid], "wait_seconds": 0}, "o")["isError"]
    bad = c.post("/messages", headers={"Authorization": f"Bearer {t['outsider']}"},
                 json={"channel": "x", "text": "y", "media": [mid]})
    assert bad.status_code == 400
    ok(call(c, t["claude"], "ask", {"to": "outsider", "message": "fwd", "attach": [mid], "wait_seconds": 0}, "A"))
    assert not call(c, t["outsider"], "view_media", {"id": mid}, "o")["isError"]   # now it was shared with them


def test_upload_links_expire_and_cap_their_files(hub, monkeypatch):
    c, t, media = hub
    base = path_of(ok(call(c, t["claude"], "request_upload", {}, "A"))["structuredContent"]["upload_url"])
    assert c.post(base + "/start", json={"name": "x", "size": media.max_upload + 1}).status_code == 400
    monkeypatch.setattr(media, "now", lambda: time.time() + M.UPLOAD_SECONDS + 10)
    assert c.get(base).status_code == 410
    assert c.post(base + "/start", json={"name": "x", "size": 10}).status_code == 410
    assert c.get("/up/" + "x" * 32).status_code == 404
