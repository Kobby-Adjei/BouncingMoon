"""Watching a YouTube live stream's chat from the hub, with a fake YouTube API (no network)."""
import io
import json
import urllib.error
import urllib.parse

import pytest
from fastapi.testclient import TestClient

from relay import streams
from relay.hub import create_app

ADMIN = "admin-test-token"
KEY = "AIza-test-key-never-shown"
VID = "qSyo5d-dVzA"
START = 1_795_000_000.0          # the broadcast's actual start


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def ts(sec):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(START + sec, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeYouTube:
    """Answers videos.list and liveChatMessages.list like the real API, from a scripted chat."""

    def __init__(self, chat_pages, video=None):
        self.pages, self.calls, self.headers = list(chat_pages), [], []
        self.video = video if video is not None else {
            "snippet": {"title": "PLAYING GTA 6 EARLY ACCESS GAMEPLAY", "channelTitle": "IShowSpeed"},
            "liveStreamingDetails": {"actualStartTime": ts(0), "activeLiveChatId": "CHAT1"}}

    def __call__(self, req, timeout=None):
        url = urllib.parse.urlparse(req.full_url)
        self.calls.append((url.path, urllib.parse.parse_qs(url.query)))
        self.headers.append(dict(req.header_items()))
        assert KEY not in req.full_url                       # the key never travels in a URL
        if url.path.endswith("/videos"):
            body = {"items": [self.video] if self.video else []}
        else:
            page = self.pages.pop(0) if self.pages else {"items": [], "pollingIntervalMillis": 5000}
            if isinstance(page, Exception):
                raise page
            body = page
        return io.BytesIO(json.dumps(body).encode())


def msgs(start, n, text="hello", every=1.0, kind="textMessageEvent", money=None):
    out = []
    for i in range(n):
        sn = {"type": kind, "publishedAt": ts(start + i * every), "displayMessage": text}
        if money:
            sn["superChatDetails"] = {"amountMicros": str(int(money * 1e6)), "currency": "USD"}   # a string, as the API sends it
        out.append({"id": f"m{start}-{i}-{text}", "snippet": sn})
    return out


def api_error(code, reason):
    body = json.dumps({"error": {"code": code, "message": reason, "errors": [{"reason": reason}]}}).encode()
    return urllib.error.HTTPError("https://www.googleapis.com/x", code, reason, {}, io.BytesIO(body))


@pytest.fixture
def make(tmp_path):
    n = [0]

    def _make(pages, video=None, key=KEY):
        fake = FakeYouTube(pages, video)
        n[0] += 1
        d = tmp_path / f"hub{n[0]}"
        _make.dir = str(d)
        app = create_app(data_dir=str(d), admin_token=ADMIN, youtube_key=key,
                         youtube_opener=fake, youtube_threads=False)
        client = TestClient(app)
        tok = client.post("/agents", headers=H(ADMIN), json={"handle": "claude"}).json()["token"]
        return client, tok, app.state.watcher, fake
    return _make


def test_link_formats():
    for u in [f"https://www.youtube.com/live/{VID}", f"https://youtu.be/{VID}", f"https://www.youtube.com/watch?v={VID}&t=3",
              f"youtube.com/watch?v={VID}", f"https://m.youtube.com/shorts/{VID}", VID]:
        assert streams.video_id(u) == VID, u
    for bad in ["https://vimeo.com/123", "https://www.youtube.com/watch", "not a link"]:
        with pytest.raises(streams.StreamError):
            streams.video_id(bad)


def test_a_chat_spike_becomes_a_moment_in_stream_time(make):
    # 20 min of calm chat (1 msg / 2 s), then 30 s where chat explodes with reactions, then calm again
    calm1 = msgs(600, 600, "gg", every=2.0)                       # 10:00 – 30:00
    spike = msgs(1800, 300, "💀💀 HE CRASHED OUT LMAO", every=0.1)  # 30:00 – 30:30
    calm2 = msgs(1830, 150, "gg", every=2.0)
    pages = [{"items": calm1, "nextPageToken": "p1", "pollingIntervalMillis": 4000},
             {"items": spike + calm2, "nextPageToken": "p2", "pollingIntervalMillis": 4000}]
    client, tok, w, fake = make(pages)
    r = client.post("/streams", headers=H(tok), json={"url": f"https://www.youtube.com/live/{VID}"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "watching" and r.json()["title"].startswith("PLAYING GTA 6")
    assert w.step(VID) == 15.0                       # never faster than the allowance-friendly pace
    w.step(VID)
    assert fake.calls[-1][1]["pageToken"] == ["p1"]  # carries on where it left off
    m = client.get(f"/streams/{VID}/moments", headers=H(tok)).json()
    assert m["messages"] == 1050 and m["covered_to"].startswith("0:3")
    top = m["moments"][0]
    assert top["peak"].startswith("0:30:") and top["clip_from"] == "0:29:40"   # 20 s before chat took off
    assert top["vs_usual"] > 5 and "💀💀 he crashed out lmao" in top["chat_sample"]
    # the key went in a header only, and is in no response
    assert all(h.get("X-goog-api-key") == KEY for h in fake.headers)
    for path in ("/streams", f"/streams/{VID}", f"/streams/{VID}/moments"):
        assert KEY not in client.get(path, headers=H(tok)).text


def test_super_chats_count_and_duplicates_are_ignored(make):
    base = msgs(0, 400, "hi", every=2.0)
    money = msgs(800, 20, "TAKE MY MONEY", every=0.5, kind="superChatEvent", money=50)
    page = {"items": base + money, "pollingIntervalMillis": 1000}
    client, tok, w, _ = make([page, page])                         # the same page twice
    client.post("/streams", headers=H(tok), json={"url": VID})
    w.step(VID); w.step(VID)
    assert w.status(VID)["messages"] == 420
    top = w.moments(VID)["moments"][0]
    assert top["super_chats"] == 20


def test_sparse_old_chat_and_the_first_minutes_are_not_moments(make):
    # the first page brings a few old pinned Super Chats from an hour earlier, then live chat starts
    old = msgs(0, 5, "old super", every=600.0, kind="superChatEvent", money=1000)
    live = msgs(4000, 1200, "w", every=0.25)          # steady 40 a bucket for 5 minutes
    client, tok, w, _ = make([{"items": old + live}])
    client.post("/streams", headers=H(tok), json={"url": VID})
    w.step(VID)
    assert w.moments(VID)["moments"] == []            # nothing stood out, and nothing was made up


def test_chat_end_and_errors_in_plain_words(make):
    client, tok, w, _ = make([{"items": msgs(0, 5) + [{"id": "e", "snippet": {"type": "chatEndedEvent", "publishedAt": ts(9)}}]}])
    client.post("/streams", headers=H(tok), json={"url": VID})
    assert w.step(VID) is None and w.status(VID)["state"] == "ended"

    client, tok, w, _ = make([api_error(403, "liveChatEnded")])
    client.post("/streams", headers=H(tok), json={"url": VID})
    assert w.step(VID) is None and w.status(VID)["state"] == "ended"


def test_streams_that_cant_be_watched(make, tmp_path):
    ended = {"snippet": {"title": "old"}, "liveStreamingDetails": {"actualStartTime": ts(0), "actualEndTime": ts(99)}}
    client, tok, _, _ = make([], video=ended)
    r = client.post("/streams", headers=H(tok), json={"url": VID})
    assert r.status_code == 409 and "already ended" in r.json()["detail"]
    client, tok, _, _ = make([], video={"snippet": {"title": "a video"}})
    assert "isn't a live stream" in client.post("/streams", headers=H(tok), json={"url": VID}).json()["detail"]


def test_no_key_says_what_to_do(tmp_path):
    client = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, youtube_key="", youtube_threads=False))
    tok = client.post("/agents", headers=H(ADMIN), json={"handle": "claude"}).json()["token"]
    r = client.post("/streams", headers=H(tok), json={"url": VID})
    assert r.status_code == 503 and "YOUTUBE_API_KEY" in r.json()["detail"]


def test_daily_allowance_is_respected(make, monkeypatch):
    monkeypatch.setattr(streams, "UNITS_PER_DAY", 12)              # 1 lookup + 2 chat reads
    client, tok, w, fake = make([{"items": msgs(0, 3)}] * 5)
    client.post("/streams", headers=H(tok), json={"url": VID})
    w.step(VID); w.step(VID)
    assert w.step(VID) == 600                                     # waits instead of overspending
    assert "allowance" in w.status(VID)["error"] and len(fake.calls) == 3


def test_connector_tools(make):
    calm = msgs(0, 600, "gg", every=2.0)
    spike = msgs(1200, 300, "NO WAY 😭😭", every=0.1)
    client, tok, w, _ = make([{"items": calm + spike, "pollingIntervalMillis": 1000}])

    def call(name, args):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}}
        return client.post("/mcp", headers=H(tok), json=body).json()["result"]

    listed = client.post("/mcp", headers=H(tok), json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
    names = {t["name"] for t in listed["result"]["tools"]}
    assert {"watch_stream", "stream_moments", "ask", "check_replies"} <= names
    r = call("watch_stream", {"url": f"https://youtu.be/{VID}"})
    assert not r["isError"] and "Watching chat" in r["content"][0]["text"]
    w.step(VID)
    r = call("stream_moments", {"url": VID, "top": 3})
    assert not r["isError"] and "0:2" in r["content"][0]["text"] and "NO WAY" in r["content"][0]["text"].upper()
    r = call("stream_moments", {"url": "https://vimeo.com/1"})
    assert r["isError"] and "YouTube link" in r["content"][0]["text"]


def test_watching_survives_a_restart(make, tmp_path):
    client, tok, w, _ = make([{"items": msgs(0, 5), "nextPageToken": "p9"}])
    client.post("/streams", headers=H(tok), json={"url": VID})
    w.step(VID)
    fake2 = FakeYouTube([{"items": msgs(5, 5)}])
    app2 = create_app(data_dir=make.dir, admin_token=ADMIN, youtube_key=KEY, youtube_opener=fake2, youtube_threads=False)
    w2 = app2.state.watcher
    assert w2.status(VID)["state"] == "watching"
    w2.step(VID)
    assert fake2.calls[0][1]["pageToken"] == ["p9"] and w2.status(VID)["messages"] == 10


def test_only_the_owner_stops_watching(make):
    client, tok, w, _ = make([])
    client.post("/streams", headers=H(tok), json={"url": VID})
    assert client.delete(f"/streams/{VID}", headers=H(tok)).status_code == 403
    owner = client.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    assert client.delete(f"/streams/{VID}", headers=H(owner)).status_code == 200
    assert w.status(VID)["state"] == "stopped"
