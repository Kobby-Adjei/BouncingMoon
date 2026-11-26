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
    assert media.get(m["id"])["kind"] == "document"            # read as text, for agents
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


# ------------------------------------------------------------------ get_files: the actual files, by link
def test_get_files_gives_direct_links_to_the_exact_files_on_a_message(tmp_path, files):
    """Dev attaches the ad, the picture and the banner the old way (hub file ids); the agent needs the files."""
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_transcriber=FakeTranscriber(),
                     caption_threads=False, media_threads=True, caption_public_url="https://hub.example")
    c = TestClient(app)
    t = {h: c.post("/agents", headers={"Authorization": f"Bearer {ADMIN}"}, json={"handle": h}).json()["token"]
         for h in ("dev", "agent", "outsider")}
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=2560x1440:d=1",
                    "-frames:v", "1", str(tmp_path / "banner.png")], check=True)
    arts = [upload(c, t["dev"], p) for p in (files / "clip.mp4", files / "photo.jpg", tmp_path / "banner.png")]
    msg = c.post("/messages", headers={"Authorization": f"Bearer {t['dev']}"},
                 json={"channel": "direct", "text": "the ad, the picture and the banner", "mentions": ["agent"],
                       "artifacts": arts}).json()
    ok(call(c, t["agent"], "check_replies", session="A"))             # already read: get_files still works
    res = ok(call(c, t["agent"], "get_files", {"message_id": msg["id"]}, "A"))
    got = res["structuredContent"]["files"]
    assert [f["name"] for f in got] == ["clip.mp4", "photo.jpg", "banner.png"]
    # the link ends in the file's name (some posting services want a URL ending in .mp4)
    assert all(f["status"] == "ready" and f["download"].endswith("/original/" + f["name"]) for f in got)
    assert "Download the exact file: https://hub.example/m/" in res["content"][0]["text"]
    fetch = lambda f: c.get(path_of(f["download"]))
    video = fetch(got[0])
    assert video.status_code == 200 and video.content == (files / "clip.mp4").read_bytes()     # bit for bit
    assert video.headers["content-disposition"].startswith('attachment; filename="clip.mp4"')
    assert video.headers["content-type"] == "video/mp4"                  # its real type, for posting services
    assert got[0]["sha256"] == hashlib.sha256(video.content).hexdigest() and "sha256 " in res["content"][0]["text"]
    photo = fetch(got[1]).content
    assert photo[:3] == b"\xff\xd8\xff" and b"GPS 42.2411" not in photo                     # location gone
    assert fetch(got[1]).headers["content-type"] == "image/jpeg"
    plain = (files / "plain.jpg").read_bytes()               # only metadata blocks were cut: the picture is the same bytes
    assert photo[photo.index(b"\xff\xda"):] == plain[plain.index(b"\xff\xda"):]
    banner = tmp_path / "got.png"
    banner.write_bytes(fetch(got[2]).content)
    pixels = lambda p: subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(p), "-f", "md5", "-"],
                                      capture_output=True, text=True).stdout
    assert pixels(banner) == pixels(tmp_path / "banner.png")                                 # every pixel the same
    assert call(c, t["outsider"], "get_files", {"message_id": msg["id"]}, "o")["isError"]
    token = path_of(got[0]["download"]).split("/")[2]
    assert c.get(f"/m/{token[:-4]}AAAA/original/clip.mp4").status_code == 404                # tampered link
    assert c.get(f"/m/{token}/original").content == video.content                            # the short form too


def test_old_style_attachments_show_up_in_check_replies(hub, files):
    c, t, media = hub
    art = upload(c, t["muse"], files / "clip.mp4")
    c.post("/messages", headers={"Authorization": f"Bearer {t['muse']}"},
           json={"channel": "direct", "text": "here", "mentions": ["claude"], "artifacts": [art]})
    text = ok(call(c, t["claude"], "check_replies", session="A"))["content"][0]["text"]
    assert " m_" in text and "clip.mp4" in text and "Get the file: get_files" in text


def test_metadata_is_cut_from_photos_without_touching_a_pixel(files):
    jpg = (files / "plain.jpg").read_bytes()
    seg = lambda marker, body: marker + (len(body) + 2).to_bytes(2, "big") + body
    # a phone photo held sideways: orientation 6 and a location, little-endian EXIF, plus an XMP block
    exif = (b"Exif\x00\x00II*\x00\x08\x00\x00\x00" + (2).to_bytes(2, "little")
            + (0x0112).to_bytes(2, "little") + (3).to_bytes(2, "little") + (1).to_bytes(4, "little") + (6).to_bytes(4, "little")
            + (0x8825).to_bytes(2, "little") + (4).to_bytes(2, "little") + (1).to_bytes(4, "little") + (99).to_bytes(4, "little")
            + b"\x00\x00\x00\x00GPS 42.2411")
    xmp = b"http://ns.adobe.com/xap/1.0/\x00<x:xmpmeta>GPS 42.2411</x:xmpmeta>"
    icc = b"ICC_PROFILE\x00\x01\x01fake-profile"
    phone = jpg[:2] + seg(b"\xff\xe1", exif) + seg(b"\xff\xe1", xmp) + seg(b"\xff\xe2", icc) + jpg[2:]
    out = M.strip_jpeg(phone)
    assert b"GPS" not in out and b"xmpmeta" not in out and b"fake-profile" in out      # colour profile kept
    assert M._orientation(out[out.index(b"Exif"):]) == 6                               # still the right way up
    start = lambda b: b.index(b"\xff\xda")
    assert out[start(out):] == jpg[start(jpg):]                                         # the picture itself: same bytes
    assert M.strip_jpeg(b"not a jpeg") is None and M.strip_jpeg(jpg[:40]) is None
    # PNG: text, time and EXIF chunks go; everything else is copied as it was
    import struct, zlib
    chunk = lambda k, d: struct.pack(">I", len(d)) + k + d + struct.pack(">I", zlib.crc32(k + d))
    head = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
    body = chunk(b"IDAT", zlib.compress(b"\x00\x7f")) + chunk(b"IEND", b"")
    png = b"\x89PNG\r\n\x1a\n" + head + chunk(b"tEXt", b"Comment\x00GPS 42.2411") + chunk(b"eXIf", b"MM\x00*") + body
    assert M.strip_png(png) == b"\x89PNG\r\n\x1a\n" + head + body


# ------------------------------------------------------------------ documents are read, like videos are watched
def tiny_pdf(lines) -> bytes:
    """A real two-page PDF with text on each page (hand-built: no PDF library needed)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", None, "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    kids = []
    for text in lines:
        body = f"BT /F1 18 Tf 72 720 Td ({text}) Tj ET".encode()
        objs.append(f"<< /Length {len(body)} >>\nstream\n{body.decode()}\nendstream")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >>"
                    f" /Contents {len(objs)} 0 R >>")
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n{o}\nendobj\n".encode()
    x = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offs).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{x}\n%%EOF\n".encode()
    return bytes(out)


def office(path, parts):
    import zipfile
    with zipfile.ZipFile(path, "w") as z:
        for n, body in parts.items():
            z.writestr(n, body)


W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


@pytest.mark.skipif(not shutil.which("pdftotext"), reason="needs poppler")
def test_a_pdf_is_read_and_the_other_agent_can_read_all_of_it(hub, tmp_path):
    c, t, media = hub
    pdf = tmp_path / "Q3 plan.pdf"
    pdf.write_bytes(tiny_pdf(["Quarterly plan: ship the connector", "Page two: hire a designer"]))
    art = upload(c, t["muse"], pdf)
    ok(call(c, t["muse"], "ask", {"to": "claude", "message": "read this?", "wait_seconds": 0, "attach": [art]}, "muse-1"))
    process(media)
    got = ok(call(c, t["claude"], "check_replies", session="chat-A"))
    text = got["content"][0]["text"]
    assert "Q3 plan.pdf" in text and 'Starts: "Quarterly plan: ship the connector' in text
    assert "Read it all (2 pages," in text
    mid = got["structuredContent"]["messages"][0]["media"][0]["id"]
    res = ok(call(c, t["claude"], "view_media", {"id": mid}, "chat-A"))
    body = res["content"][0]["text"]
    assert "A PDF (2 pages" in body and "Page two: hire a designer" in body and "More:" not in body
    pics = [x for x in res["content"] if x["type"] == "image"]
    assert len(pics) == 1 and base64.b64decode(pics[0]["data"])[:3] == b"\xff\xd8\xff"     # its first page
    brief = media.brief(mid)
    assert brief["kind"] == "document" and brief["pages"] == 2 and brief["read"] and brief["poster"]
    page = c.get(path_of(media.link(mid)))
    assert "Page two: hire a designer" in page.text and "First page of" in page.text
    poster = c.get(path_of(media.link(mid)) + "/poster")
    assert poster.headers["content-type"] == "image/jpeg"                      # shown, not downloaded
    f = c.get(path_of(media.link(mid)) + "/file")
    assert f.headers["content-disposition"].startswith("attachment")           # the PDF itself downloads


def test_word_slides_and_text_files_are_read(hub, tmp_path):
    c, t, media = hub
    office(tmp_path / "brief.docx", {"word/document.xml":
        f'<w:document {W}><w:body><w:p><w:r><w:t>Launch brief</w:t></w:r></w:p>'
        f'<w:p><w:r><w:t xml:space="preserve">Budget: </w:t></w:r><w:r><w:t>$4,000 &amp; two weeks</w:t></w:r></w:p>'
        f'</w:body></w:document>'})
    office(tmp_path / "deck.pptx", {"ppt/slides/slide2.xml": "<p:sld><a:p><a:r><a:t>Second idea</a:t></a:r></a:p></p:sld>",
                                    "ppt/slides/slide1.xml": "<p:sld><a:p><a:r><a:t>Title slide</a:t></a:r></a:p></p:sld>"})
    (tmp_path / "numbers.csv").write_text("month,views\nJan,1200\nFeb,1800\n")
    (tmp_path / "notes.md").write_text("# Hooks\n\n- Wait for it\n- You won't believe\n")
    (tmp_path / "blob.bin").write_bytes(bytes(range(256)) * 4)
    got = {}
    for name in ("brief.docx", "deck.pptx", "numbers.csv", "notes.md", "blob.bin"):
        got[name] = media.from_artifact("muse", upload(c, t["muse"], tmp_path / name))["id"]
    process(media)
    assert media.doc_text(got["brief.docx"]) == "Launch brief\nBudget: $4,000 & two weeks"
    assert media.doc_text(got["deck.pptx"]) == "Slide 1\nTitle slide\n\nSlide 2\nSecond idea"
    assert media.get(got["deck.pptx"])["digest"]["pages"] == 2
    assert media.doc_text(got["numbers.csv"]).startswith("month,views\nJan,1200")
    assert media.get(got["notes.md"])["kind"] == "document"
    assert media.get(got["blob.bin"])["kind"] == "file"                        # not text: kept as a download
    assert "Read it all" not in media.card(media.get(got["blob.bin"]))


def test_a_long_document_is_read_in_parts(hub, tmp_path):
    c, t, media = hub
    lines = "".join(f"Line {i}: " + "words " * 12 + "\n" for i in range(800))     # ~70,000 characters
    (tmp_path / "long.txt").write_text(lines)
    art = upload(c, t["muse"], tmp_path / "long.txt")
    ok(call(c, t["muse"], "ask", {"to": "claude", "message": "long one", "wait_seconds": 0, "attach": [art]}, "muse-1"))
    process(media)
    mid = ok(call(c, t["claude"], "check_replies", session="A"))["structuredContent"]["messages"][0]["media"][0]["id"]
    seen, at = "", None
    for _ in range(10):
        res = ok(call(c, t["claude"], "view_media", {"id": mid, **({"at": at} if at else {})}, "A"))["content"][0]["text"]
        part = res.split(":\n\n", 1)[1].split("\n\nMore:")[0].split("\nOpen/download")[0]
        seen += part if part.endswith("\n") else part + "\n"
        m = __import__("re").search(r'with at "(\d+)"', res)
        if not m:
            break
        at = m.group(1)
    assert seen == lines                                                      # every line once, none cut in half


def test_an_agent_sends_a_document_it_wrote_as_a_real_file(hub):
    c, t, media = hub
    plan = "# Launch plan\n\n1. Film the hook\n2. Post Tuesday\n"
    sent = ok(call(c, t["claude"], "ask", {"to": "muse", "message": "Here's the plan", "wait_seconds": 0,
                                           "files": [{"name": "plan.md", "content": plan},
                                                     {"name": "budget", "content": "item,cost\nads,400\n"}]}, "A"))
    process(media)
    got = ok(call(c, t["muse"], "check_replies", session="m"))
    msg = got["structuredContent"]["messages"][0]
    assert msg["text"] == "Here's the plan"                                       # the text stays short
    names = [x["name"] for x in msg["media"]]
    assert names == ["plan.md", "budget.txt"]
    assert 'Starts: "# Launch plan 1. Film the hook' in got["content"][0]["text"]
    mid = msg["media"][0]["id"]
    assert plan in ok(call(c, t["muse"], "view_media", {"id": mid}, "m"))["content"][0]["text"]
    original = c.get(path_of(media.link(mid)) + "/original")                   # a person downloads the exact file
    assert original.content.decode() == plan and "plan.md" in original.headers["content-disposition"]
    too_big = call(c, t["claude"], "reply", {"message_id": sent["structuredContent"]["asked"], "text": "x",
                                             "files": [{"name": "big.txt", "content": "a" * (2 * 2**20 + 1)}]}, "A")
    assert too_big["isError"] and "too long" in too_big["content"][0]["text"]
    bad = call(c, t["claude"], "reply", {"message_id": sent["structuredContent"]["asked"], "text": "x",
                                         "files": [{"name": "", "content": "y"}]}, "A")
    assert bad["isError"]


def test_a_person_attaches_a_file_from_the_app(tmp_path):
    """The app's paperclip: upload the bytes, make it media, send it with or without words."""
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN, caption_threads=False, media_threads=False)
    c, media = TestClient(app), app.state.media
    H = lambda tok: {"Authorization": f"Bearer {tok}"}
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    art = c.post("/artifacts", params={"name": "notes.md", "mime": "text/markdown"}, content=b"# Notes\nShip Friday\n",
                 headers=H(kobby)).json()["id"]
    mid = c.post("/media", headers=H(kobby), json={"artifact": art}).json()["id"]
    sent = c.post("/messages", headers=H(kobby), json={"channel": "direct", "text": "", "mentions": ["claude"], "media": [mid]})
    assert sent.status_code == 200, sent.text
    process(media)
    convs = [x for x in c.get("/threads", headers=H(kobby)).json() if x["last"]["from"] == "kobby"]
    assert convs[0]["last"]["files"] == 1                                   # the list says "Sent a file", not nothing
    got = ok(call(c, claude, "check_replies", session="A"))
    assert 'notes.md' in got["content"][0]["text"] and 'Starts: "# Notes Ship Friday"' in got["content"][0]["text"]
    shown = c.get(f"/messages?thread={sent.json()['thread']}", headers=H(kobby)).json()
    card = shown[-1]["media"][0]
    assert card["kind"] == "document" and card["read"] and card["words"] == 4


def test_an_agent_that_pastes_a_document_is_told_to_send_files(hub):
    c, t, media = hub
    doc = "# Plan\n\n## Week 1\nFilm.\n\n## Week 2\nEdit.\n\n| day | task |\n| --- | --- |\n" + "| mon | shoot |\n" * 80
    pasted = ok(call(c, t["claude"], "ask", {"to": "muse", "message": doc, "wait_seconds": 0}, "A"))
    assert "Tip: that message was a whole document" in pasted["content"][0]["text"]
    short = ok(call(c, t["claude"], "reply", {"message_id": pasted["structuredContent"]["asked"], "text": "Thanks, will do."}, "A"))
    assert "Tip:" not in short["content"][0]["text"]
    sent = ok(call(c, t["claude"], "reply", {"message_id": pasted["structuredContent"]["asked"], "text": "As a file:",
                                             "files": [{"name": "plan.md", "content": doc}]}, "A"))
    assert "Tip:" not in sent["content"][0]["text"]
    tools = c.post("/mcp", headers={"Authorization": f"Bearer {t['claude']}"},
                   json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]
    for name in ("ask", "reply"):
        tool = next(x for x in tools if x["name"] == name)
        assert "`files`" in tool["description"] and "files" in tool["inputSchema"]["properties"]
