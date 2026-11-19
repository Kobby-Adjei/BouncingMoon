"""Watch a public YouTube live stream's chat from the hub and rank the moments chat reacted to.

YouTube blocks cloud servers from downloading video, but its official Data API lets any server
read a public live stream's chat while it is live. The hub reads that chat as it happens, keeps
each message's time and text (never who wrote it), and ranks the stretches where chat exploded:
messages per 10 seconds against the stream's own recent pace, reaction words, and Super Chats.
Timestamps are in the stream's own timeline, from the broadcast's actual start time.

The API only returns recent chat, never history, so a stream is covered from the moment watching
starts. Chat replay of a finished stream is not available through the API.

Needs YOUTUBE_API_KEY (a free key on the hub's Google Cloud project). It travels in the
X-goog-api-key header, never in a URL, and never appears in a response or a log line.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from typing import Callable, Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field

API = "https://www.googleapis.com/youtube/v3"
UNITS_PER_DAY = 9500            # of the default 10,000: leave room for a few status lookups
CHAT_CALL_UNITS = 5             # liveChatMessages.list
VIDEO_CALL_UNITS = 1            # videos.list
MAX_WATCH_SECONDS = 12 * 3600
VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
REACTION = re.compile(
    r"(💀|😭|😂|🤣|😱|🔥|lmao|lmfao|lol|haha|omg|no way|nah|bro|crazy|insane|wtf|rage|crash|"
    r"crashing|crashout|crash out|scream|w+\b|\bl+\b|clip (it|that)|speed)", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS streams(
  video_id TEXT PRIMARY KEY, url TEXT, title TEXT, channel TEXT, started REAL, chat_id TEXT,
  state TEXT, error TEXT, created REAL, by TEXT, last_poll REAL, page_token TEXT,
  messages INTEGER NOT NULL DEFAULT 0, calls INTEGER NOT NULL DEFAULT 0, watch_from REAL);
CREATE TABLE IF NOT EXISTS stream_chat(
  video_id TEXT, msg_id TEXT, t REAL, text TEXT, kind TEXT, amount REAL,
  PRIMARY KEY(video_id, msg_id));
CREATE INDEX IF NOT EXISTS stream_chat_t ON stream_chat(video_id, t);
CREATE TABLE IF NOT EXISTS youtube_quota(day TEXT PRIMARY KEY, units INTEGER NOT NULL);
"""


class StreamError(Exception):
    """A plain-language reason the stream can't be watched."""


def video_id(url: str) -> str:
    url = (url or "").strip()
    if VIDEO_ID.match(url):
        return url
    p = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    host = (p.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        cand = p.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "music.youtube.com"):
        q = urllib.parse.parse_qs(p.query).get("v")
        parts = [x for x in p.path.split("/") if x]
        cand = q[0] if q else (parts[1] if len(parts) >= 2 and parts[0] in ("live", "shorts", "embed", "v") else "")
    else:
        raise StreamError("That isn't a YouTube link.")
    if not VIDEO_ID.match(cand or ""):
        raise StreamError("Couldn't find the video in that YouTube link.")
    return cand


def iso(ts: Optional[str]) -> Optional[float]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def hms(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


class YouTube:
    def __init__(self, key: str, opener: Callable = urllib.request.urlopen):
        self.key, self.opener = key, opener

    def get(self, path: str, **params) -> dict:
        req = urllib.request.Request(f"{API}/{path}?{urllib.parse.urlencode(params)}",
                                     headers={"X-goog-api-key": self.key, "Accept": "application/json"})
        try:
            with self.opener(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode()).get("error", {})
            except Exception:
                err = {}
            reasons = [x.get("reason", "") for x in err.get("errors", [])] or [str(err.get("status", ""))]
            raise ApiError(e.code, reasons[0] if reasons else "", err.get("message", "")) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ApiError(0, "network", str(e)) from None


class ApiError(Exception):
    def __init__(self, status: int, reason: str, message: str):
        super().__init__(f"{status} {reason}")
        self.status, self.reason, self.message = status, reason, message


def plain(e: ApiError) -> str:
    r = e.reason
    if r in ("liveChatEnded",):
        return "The stream's chat has ended."
    if r in ("liveChatDisabled",):
        return "Chat is turned off on that stream."
    if r in ("liveChatNotFound", "videoNotFound", "notFound"):
        return "YouTube can't find that stream's chat."
    if r in ("quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"):
        return "The hub has used today's YouTube allowance. It resets at midnight Pacific time."
    if r in ("keyInvalid", "badRequest") or e.status in (400, 401) and "key" in (e.message or "").lower():
        return "The hub's YouTube key isn't valid. The owner can replace YOUTUBE_API_KEY in the hub's settings."
    if r in ("accessNotConfigured", "SERVICE_DISABLED") or "has not been used" in (e.message or ""):
        return "The YouTube Data API isn't turned on for the hub's Google project yet."
    if r == "forbidden" or e.status == 403:
        return "YouTube refused the request for that stream."
    if e.status == 0:
        return "Couldn't reach YouTube just now."
    return f"YouTube said no ({e.status} {r})."


class Watcher:
    def __init__(self, store, key: Optional[str], opener: Callable = urllib.request.urlopen,
                 now: Callable[[], float] = time.time, threads: bool = True, min_interval: float = 15.0):
        self.store, self.now, self.threads, self.min_interval = store, now, threads, min_interval
        self.yt = YouTube(key, opener) if key else None
        self.running: dict = {}
        self.lock = threading.Lock()
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)

    # ------------------------------------------------------------ quota
    def _spend(self, units: int) -> None:
        day = datetime.fromtimestamp(self.now() - 8 * 3600, timezone.utc).strftime("%Y-%m-%d")   # resets ~midnight Pacific
        with self.store.tx() as c:
            r = c.execute("SELECT units FROM youtube_quota WHERE day=?", (day,)).fetchone()
            used = r["units"] if r else 0
            if used + units > UNITS_PER_DAY:
                raise StreamError("The hub has used today's YouTube allowance. It resets at midnight Pacific time.")
            c.execute("INSERT INTO youtube_quota(day,units) VALUES (?,?) ON CONFLICT(day) DO UPDATE SET units=units+?",
                      (day, units, units))

    def need_key(self):
        if not self.yt:
            raise StreamError("The hub needs a YouTube key first. The owner adds YOUTUBE_API_KEY in the hub's settings.")

    # ------------------------------------------------------------ start / status
    def watch(self, url: str, by: str) -> dict:
        self.need_key()
        vid = video_id(url)
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM streams WHERE video_id=?", (vid,)).fetchone()
        if r and r["state"] == "watching":
            self._ensure_thread(vid)
            return self.status(vid)
        self._spend(VIDEO_CALL_UNITS)
        try:
            data = self.yt.get("videos", part="snippet,liveStreamingDetails", id=vid)
        except ApiError as e:
            raise StreamError(plain(e)) from None
        items = data.get("items") or []
        if not items:
            raise StreamError("YouTube can't find that video. It may be private or removed.")
        v = items[0]
        sn, live = v.get("snippet") or {}, v.get("liveStreamingDetails") or {}
        chat = live.get("activeLiveChatId")
        if not live:
            raise StreamError("That video isn't a live stream.")
        if not chat:
            if live.get("actualEndTime"):
                raise StreamError("That stream has already ended. YouTube only shares chat while a stream is live.")
            raise StreamError("That stream has no live chat right now.")
        started = iso(live.get("actualStartTime")) or iso(live.get("scheduledStartTime"))
        now = self.now()
        with self.store.tx() as c:
            c.execute("INSERT INTO streams(video_id,url,title,channel,started,chat_id,state,error,created,by,watch_from)"
                      " VALUES (?,?,?,?,?,?,'watching',NULL,?,?,?) ON CONFLICT(video_id) DO UPDATE SET"
                      " chat_id=excluded.chat_id, state='watching', error=NULL, title=excluded.title,"
                      " started=excluded.started, page_token=NULL, watch_from=excluded.watch_from",
                      (vid, f"https://www.youtube.com/watch?v={vid}", sn.get("title"), sn.get("channelTitle"),
                       started, chat, now, by, now))
        self._ensure_thread(vid)
        return self.status(vid)

    def stop(self, vid: str, why: str = "stopped") -> None:
        with self.store.tx() as c:
            c.execute("UPDATE streams SET state=?, page_token=NULL WHERE video_id=? AND state='watching'", (why, vid))

    def status(self, vid: str) -> dict:
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM streams WHERE video_id=?", (vid,)).fetchone()
            if not r:
                raise StreamError("The hub isn't watching that stream. Start with watch_stream.")
            last = c.execute("SELECT MAX(t) m FROM stream_chat WHERE video_id=?", (vid,)).fetchone()["m"]
        out = {"video_id": vid, "url": r["url"], "title": r["title"], "channel": r["channel"], "state": r["state"],
               "error": r["error"], "messages": r["messages"],
               "stream_started": r["started"], "covered_from": hms(r["watch_from"] - r["started"]) if r["started"] and r["watch_from"] else None,
               "covered_to": hms(last - r["started"]) if last and r["started"] else None}
        if r["state"] == "watching" and last:
            out["behind_seconds"] = max(0, int(self.now() - last))
        return out

    def list(self) -> list:
        with self.store.ro() as c:
            vids = [r["video_id"] for r in c.execute("SELECT video_id FROM streams ORDER BY created DESC LIMIT 50")]
        return [self.status(v) for v in vids]

    # ------------------------------------------------------------ polling
    def _ensure_thread(self, vid: str) -> None:
        if not self.threads:
            return
        with self.lock:
            t = self.running.get(vid)
            if t and t.is_alive():
                return
            t = threading.Thread(target=self._loop, args=(vid,), name=f"stream-{vid}", daemon=True)
            self.running[vid] = t
            t.start()

    def resume(self) -> None:
        """After a restart, carry on with every stream that was being watched."""
        if not self.yt:
            return
        with self.store.ro() as c:
            vids = [r["video_id"] for r in c.execute("SELECT video_id FROM streams WHERE state='watching'")]
        for v in vids:
            self._ensure_thread(v)

    def _loop(self, vid: str) -> None:
        while True:
            try:
                wait = self.step(vid)
            except Exception as e:                    # never let one stream take the hub down
                print(f"[relay] stream {vid}: {type(e).__name__}", flush=True)
                wait = 60
            if wait is None:
                return
            time.sleep(wait)

    def step(self, vid: str) -> Optional[float]:
        """One poll. Returns seconds to wait before the next, or None when watching is over."""
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM streams WHERE video_id=?", (vid,)).fetchone()
        if not r or r["state"] != "watching":
            return None
        if self.now() - (r["watch_from"] or r["created"]) > MAX_WATCH_SECONDS:
            self.stop(vid, "ended")
            return None
        try:
            self._spend(CHAT_CALL_UNITS)
        except StreamError as e:
            with self.store.tx() as c:
                c.execute("UPDATE streams SET error=? WHERE video_id=?", (str(e), vid))
            return 600
        params = {"liveChatId": r["chat_id"], "part": "snippet", "maxResults": 2000}
        if r["page_token"]:
            params["pageToken"] = r["page_token"]
        try:
            data = self.yt.get("liveChat/messages", **params)
        except ApiError as e:
            msg = plain(e)
            final = e.reason in ("liveChatEnded", "liveChatDisabled", "liveChatNotFound", "forbidden", "keyInvalid")
            with self.store.tx() as c:
                c.execute("UPDATE streams SET error=?, state=CASE WHEN ? THEN ? ELSE state END WHERE video_id=?",
                          (None if e.reason == "liveChatEnded" else msg, final,
                           "ended" if e.reason == "liveChatEnded" else "error", vid))
            return None if final else 60
        rows, ended = [], False
        for m in data.get("items") or []:
            sn = m.get("snippet") or {}
            kind = sn.get("type") or "textMessageEvent"
            if kind == "chatEndedEvent":
                ended = True
                continue
            if kind not in ("textMessageEvent", "superChatEvent", "superStickerEvent"):
                continue
            t = iso(sn.get("publishedAt"))
            text = (sn.get("displayMessage") or (sn.get("textMessageDetails") or {}).get("messageText") or "")[:300]
            sc = sn.get("superChatDetails") or sn.get("superStickerDetails") or {}
            amount = (sc.get("amountMicros") or 0) / 1e6 if sc else None
            if t is None or not m.get("id"):
                continue
            rows.append((vid, m["id"], t, text, "super" if sc else "text", amount))
        with self.store.tx() as c:
            before = c.total_changes
            c.executemany("INSERT OR IGNORE INTO stream_chat(video_id,msg_id,t,text,kind,amount) VALUES (?,?,?,?,?,?)", rows)
            added = c.total_changes - before
            c.execute("UPDATE streams SET page_token=?, last_poll=?, calls=calls+1, messages=messages+?, error=NULL"
                      " WHERE video_id=?", (data.get("nextPageToken"), self.now(), added, vid))
        if ended or data.get("offlineAt"):
            self.stop(vid, "ended")
            return None
        interval = (data.get("pollingIntervalMillis") or 0) / 1000
        # behind real time with a full page: read on sooner; otherwise keep to the daily allowance
        if len(data.get("items") or []) >= 1900:
            return max(interval, 3.0)
        return max(interval, self.min_interval)

    # ------------------------------------------------------------ moments
    def moments(self, vid: str, top: int = 10, bucket: int = 10) -> dict:
        top, bucket = max(1, min(int(top), 30)), max(5, min(int(bucket), 60))
        st = self.status(vid)
        with self.store.ro() as c:
            r = c.execute("SELECT started FROM streams WHERE video_id=?", (vid,)).fetchone()
            msgs = c.execute("SELECT t, text, kind, amount FROM stream_chat WHERE video_id=? ORDER BY t", (vid,)).fetchall()
        started = r["started"] or (msgs[0]["t"] if msgs else self.now())
        if len(msgs) < 30:
            return {**st, "moments": [], "note": "Not enough chat yet. Ask again in a few minutes."}
        first = int((msgs[0]["t"] - started) // bucket)
        last = int((msgs[-1]["t"] - started) // bucket)
        n = last - first + 1
        count, react, money = [0] * n, [0] * n, [0.0] * n
        texts: list = [[] for _ in range(n)]
        for m in msgs:
            i = int((m["t"] - started) // bucket) - first
            count[i] += 1
            if REACTION.search(m["text"] or ""):
                react[i] += 1
            if m["amount"]:
                money[i] += m["amount"]
            if len(texts[i]) < 400:
                texts[i].append(m["text"] or "")
        # each bucket against the stream's own pace over the previous 5 minutes (not the whole stream:
        # chat speeds up and slows down for reasons that aren't moments)
        win = max(6, 300 // bucket)
        score = [0.0] * n
        for i in range(n):
            prev = sorted(count[max(0, i - win):i]) or [count[i]]
            base = max(prev[len(prev) // 2], 1)
            score[i] = (count[i] / base) * (1 + react[i] / max(count[i], 1)) + min(money[i] / 20, 3)
        # merge neighbouring hot buckets into one moment, best first
        order = sorted(range(n), key=lambda i: score[i], reverse=True)
        used, picks = set(), []
        for i in order:
            if score[i] < 1.4 or len(picks) >= top:
                break
            if i in used:
                continue
            lo = hi = i
            while lo - 1 >= 0 and lo - 1 not in used and score[lo - 1] >= 1.2 and i - lo < 6:
                lo -= 1
            while hi + 1 < n and hi + 1 not in used and score[hi + 1] >= 1.2 and hi - i < 6:
                hi += 1
            used.update(range(max(0, lo - 3), min(n, hi + 4)))       # keep moments apart
            spike_start = started + (lo + first) * bucket
            spike_end = started + (hi + first + 1) * bucket
            sample = Counter(t.strip().lower() for j in range(lo, hi + 1) for t in texts[j] if t.strip())
            picks.append({
                # chat reacts a few seconds after what happened, and viewers watch with a delay:
                # start the clip ~20 s before chat took off
                "clip_from": hms(spike_start - 20 - started), "peak": hms(started + (i + first) * bucket - started),
                "clip_to": hms(spike_end + 5 - started),
                "chat_per_min": round(sum(count[lo:hi + 1]) * 60 / ((hi - lo + 1) * bucket)),
                "vs_usual": round(score[i], 1),
                "reaction_share": round(sum(react[lo:hi + 1]) / max(sum(count[lo:hi + 1]), 1), 2),
                "super_chats": round(sum(money[lo:hi + 1]), 2) or None,
                "chat_sample": [t[:120] for t, _ in sample.most_common(8)],
            })
        return {**st, "bucket_seconds": bucket, "moments": picks,
                "note": "Timestamps are in the stream's own timeline. Chat runs a few seconds behind the stream, "
                        "so each clip starts about 20 s before chat took off."}


class WatchIn(BaseModel):
    url: str = Field(min_length=5, max_length=500)


def mount(app, hub, watcher: Watcher) -> dict:
    """Routes, plus the connector tools (returned so the MCP endpoint can list them)."""
    auth = hub.auth

    def guard(fn, *a):
        try:
            return fn(*a)
        except StreamError as e:
            raise HTTPException(409 if watcher.yt else 503, str(e)) from None

    def member(me):
        if me["role"] == "admin":
            raise HTTPException(403, "use your own key")
        return me["handle"]

    @app.post("/streams")
    def start(body: WatchIn, me: dict = Depends(auth)):
        return guard(watcher.watch, body.url, member(me))

    @app.get("/streams")
    def all_streams(me: dict = Depends(auth)):
        return watcher.list()

    @app.get("/streams/{vid}")
    def one(vid: str, me: dict = Depends(auth)):
        return guard(watcher.status, guard(video_id, vid))

    @app.get("/streams/{vid}/moments")
    def moments(vid: str, top: int = 10, bucket: int = 10, me: dict = Depends(auth)):
        return guard(watcher.moments, guard(video_id, vid), top, bucket)

    @app.delete("/streams/{vid}")
    def stop(vid: str, me: dict = Depends(auth)):
        if me["role"] != "admin" and not me.get("owner"):
            raise HTTPException(403, "only the hub's owner can stop watching a stream")
        watcher.stop(guard(video_id, vid))
        return {"ok": True}

    # ------------------------------------------------------------ connector tools
    def ToolFail(msg):              # the MCP endpoint shows an HTTPException's detail as the tool's error
        return HTTPException(409, msg)

    def tool_watch(me, args):
        try:
            s = watcher.watch(str(args.get("url", "")), me)
        except StreamError as e:
            raise ToolFail(str(e)) from None
        return (f"Watching chat on \"{s['title']}\" from {s['covered_from'] or 'now'} in the stream. "
                "Chat is only readable while it's live, so earlier moments aren't covered. "
                "Call stream_moments with the same link for the ranked moments."), s

    def tool_moments(me, args):
        try:
            vid = video_id(str(args.get("url", "")))
            m = watcher.moments(vid, args.get("top", 10))
        except StreamError as e:
            raise ToolFail(str(e)) from None
        if not m["moments"]:
            return m.get("note") or "No standout moments in chat yet.", m
        lines = [f"{i + 1}. {x['clip_from']}–{x['clip_to']} (peak {x['peak']}): chat {x['vs_usual']}× usual"
                 f"{', $' + str(x['super_chats']) + ' in Super Chats' if x['super_chats'] else ''}. "
                 f"Chat: {' | '.join(x['chat_sample'][:4])}" for i, x in enumerate(m["moments"])]
        return (f"Top chat moments in \"{m['title']}\" (covered {m['covered_from']}–{m['covered_to']}):\n"
                + "\n".join(lines) + "\n" + m["note"]), m

    tools = [
        {"name": "watch_stream",
         "description": "Start watching a public YouTube live stream's chat (the hub does it, in the cloud). "
                        "Chat is captured from now on while the stream is live; YouTube doesn't share earlier chat. "
                        "Use stream_moments afterwards to get the moments chat reacted to most, with timestamps.",
         "inputSchema": {"type": "object", "properties": {"url": {"type": "string", "description": "The YouTube link"}},
                         "required": ["url"]}},
        {"name": "stream_moments",
         "description": "Ranked moments from a stream the hub is watching: where chat spiked against its usual pace, "
                        "with stream timestamps (clip from/to), how hot chat got, Super Chats, and a sample of what chat said. "
                        "Describe each moment only from what chat said; the hub doesn't see or hear the video.",
         "inputSchema": {"type": "object", "properties": {"url": {"type": "string"},
                                                          "top": {"type": "integer", "minimum": 1, "maximum": 30, "default": 10}},
                         "required": ["url"]}},
    ]
    return {"tools": tools, "handlers": {"watch_stream": tool_watch, "stream_moments": tool_moments}}
