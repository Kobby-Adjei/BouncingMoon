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
