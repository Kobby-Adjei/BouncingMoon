"""Caption clip: vertical shorts with word-by-word captions, made on the hub.

Real ffmpeg on small generated videos; a fake transcriber that returns word timings (the real speech
model is not installed where the tests run)."""
import http.server
import json
import os
import re
import socket
import subprocess
import threading
import time

import pytest
from fastapi.testclient import TestClient

from relay import captions
from relay.hub import create_app, private_host

ADMIN = "admin-test-token"
WORDS = "bro he just crashed the car into the police station no way".split()


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


class FakeTranscriber(captions.Transcriber):
    """Says WORDS from 0.2 s, one every 0.25 s, whatever the audio."""

    def __init__(self):
        self.calls = []

    def words(self, pcm_path):
        self.calls.append(os.path.getsize(pcm_path))
        return [{"start": 0.2 + i * 0.25, "end": 0.2 + i * 0.25 + 0.2, "word": " " + w + ("." if w == "station" else "")}
                for i, w in enumerate(WORDS)]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    out = {}
    out["test"] = str(d / "test.mp4")          # 1280x720, 4 s, a tone
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=4", "-f", "lavfi", "-i",
           "sine=frequency=440:duration=4", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out["test"])
    out["halves"] = str(d / "halves.mp4")      # left half red, right half blue, no audio
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=640x720:d=1.5", "-f", "lavfi", "-i", "color=c=blue:s=640x720:d=1.5",
           "-filter_complex", "hstack", "-c:v", "libx264", "-pix_fmt", "yuv420p", out["halves"])
    out["dark"] = str(d / "dark.mp4")          # plain dark picture with a tone: captions stand out
    ffmpeg("-f", "lavfi", "-i", "color=c=0x202838:s=1280x720:d=3", "-f", "lavfi", "-i", "sine=duration=3",
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out["dark"])
    return out


@pytest.fixture
def make(tmp_path):
    n = [0]

    def _make(**kw):
        n[0] += 1
        d = kw.pop("data_dir", None) or str(tmp_path / f"hub{n[0]}")
        fake = kw.pop("transcriber", None) or FakeTranscriber()
        kw.setdefault("caption_threads", False)
        app = create_app(data_dir=d, admin_token=ADMIN, caption_transcriber=fake,
                         caption_public_url="https://hub.example", **kw)
        client = TestClient(app)
        r = client.post("/agents", headers=H(ADMIN), json={"handle": "kobby"})
        tok = r.json()["token"] if r.status_code == 200 else None
        return client, tok, app.state.captioner, fake, d
    return _make


def upload(client, tok, path, name="clip.mp4"):
    with open(path, "rb") as f:
        r = client.post(f"/artifacts?name={name}&mime=video/mp4", headers=H(tok), content=f.read())
    assert r.status_code == 200, r.text
    return r.json()["id"]


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                       capture_output=True, text=True, timeout=60)
    return json.loads(p.stdout)


def frame(path, t, w=1080, h=1920):
    p = subprocess.run(["ffmpeg", "-v", "error", "-ss", str(t), "-i", path, "-frames:v", "1", "-f", "rawvideo",
                        "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=60)
    assert len(p.stdout) == w * h * 3
    return p.stdout


def short(client, tok, cap, tmp_path, **body):
    r = client.post("/captions", headers=H(tok), json=body)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "queued"
    assert cap.run_once()
    s = client.get(f"/captions/{r.json()['id']}", headers=H(tok)).json()
    assert s["status"] == "done", s["error"]
    out = str(tmp_path / f"{s['id']}.mp4")
    with open(out, "wb") as f:
        f.write(client.get(s["share_path"]).content)
    return s, out


# --------------------------------------------------------------------------- the video
def test_a_short_is_vertical_h264_with_audio_and_trimmed(make, media, tmp_path):
    client, tok, cap, fake, _ = make()
    fid = upload(client, tok, media["test"])
    s, out = short(client, tok, cap, tmp_path, file_id=fid, start="0:01", end=3.5)
    info = probe(out)
    v = next(x for x in info["streams"] if x["codec_type"] == "video")
    a = next(x for x in info["streams"] if x["codec_type"] == "audio")
    assert (v["codec_name"], v["width"], v["height"], v["pix_fmt"]) == ("h264", 1080, 1920, "yuv420p")
    assert a["codec_name"] == "aac"
    assert abs(float(info["format"]["duration"]) - 2.5) < 0.15 and abs(s["duration"] - 2.5) < 0.15
    # only the trimmed 2.5 s was transcribed (16 kHz mono 16-bit)
    assert abs(fake.calls[0] - 2.5 * 16000 * 2) < 16000
    assert s["width"] == 1080 and s["height"] == 1920 and s["captions"] is True
    assert s["transcript"].startswith("bro he just") and s["source"] == fid
    # stored as an artifact of the source, and the hub can list it
    meta = client.get(f"/artifacts/{fid}/meta", headers=H(tok)).json()
    assert [d["kind"] for d in meta["derived"]] == ["short"] and meta["derived"][0]["mime"] == "video/mp4"
    assert [x["id"] for x in client.get("/captions", headers=H(tok)).json()] == [s["id"]]


def test_captions_are_burned_into_the_middle_with_the_spoken_word_yellow(make, media, tmp_path):
    client, tok, cap, _, _ = make()
    s, out = short(client, tok, cap, tmp_path, file_id=upload(client, tok, media["dark"]))

    def counts(t):
        px = frame(out, t)
        rows = {"middle": range(880, 1040), "top": range(200, 360)}
        res = {}
        for k, rr in rows.items():
            yellow = white = 0
            for y in rr:
                row = px[y * 1080 * 3:(y + 1) * 1080 * 3]
                for x in range(0, 1080 * 3, 9):
                    r, g, b = row[x], row[x + 1], row[x + 2]
                    if r > 200 and g > 180 and b < 90:
                        yellow += 1
                    elif r > 220 and g > 220 and b > 220:
                        white += 1
            res[k] = (yellow, white)
        return res

    during, after = counts(0.5), counts(2.95)        # words run 0.2 s - 3.15 s; the clip is 3 s
    assert during["middle"][0] > 200 and during["middle"][1] > 200     # a yellow word beside white ones
    assert during["top"] == (0, 0)                                      # nothing at the top: it's centred
    silent = counts(0.05)                                               # before the first word: no caption
    assert silent["middle"] == (0, 0)
    assert after["middle"][0] + after["middle"][1] > 0


def test_ass_is_centred_three_words_at_most_and_highlights_each_word_in_turn():
    words = [{"start": i * 0.3, "end": i * 0.3 + 0.25, "word": f" {w}"} for i, w in enumerate(WORDS)]
    words.insert(3, {"start": 0.75, "end": 0.85, "word": " {\\b1}evil\\N"})        # override tags can't get in
    ass = captions.build_ass(words, 5.0, "Montserrat ExtraBold", False)
    styles = {l.split(",")[0][7:]: l.split(",") for l in ass.splitlines() if l.startswith("Style: ")}
    fmt = [f.strip() for f in next(l for l in ass.splitlines() if l.startswith("Format: Name"))[8:].split(",")]
    word = dict(zip(fmt, styles["Word"]))
    assert word["Alignment"] == "5" and word["Fontname"] == "Montserrat ExtraBold"   # 5 = middle centre
    assert int(word["Outline"]) >= 6 and word["PrimaryColour"] == "&H00FFFFFF"
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    events = [l for l in ass.splitlines() if l.startswith("Dialogue: 1,")]
    lit = []
    for e in events:
        text = e.split(",", 9)[9]
        assert "\\an5" in text
        shown = re.sub(r"\{[^}]*\}", "", text).split()
        assert 1 <= len(shown) <= 3 and all(w == w.upper() for w in shown)
        hl = re.findall(r"\{\\c" + re.escape(captions.HIGHLIGHT) + r"\}([^{]+)\{", text)
        assert len(hl) == 1
        lit.append(hl[0])
    assert lit == [w.upper() for w in WORDS[:3]] + ["B1EVILN"] + [w.upper() for w in WORDS[3:]]
    assert "\\b1" not in ass and "evil\\N" not in ass
    starts = [e.split(",")[1] for e in events]
    assert starts == sorted(starts)
    # a pause or the end of a sentence starts a new caption
    groups = captions.group_words([{"start": 0, "end": .2, "word": " no"}, {"start": .2, "end": .4, "word": " way."},
                                   {"start": .5, "end": .7, "word": " lets"}, {"start": 3, "end": 3.2, "word": " go"}])
    assert [[w["text"] for w in g] for g in groups] == [["NO", "WAY"], ["LETS"], ["GO"]]


def test_crop_slices_left_centre_right(make, media, tmp_path):
    assert captions.crop_box(1280, 720, 0.0) == (404, 720, 0, 0)
    assert captions.crop_box(1280, 720, 0.5) == (404, 720, 438, 0)
    assert captions.crop_box(1280, 720, 1.0) == (404, 720, 876, 0)
    assert captions.crop_box(1920, 1080, 0.5) == (606, 1080, 656, 0)
    assert captions.crop_box(1080, 1920, 0.5) == (1080, 1920, 0, 0)            # already vertical
    assert captions.crop_box(1080, 2400, 0.5) == (1080, 1920, 0, 240)        # taller: the middle
    for v, want in [("center", 0.5), ("left", 0), ("right", 1), (0.25, 0.25), ("0.8", 0.8), (None, 0.5)]:
        assert captions.crop_position(v)[1] == want
    for bad in ["top", 2, -0.1, True]:
        with pytest.raises(captions.CaptionError):
            captions.crop_position(bad)
    client, tok, cap, _, _ = make()
    fid = upload(client, tok, media["halves"])

    def colour(crop):
        _, out = short(client, tok, cap, tmp_path, file_id=fid, crop=crop)
        px = frame(out, 0.5)
        y = 400                                      # above the captions band
        r = sum(px[(y * 1080 + x) * 3] for x in range(0, 1080, 4))
        b = sum(px[(y * 1080 + x) * 3 + 2] for x in range(0, 1080, 4))
        return r, b, out
    r, b, out = colour("left")
    assert r > 10 * b                                # all red
    r, b, _ = colour("right")
    assert b > 10 * r                                # all blue
    r, b, _ = colour("center")
    assert 0.5 < r / b < 2                           # half and half
    # the source had no audio: the short still has an AAC track, and no captions were made up
    assert any(s["codec_name"] == "aac" for s in probe(out)["streams"])


def test_long_requests_are_capped(make, media, tmp_path, monkeypatch):
    monkeypatch.setattr(captions, "MAX_SECONDS", 2.0)
    client, tok, cap, _, _ = make()
    s, out = short(client, tok, cap, tmp_path, file_id=upload(client, tok, media["test"]))
    assert abs(float(probe(out)["format"]["duration"]) - 2.0) < 0.15 and s["trimmed_to_max"] is True


# --------------------------------------------------------------------------- inputs
def test_times_and_bad_requests(make, media):
    assert [captions.seconds(v) for v in (None, "", 75, "75.5", "1:15", "1:02:03", "0:01.5")] == \
        [None, None, 75.0, 75.5, 75.0, 3723.0, 1.5]
    for bad in ("-3", "1:75", "abc", "1.5:00", "1:2:3:4"):
        with pytest.raises(captions.CaptionError):
            captions.seconds(bad)
    client, tok, cap, _, _ = make()
    fid = upload(client, tok, media["test"])
    for body, says in [({}, "either"), ({"url": "https://x.example/a.mp4", "file_id": fid}, "either"),
                       ({"file_id": "nope"}, "no file"), ({"file_id": fid, "start": 5, "end": 2}, "after start"),
                       ({"file_id": fid, "crop": "top"}, "crop"), ({"url": "ftp://x.example/a.mp4"}, "http"),
                       ({"file_id": fid, "start": "9:99"}, "time")]:
        r = client.post("/captions", headers=H(tok), json=body)
        assert r.status_code == 400 and says in r.json()["detail"].lower(), (body, r.text)
    assert client.post("/captions", headers=H(ADMIN), json={"file_id": fid}).status_code == 403
    assert client.post("/captions", json={"file_id": fid}).status_code == 401
    # start past the end fails the job in plain words
    r = client.post("/captions", headers=H(tok), json={"file_id": fid, "start": 30}).json()
    cap.run_once()
    s = client.get(f"/captions/{r['id']}", headers=H(tok)).json()
    assert s["status"] == "failed" and "past the end" in s["error"]
    assert client.get("/captions/c_000000000000", headers=H(tok)).status_code == 404


def test_youtube_links_are_refused_plainly(make):
    client, tok, _, _, _ = make()
    for u in ("https://www.youtube.com/watch?v=qSyo5d-dVzA", "https://youtu.be/qSyo5d-dVzA", "youtube.com/shorts/x",
              "https://m.youtube.com/live/qSyo5d-dVzA"):
        r = client.post("/captions", headers=H(tok), json={"url": u})
        assert r.status_code == 400 and r.json()["detail"] == captions.YOUTUBE_MSG, u


def test_drive_and_dropbox_links_become_direct_downloads():
    d = "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    for u in ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing",
              "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download"):
        assert captions.direct_url(u) == d, u
    assert captions.direct_url("https://www.dropbox.com/s/abc123/clip.mp4?dl=0") == "https://www.dropbox.com/s/abc123/clip.mp4?dl=1"
    assert captions.direct_url("https://www.dropbox.com/scl/fi/xyz/clip.mp4?rlkey=k1&dl=0") == \
        "https://www.dropbox.com/scl/fi/xyz/clip.mp4?rlkey=k1&dl=1"
    assert captions.direct_url("https://www.dropbox.com/s/abc/clip.mp4").endswith("?dl=1")
    assert captions.direct_url("https://cdn.example.com/a.mp4?sig=1") == "https://cdn.example.com/a.mp4?sig=1"
    with pytest.raises(captions.CaptionError):
        captions.direct_url("https://drive.google.com/drive/folders")
    # a result never repeats a link's query, where share keys and signatures live
    assert captions.shown_url("https://www.dropbox.com/scl/fi/xyz/clip.mp4?rlkey=secret&dl=1") == \
        "https://www.dropbox.com/scl/fi/xyz/clip.mp4"


def test_ssrf_guard(make, monkeypatch):
    client, tok, _, _, _ = make()
    for u in ("http://127.0.0.1/a.mp4", "http://169.254.169.254/latest/meta-data", "http://localhost:8787/x.mp4",
              "http://[::1]/a.mp4", "http://10.1.2.3/a.mp4", "http://intranet/a.mp4"):
        r = client.post("/captions", headers=H(tok), json={"url": u})
        assert r.status_code == 400 and "private" in r.json()["detail"], u
    # a public-looking name that resolves inside
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))])
    with pytest.raises(captions.CaptionError, match="private"):
        captions.check_host("sneaky.example.com", private_host)
    monkeypatch.undo()

    # a server that redirects to the cloud metadata address: every hop is checked
    srv, port = serve({"/go.mp4": (302, {"Location": "http://169.254.169.254/latest"}, b"")})
    try:
        monkeypatch.setattr(captions, "_public", lambda ip: True)       # let the first hop (this machine) through
        opener = captions.guarded_opener(private_host, allow_private=False)
        with pytest.raises(captions.CaptionError, match="private"):
            opener.open(f"http://127.0.0.1:{port}/go.mp4", timeout=5)
        monkeypatch.undo()
        # and without that, the connection itself is refused: it reached a private address
        with pytest.raises(captions.CaptionError, match="private"):
            captions.guarded_opener(private_host, allow_private=False).open(f"http://127.0.0.1:{port}/go.mp4", timeout=5)
    finally:
        srv.shutdown()


def serve(routes):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, headers, body = routes.get(self.path.split("?")[0], (404, {}, b"no"))
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def test_a_link_is_downloaded_with_limits(make, media, tmp_path):
    body = open(media["test"], "rb").read()
    srv, port = serve({"/clip.mp4": (200, {"Content-Type": "video/mp4"}, body),
                       "/page": (200, {"Content-Type": "text/html"}, b"<html>sign in</html>"),
                       "/moved.mp4": (301, {"Location": "/clip.mp4"}, b"")})
    try:
        base = f"http://127.0.0.1:{port}"
        with pytest.raises(captions.CaptionError, match="web page"):
            captions.download(base + "/page", str(tmp_path / "x"), private_host, allow_private=True)
        with pytest.raises(captions.CaptionError, match="over"):
            captions.download(base + "/clip.mp4", str(tmp_path / "x"), private_host, allow_private=True, cap=1000)
        with pytest.raises(captions.CaptionError, match="404"):
            captions.download(base + "/gone.mp4", str(tmp_path / "x"), private_host, allow_private=True)
        client, tok, cap, _, _ = make(allow_private_fetch=True)
        s, out = short(client, tok, cap, tmp_path, url=base + "/moved.mp4?token=abc", end=2)
        assert probe(out)["streams"][0]["width"] == 1080 and s["source"] == base + "/moved.mp4"
        assert "abc" not in json.dumps(s)
    finally:
        srv.shutdown()


# --------------------------------------------------------------------------- share links
def test_share_link_works_without_a_key_expires_and_cannot_be_guessed(make, media, tmp_path):
    client, tok, cap, _, _ = make()
    fid = upload(client, tok, media["test"])
    s, out = short(client, tok, cap, tmp_path, file_id=fid, end=2)
    token = s["share_path"][3:-4]
    assert len(token) >= 43 and s["share_url"] == "https://hub.example" + s["share_path"]
    assert token not in (fid, s["artifact"]) and s["artifact"] not in s["share_path"]
    r = client.get(s["share_path"])                                   # no Authorization header
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    assert r.headers["accept-ranges"] == "bytes" and r.headers["x-content-type-options"] == "nosniff"
    part = client.get(s["share_path"], headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and part.content == r.content[:100]
    # nothing else opens: not the artifact id, not a near miss, not a made-up token
    for bad in (f"/s/{s['artifact']}.mp4", f"/s/{fid}.mp4", f"/s/{token[:-1]}x.mp4", f"/s/{token}",
                "/s/" + "A" * 43 + ".mp4", "/s/..%2Fhub.py"):
        assert client.get(bad).status_code == 404, bad
    with pytest.raises(LookupError):
        cap.shared_file(fid)
    # 7 days later it's gone, and the status says so
    real = cap.now
    cap.now = lambda: real() + 7 * 86400 + 5
    assert client.get(s["share_path"]).status_code == 410
    later = client.get(f"/captions/{s['id']}", headers=H(tok)).json()
    assert later["share_expired"] is True and later["share_url"] is None and later["share_path"] is None


# --------------------------------------------------------------------------- agents
def rpc(client, tok, name, args):
    r = client.post("/mcp", headers=H(tok), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                  "params": {"name": name, "arguments": args}})
    assert r.status_code == 200, r.text
    return r.json()["result"]


def test_agents_make_a_short_through_the_connector(make, media):
    client, tok, cap, _, _ = make(caption_threads=True, caption_tool_wait=40, caption_idle_seconds=0.2)
    muse = client.post("/agents", headers=H(ADMIN), json={"handle": "muse"}).json()["token"]
    tools = {t["name"]: t for t in client.post("/mcp", headers=H(muse), json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()["result"]["tools"]}
    assert {"caption_clip", "clip_status", "watch_stream"} <= set(tools)
    desc = tools["caption_clip"]["description"]
    assert "YouTube" in desc and "approve" in desc and "file" in desc.lower()
    bad = rpc(client, muse, "caption_clip", {"url": "https://youtu.be/qSyo5d-dVzA"})
    assert bad["isError"] and bad["content"][0]["text"] == captions.YOUTUBE_MSG
    fid = upload(client, tok, media["test"])
    res = rpc(client, muse, "caption_clip", {"file_id": fid, "start": 1, "end": "0:03", "crop": "right"})
    assert not res["isError"], res
    text, data = res["content"][0]["text"], res["structuredContent"]
    assert data["status"] == "done" and data["share_url"] in text and "Transcript: bro he just" in text
    assert "person" in text and abs(data["duration"] - 2.0) < 0.15 and data["crop"] == "right"
    assert ADMIN not in text and tok not in text and muse not in json.dumps(data)
    st = rpc(client, muse, "clip_status", {"id": data["id"]})
    assert st["structuredContent"]["share_url"] == data["share_url"]
    assert rpc(client, muse, "clip_status", {"id": "c_nope"})["isError"]
    assert client.get(data["share_path"]).status_code == 200
    # everyone's own list; the owner would see all
    assert client.get("/captions", headers=H(tok)).json() == []
    assert [x["id"] for x in client.get("/captions", headers=H(muse)).json()] == [data["id"]]
    deadline = time.time() + 5                       # the idle worker lets go of the model and stops
    while cap.thread is not None and time.time() < deadline:
        time.sleep(0.1)
    assert cap.thread is None


def test_a_restart_requeues_a_short_once_then_fails_it_plainly(make, media, tmp_path):
    client, tok, cap, _, d = make()
    fid = upload(client, tok, media["test"])
    cid = client.post("/captions", headers=H(tok), json={"file_id": fid, "end": 2}).json()["id"]
    with cap.store.tx() as c:                        # the hub died while this was being made
        c.execute("UPDATE captions SET status='working', stage='cutting', attempts=1 WHERE id=?", (cid,))
    os.makedirs(os.path.join(cap.store.tmp, "captions", cid))
    client2, _, cap2, _, _ = make(data_dir=d)
    s = client2.get(f"/captions/{cid}", headers=H(tok)).json()
    assert s["status"] == "queued" and "restarted" in s["error"]
    assert not os.path.exists(os.path.join(cap2.store.tmp, "captions", cid))     # leftovers cleaned up
    assert cap2.run_once()
    assert client2.get(f"/captions/{cid}", headers=H(tok)).json()["status"] == "done"
    # a second crash on the same short fails it, saying why
    cid2 = client2.post("/captions", headers=H(tok), json={"file_id": fid, "end": 2}).json()["id"]
    with cap2.store.tx() as c:
        c.execute("UPDATE captions SET status='working', attempts=2 WHERE id=?", (cid2,))
    client3, _, _, _, _ = make(data_dir=d)
    s = client3.get(f"/captions/{cid2}", headers=H(tok)).json()
    assert s["status"] == "failed" and "restarted twice" in s["error"]
