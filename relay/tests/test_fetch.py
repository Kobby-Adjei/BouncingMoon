"""The safe download media uses for links: direct links from share pages, YouTube refused plainly,
nothing private reachable (even through a redirect), and limits on what is saved."""
import http.server
import socket
import threading

import pytest

from relay import captions
from relay.hub import private_host


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


def test_youtube_links_are_refused_plainly():
    for u in ("https://www.youtube.com/watch?v=qSyo5d-dVzA", "https://youtu.be/qSyo5d-dVzA", "youtube.com/shorts/x",
              "https://m.youtube.com/live/qSyo5d-dVzA"):
        with pytest.raises(captions.CaptionError, match="YouTube"):
            captions.direct_url(u)


def test_drive_and_dropbox_links_become_direct_downloads():
    d = "https://drive.usercontent.google.com/download?id=1AbCdEfGhIjKlMnOp&export=download&confirm=t"
    for u in ("https://drive.google.com/file/d/1AbCdEfGhIjKlMnOp/view?usp=sharing",
              "https://drive.google.com/open?id=1AbCdEfGhIjKlMnOp", "https://drive.google.com/uc?id=1AbCdEfGhIjKlMnOp&export=download"):
        assert captions.direct_url(u) == d, u
    assert captions.direct_url("https://www.dropbox.com/s/abc123/clip.mp4?dl=0") == "https://www.dropbox.com/s/abc123/clip.mp4?dl=1"
    assert captions.direct_url("https://www.dropbox.com/s/abc/clip.mp4").endswith("?dl=1")
    assert captions.direct_url("https://cdn.example.com/a.mp4?sig=1") == "https://cdn.example.com/a.mp4?sig=1"
    with pytest.raises(captions.CaptionError):
        captions.direct_url("https://drive.google.com/drive/folders")
    assert captions.shown_url("https://www.dropbox.com/scl/fi/xyz/clip.mp4?rlkey=secret&dl=1") == \
        "https://www.dropbox.com/scl/fi/xyz/clip.mp4"


def test_nothing_private_is_reachable(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda h, *a, **k: [(2, 1, 6, "", ("10.0.0.7", 0))])
    with pytest.raises(captions.CaptionError, match="private"):
        captions.check_host("sneaky.example.com", private_host)
    monkeypatch.undo()
    # a server that redirects to the cloud metadata address: every hop is checked
    srv, port = serve({"/go.mp4": (302, {"Location": "http://169.254.169.254/latest"}, b"")})
    try:
        monkeypatch.setattr(captions, "_public", lambda ip: True)       # let the first hop (this machine) through
        with pytest.raises(captions.CaptionError, match="private"):
            captions.guarded_opener(private_host, allow_private=False).open(f"http://127.0.0.1:{port}/go.mp4", timeout=5)
        monkeypatch.undo()
        with pytest.raises(captions.CaptionError, match="private"):
            captions.guarded_opener(private_host, allow_private=False).open(f"http://127.0.0.1:{port}/go.mp4", timeout=5)
    finally:
        srv.shutdown()


def test_a_link_is_downloaded_with_limits(tmp_path):
    body = b"\x00\x00\x00\x18ftypmp42" + b"x" * 5000
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
        captions.download(base + "/moved.mp4", str(tmp_path / "ok"), private_host, allow_private=True)
        assert (tmp_path / "ok").read_bytes() == body
    finally:
        srv.shutdown()
