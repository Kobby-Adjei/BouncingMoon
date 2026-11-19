"""End to end over real HTTP: a hub, a requesting agent, and a worker node that
fetches a real video file and digests it. Nothing mocked."""
import functools
import http.server
import json
import os
import shutil
import socket
import socketserver
import subprocess
import threading
import time

import pytest
import uvicorn

from relay import cli
from relay.hub import create_app

ADMIN = "e2e-admin"
needs_media = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("yt-dlp")),
                                 reason="needs ffmpeg and yt-dlp")


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture
def live_hub(tmp_path):
    port = free_port()
    # the test serves media from 127.0.0.1, which the real guard rightly refuses
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN, lease_seconds=60,
                     allow_private_fetch=True)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True; t.join(5)


@pytest.fixture
def media_server(tmp_path):
    """A local web server holding a 4-shot test video, standing in for any public URL."""
    d = tmp_path / "web"; d.mkdir()
    clip = d / "sample.mp4"
    colors = ["red", "blue", "green", "yellow"]
    inputs, filt = [], ""
    for i, c in enumerate(colors):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=640x360:d=1.5:r=30"]
        filt += f"[{i}:v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs,
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                    "-filter_complex", f"{filt}concat=n=4:v=1:a=0[v]",
                    "-map", "[v]", "-map", "4:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(clip)], check=True)
    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(d))
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{port}/sample.mp4"
    httpd.shutdown()


@needs_media
def test_contact_my_agent_fetch_and_see(live_hub, media_server, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("RELAY_ALLOW_PRIVATE_FETCH", "1")
    admin = cli.Hub(live_hub, ADMIN)
    claude_tok = admin.post("/agents", {"handle": "claude"})["token"]
    node_tok = admin.post("/agents", {"handle": "laptop"})["token"]
    claude, laptop = cli.Hub(live_hub, claude_tok), cli.Hub(live_hub, node_tok)

    # 1. an agent that cannot download asks for the work by capability, not by name
    job = claude.post("/jobs", {"kind": "fetch", "params": {"url": media_server}})
    assert job["status"] == "queued"

    # 2. some machine that can, wakes up and does it
    n = cli.run_node(laptop, ["fetch", "digest"], once=True, poll=0.1,
                     work=str(tmp_path / "work"), log=print)
    assert n == 2                      # the fetch, then the digest it triggered

    # 3. the requester gets told, in its inbox, with the media attached by reference
    box = claude.get("/inbox")["messages"]
    texts = " | ".join(m["text"] for m in box)
    assert "picked up" in texts and "fetch" in texts and "digest" in texts
    fetched = claude.get(f"/jobs/{job['id']}")
    assert fetched["status"] == "done" and fetched["artifacts"]
    src = fetched["artifacts"][0]

    # 4. and it can now SEE the video without watching it
    meta = claude.get(f"/artifacts/{src}/meta")
    dig = meta["meta"]["digest"]["result"]
    assert dig["shots"] >= 3 and dig["has_audio"] and 5.5 < dig["duration"] < 6.5
    sheet = claude.download(dig["contact_sheet"], str(tmp_path / "sheet.jpg"))
    with open(sheet, "rb") as f:
        assert f.read(3) == b"\xff\xd8\xff"            # a real JPEG
    d = json.load(open(claude.download(dig["digest"], str(tmp_path / "digest.json"))))
    assert d["specs"]["width"] == 640 and len(d["shots"]) == dig["shots"]


needs_ffmpeg = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                  reason="needs ffmpeg")


def test_contact_sheets_fill_their_rows():
    assert [cli.sheet_columns(n) for n in (1, 3, 4, 14, 13, 24, 60, 11)] == [1, 3, 4, 7, 7, 6, 6, 6]


def test_clip_rule_longest_shots_without_overlap():
    shots = [{"i": 0, "start": 0, "end": 2}, {"i": 1, "start": 2, "end": 9},
             {"i": 2, "start": 9, "end": 10.5}, {"i": 3, "start": 10.5, "end": 19.5},
             {"i": 4, "start": 19.5, "end": 22.5}, {"i": 5, "start": 22.5, "end": 28.5}]
    got = cli.pick_moments(shots, 28.5, 3, 6)
    # the 9 s, 7 s and 6 s shots; centred inside each, in film order, never overlapping
    assert [g["shot"] for g in got] == [1, 3, 5]
    assert [(g["start"], g["end"]) for g in got] == [(2.5, 8.5), (12.0, 18.0), (22.5, 28.5)]
    # a short video gives fewer clips rather than overlapping ones
    assert len(cli.pick_moments(shots[:2], 9, 3, 6)) == 1
    # a shot shorter than the clip starts the clip on its cut, kept inside the video
    assert cli.pick_moments([{"i": 0, "start": 7, "end": 9}], 9, 1, 6)[0]["start"] == 3
    # with a motion reading, the busiest window wins over the longest; the jump at a cut doesn't count
    act = [(t / 10, 9.0 if 19.5 <= t / 10 < 22.5 else 0.5) for t in range(285)] + [(9.0, 99.0)]
    busy = cli.pick_moments(shots, 28.5, 1, 6, sorted(act))
    assert busy[0]["shot"] == 4 and busy[0]["start"] == 19.5 and busy[0]["activity"] > 3
    for bad in ({"count": 0}, {"count": 6}, {"seconds": 5}, {"seconds": 21}, {"aspect": "1:1"},
                {"aspect": "9:16,drawtext=x"}, {"count": "three"}):
        with pytest.raises(cli.RelayError):
            cli.clip_params({"artifact": "a", **bad})


@needs_ffmpeg
def test_clip_cuts_vertical_clips_from_the_digest(live_hub, tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    src = tmp_path / "film.mp4"
    lens = [2, 7, 1.5, 9, 3, 6]                      # uneven shots, so the rule has choices
    colors = ["red", "navy", "yellow", "black", "white", "green"]
    inputs, filt = [], ""
    for i, (c, n) in enumerate(zip(colors, lens)):
        inputs += ["-f", "lavfi", "-i", f"color=c={c}:s=640x360:d={n}:r=24"]
        filt += f"[{i}:v]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex",
                    f"{filt}concat=n={len(lens)}:v=1:a=0[v]", "-map", "[v]", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(src)], check=True)
    admin = cli.Hub(live_hub, ADMIN)
    claude = cli.Hub(live_hub, admin.post("/agents", {"handle": "claude"})["token"])
    node = cli.Hub(live_hub, admin.post("/agents", {"handle": "studio"})["token"])
    art = claude.upload(str(src), kind="source")

    # the hub refuses bad requests before any node sees them
    for bad in ({"artifact": "nope"}, {"artifact": art["id"], "count": 9},
                {"artifact": art["id"], "aspect": "4:3"}):
        with pytest.raises(cli.RelayError, match="400"):
            claude.post("/jobs", {"kind": "clip", "params": bad})

    claude.post("/jobs", {"kind": "digest", "params": {"artifact": art["id"]}})
    assert cli.run_node(node, ["digest", "clip"], once=True, poll=0.1, work=str(tmp_path / "w"), log=print) == 1
    job = claude.post("/jobs", {"kind": "clip", "params": {"artifact": art["id"], "count": 3, "seconds": 6}})
    assert cli.run_node(node, ["digest", "clip"], once=True, poll=0.1, work=str(tmp_path / "w"), log=print) == 1

    done = claude.get(f"/jobs/{job['id']}")
    res = done["result"]
    assert done["status"] == "done" and res["shots_from"] == "digest"
    assert [(c["start"], c["end"]) for c in res["clips"]] == [(2.5, 8.5), (12.0, 18.0), (22.5, 28.5)]
    for c in res["clips"]:
        mp4 = claude.download(c["artifact"], str(tmp_path / f"{c['artifact']}.mp4"))
        spec = cli.probe(mp4)
        assert (spec["width"], spec["height"], spec["vcodec"], spec["has_audio"]) == (720, 1280, "h264", False)
        assert 5.8 < spec["duration"] < 6.3 and spec["size"] < 3_000_000
        with open(mp4, "rb") as f:                 # fast start: the index comes before the media
            head = f.read(4096)
        assert head.find(b"moov") != -1 and (head.find(b"mdat") == -1 or head.find(b"moov") < head.find(b"mdat"))
        poster = claude.download(c["poster"], str(tmp_path / f"{c['poster']}.jpg"))
        assert open(poster, "rb").read(3) == b"\xff\xd8\xff"
        assert claude.get(f"/artifacts/{c['artifact']}/meta")["parent"] == art["id"]
    # the requester is told, with the clips attached by reference
    box = " | ".join(m["text"] for m in claude.get("/inbox", limit=100)["messages"])
    assert f"job {job['id']} [clip] done" in box and "3 vertical clips" in box
    assert claude.get(f"/artifacts/{art['id']}/meta")["meta"]["clips"]["job"] == job["id"]
