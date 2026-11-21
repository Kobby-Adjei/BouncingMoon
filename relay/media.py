"""Media: share videos, audio, images and files between agents and people, already understood.

Any agent or person can put media into a conversation: an agent attaches a link or a file already on
the hub, or a person uses an upload link an agent asked for (a page that works on any phone, picks from
the camera roll, and keeps going when the connection drops). No Drive, no command line.

The hub does the watching once, when the media arrives, so no agent has to:
  video  specs, shot changes, a labelled contact sheet, a timed transcript, speech/silence, loudness,
         a copy every browser plays (H.264/AAC MP4) and a poster frame
  audio  specs, a timed transcript, speech/silence, loudness, a copy every browser plays
  image  location and other metadata stripped (the original with it is not kept), a preview an agent
         can look at
  other  kept as a download (never shown inline: a file could be a web page)

Agents get a short card in the conversation and can look closer with view_media: the contact sheet or
the image itself comes back as a picture, and `at` returns frames around a moment. People get a viewer
link: a private page that plays it on any phone, signed and expiring, no key needed.

One media item at a time, in a background thread, from a queue table, so a restart picks up where it
left off. ffmpeg does the work; the speech model is the one captions already loads.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Optional

from fastapi import Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from . import captions

LINK_SECONDS = 7 * 86400          # a viewer link works this long
UPLOAD_SECONDS = 24 * 3600        # an upload link works this long
UPLOAD_FILES = 10                 # files one upload link takes
CHUNK = 8 * 2 ** 20               # the upload page sends files in pieces this big, so a dropped connection resumes
SHEET_FRAMES = 12
MAX_TRANSCRIBE = 1800.0           # seconds of speech transcribed (a long recording: the first 30 minutes)
MAX_TRANSCODE = 1800.0            # longer videos that browsers can't play natively are offered as a download
PREVIEW_PX = 1568                 # the longest side of an image an agent looks at
ID_RE = re.compile(r"^m_[0-9a-f]{16}$")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,80}$")
IMAGE_MAGIC = ((b"\xff\xd8\xff", "image/jpeg"), (b"\x89PNG\r\n\x1a\n", "image/png"), (b"GIF87a", "image/gif"),
               (b"GIF89a", "image/gif"))
PLAYABLE_VIDEO = ("h264",)
PLAYABLE_AUDIO = ("aac", "mp3")
LABEL_FONTS = ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
               "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
               "/usr/share/fonts/opentype/montserrat/Montserrat-ExtraBold.otf")

SCHEMA = """
CREATE TABLE IF NOT EXISTS media(
  id TEXT PRIMARY KEY, kind TEXT, name TEXT, mime TEXT, size INTEGER, created_by TEXT NOT NULL,
  created REAL, updated REAL, status TEXT NOT NULL, stage TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  source_url TEXT, artifact TEXT, playable TEXT, poster TEXT, sheet TEXT, preview TEXT,
  digest TEXT NOT NULL DEFAULT '{}', revoked REAL);
CREATE INDEX IF NOT EXISTS media_queue ON media(status, created);
CREATE TABLE IF NOT EXISTS message_media(msg INTEGER NOT NULL, media TEXT NOT NULL, PRIMARY KEY(msg, media));
CREATE INDEX IF NOT EXISTS message_media_by_media ON message_media(media);
CREATE TABLE IF NOT EXISTS upload_links(
  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, handle TEXT NOT NULL, session TEXT, anchor INTEGER,
  note TEXT, created REAL, expires REAL NOT NULL, files INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS upload_files(
  id TEXT PRIMARY KEY, link TEXT NOT NULL, name TEXT, mime TEXT, size INTEGER NOT NULL,
  received INTEGER NOT NULL DEFAULT 0, media TEXT, created REAL);
"""


class MediaError(Exception):
    """Something the person or agent can act on, in plain words."""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hms(sec: float) -> str:
    return captions.hms(sec)


def clock(sec: float) -> str:
    """0:03.5 style, for moments inside a clip."""
    sec = max(0.0, float(sec))
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}" if sec < 600 else hms(sec)


def size_text(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} bytes"


def parse_time(v: Any) -> Optional[float]:
    return captions.seconds(v)


def safe_name(name: str, default: str = "file") -> str:
    name = os.path.basename(str(name or "")).strip().replace("\x00", "")
    name = re.sub(r"[\r\n\t\"\\/<>:|?*]", "_", name)[:120]
    return name or default


def label_font() -> Optional[str]:
    return next((f for f in LABEL_FONTS if os.path.exists(f)), None)


def _run(cmd: list, timeout: float) -> subprocess.CompletedProcess:
    return captions._run(cmd, timeout)


def _ff(timeout: float, *args) -> subprocess.CompletedProcess:
    return _run(["ffmpeg", "-hide_banner", "-nostdin", "-y", *args], timeout)


# --------------------------------------------------------------------------- reading media
def sniff(path: str) -> dict:
    """What is this file? kind (video | audio | image | file), mime, and ffprobe facts where they apply."""
    with open(path, "rb") as f:
        head = f.read(64)
    for magic, mime in IMAGE_MAGIC:
        if head.startswith(magic):
            return {"kind": "image", "mime": mime}
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return {"kind": "image", "mime": "image/webp"}
    if head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"mif1", b"msf1", b"heim", b"heis"):
        return {"kind": "image", "mime": "image/heic"}
    if head.startswith(b"%PDF"):
        return {"kind": "file", "mime": "application/pdf"}
    try:
        info = json.loads(_run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
                                "-show_streams", path], 120).stdout)
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return {"kind": "file", "mime": "application/octet-stream"}
    streams = info.get("streams") or []
    fmt = info.get("format") or {}
    v = next((s for s in streams if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur = float(fmt.get("duration") or (v or a or {}).get("duration") or 0)
    if v and v.get("width") and dur > 0.2:
        p = captions.probe(path)
        fps = v.get("avg_frame_rate") or "0/1"
        try:
            num, den = fps.split("/")
            fps = round(float(num) / float(den), 2) if float(den) else None
        except (ValueError, ZeroDivisionError):
            fps = None
        return {"kind": "video", "mime": "video/mp4", "duration": p["duration"], "width": p["width"],
                "height": p["height"], "vcodec": p["vcodec"], "acodec": p["acodec"], "has_audio": p["has_audio"],
                "fps": fps, "container": fmt.get("format_name", "")}
    if a and dur > 0:
        return {"kind": "audio", "mime": "audio/mpeg", "duration": dur, "acodec": a.get("codec_name"),
                "channels": a.get("channels"), "sample_rate": a.get("sample_rate"),
                "container": fmt.get("format_name", "")}
    if v and v.get("width"):                           # a single frame: an image format ffmpeg reads
        return {"kind": "image", "mime": "image/" + str(v.get("codec_name") or "unknown")}
    return {"kind": "file", "mime": "application/octet-stream"}


def shot_changes(path: str, duration: float, limit: int = 60) -> list:
    """Where the picture cuts to something new (seconds). Key frames only, so it's quick on long videos."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-skip_frame", "nokey", "-i", path, "-an",
                            "-vf", "scale=160:-2,select='gt(scene,0.32)',showinfo", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=max(120, duration))
    except subprocess.TimeoutExpired:
        return []
    times = sorted({round(float(t), 2) for t in re.findall(r"pts_time:([0-9.]+)", p.stderr)})
    out = [t for t in times if 0.3 < t < duration - 0.2]
    if len(out) > limit:                               # keep them spread over the whole clip
        step = len(out) / limit
        out = [out[int(i * step)] for i in range(limit)]
    return out


def sheet_times(duration: float, shots: list, n: int = SHEET_FRAMES) -> list:
    """Moments for the contact sheet: the start, every shot change if they fit, else evenly spread."""
    if duration <= 0:
        return [0.0]
    starts = [0.0] + list(shots)
    if 2 <= len(starts) <= n:
        pts = [s + min(0.5, (duration - s) / 2) for s in starts]
    else:
        k = min(n, max(1, int(duration // 1.5) or 1))
        pts = [duration * (i + 0.5) / k for i in range(k)]
    return [round(min(max(p, 0.0), max(duration - 0.05, 0.0)), 2) for p in pts]


def grab(src: str, t: float, dest: str, width: int, label: Optional[str] = None, square: Optional[int] = None) -> bool:
    vf = f"scale={width}:-2"
    if square:
        vf = (f"scale={square}:{square}:force_original_aspect_ratio=decrease,"
              f"pad={square}:{square}:(ow-iw)/2:(oh-ih)/2:color=black")
    font = label_font()
    if label and font:
        vf += (f",drawtext=fontfile={font}:text='{label.replace(':', chr(92) + ':')}':x=8:y=h-th-8:fontsize=22:"
               "fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=5")
    try:
        _ff(60, "-ss", f"{max(t, 0):.2f}", "-i", src, "-frames:v", "1", "-vf", vf, "-q:v", "4", dest)
    except (RuntimeError, subprocess.TimeoutExpired):
        return False
    return os.path.exists(dest) and os.path.getsize(dest) > 0


def contact_sheet(src: str, times: list, work: str) -> Optional[str]:
    cells = []
    for i, t in enumerate(times):
        p = os.path.join(work, f"cell_{i:02d}.jpg")
        if grab(src, t, p, 0, label=clock(t), square=320):
            cells.append(p)
    if not cells:
        return None
    cols = 4 if len(cells) > 4 else len(cells)
    rows = -(-len(cells) // cols)
    for i, p in enumerate(cells):                        # renumber without gaps for the image sequence
        os.replace(p, os.path.join(work, f"tile_{i:02d}.jpg"))
    out = os.path.join(work, "sheet.jpg")
    _ff(120, "-framerate", "1", "-i", os.path.join(work, "tile_%02d.jpg"), "-frames:v", "1",
        "-vf", f"tile={cols}x{rows}:padding=4:color=black", "-q:v", "4", out)
    return out


def sound(src: str, duration: float) -> dict:
    """Speech/silence spans and loudness, from ffmpeg's own meters."""
    try:
        p = subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-i", src, "-vn",
                            "-af", "silencedetect=n=-38dB:d=0.7,volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True, timeout=max(120, duration))
    except subprocess.TimeoutExpired:
        return {}
    err = p.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[0-9.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([0-9.]+)", err)]
    silences = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else duration
        silences.append([round(max(s, 0), 2), round(min(e, duration), 2)])
    quiet = sum(e - s for s, e in silences)
    mean = re.search(r"mean_volume: (-?[0-9.]+) dB", err)
    peak = re.search(r"max_volume: (-?[0-9.]+) dB", err)
    return {"silences": silences[:40], "sound_share": round(1 - quiet / duration, 2) if duration else None,
            "mean_db": float(mean.group(1)) if mean else None, "peak_db": float(peak.group(1)) if peak else None}


def transcript_lines(words: list, gap: float = 0.9, max_words: int = 14) -> list:
    """Words with times into short timed lines: a new line after a pause or every few words."""
    lines, cur, start, last = [], [], None, None
    for w in words:
        word = str(w.get("word", "")).strip()
        if not word:
            continue
        if cur and (w["start"] - last > gap or len(cur) >= max_words):
            lines.append({"t": round(start, 2), "end": round(last, 2), "text": " ".join(cur)})
            cur, start = [], None
        start = w["start"] if start is None else start
        cur.append(word)
        last = w["end"]
    if cur:
        lines.append({"t": round(start, 2), "end": round(last, 2), "text": " ".join(cur)})
    return lines


# --------------------------------------------------------------------------- the service
class Media:
    def __init__(self, store, private_host: Callable[[str], bool], secret: str,
                 transcriber: Optional[captions.Transcriber] = None, threads: bool = True,
                 allow_private: bool = False, public_url: Optional[str] = None, max_upload: int = 4 * 2 ** 30,
                 now: Callable[[], float] = time.time, log=print, idle_seconds: float = 300):
        self.store, self.private_host, self.threads = store, private_host, threads
        self.key = hashlib.sha256(b"relay-media-links:" + secret.encode()).digest()
        self.transcriber = transcriber
        self.allow_private, self.max_upload, self.now, self.log, self.idle = allow_private, max_upload, now, log, idle_seconds
        self.public_url = (public_url or os.environ.get("RELAY_PUBLIC_URL") or
                           ("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
                           ).rstrip("/") or None
        self.on_upload: Optional[Callable[[dict, str], None]] = None     # the hub tells the asking chat
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.wake = threading.Event()
        self.tmp = os.path.join(store.tmp, "media")
        os.makedirs(self.tmp, exist_ok=True)
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            if "origin_artifact" not in {r["name"] for r in c.execute("PRAGMA table_info(media)")}:
                c.execute("ALTER TABLE media ADD COLUMN origin_artifact TEXT")   # the hub file it was made from

    def url(self, path: str) -> str:
        return (self.public_url or "") + path

    # ------------------------------------------------------------ making media
    def _new(self, c, by: str, name: Optional[str], status: str = "queued", **cols) -> str:
        mid = "m_" + uuid.uuid4().hex[:16]
        now = self.now()
        c.execute("INSERT INTO media(id,name,created_by,created,updated,status,stage) VALUES (?,?,?,?,?,?,?)",
                  (mid, safe_name(name) if name else None, by, now, now, status, status))
        for k, v in cols.items():
            c.execute(f"UPDATE media SET {k}=? WHERE id=?", (v, mid))
        return mid

    def from_url(self, by: str, url: str) -> dict:
        url = (url or "").strip()
        if not re.match(r"^https?://", url) or len(url) > 2000:
            raise MediaError("Attach a link that starts with http:// or https://, or a media id (m_…).")
        try:
            target = captions.direct_url(url)
        except captions.CaptionError as e:
            raise MediaError(str(e)) from None
        if not self.allow_private and self.private_host(captions._host(target)):
            raise MediaError("That link points at a private or local address.")
        name = safe_name(target.split("?")[0].rsplit("/", 1)[-1], "link")
        with self.store.tx() as c:
            mid = self._new(c, by, name, source_url=url)
        self._ensure_thread()
        return self.get(mid)

    def from_artifact(self, by: str, artifact: str) -> dict:
        with self.store.tx() as c:
            a = c.execute("SELECT * FROM artifacts WHERE id=?", (artifact,)).fetchone()
            if not a:
                raise MediaError(f"No file {artifact} on the hub.")
            # always a media item of its own (who shared it, in which conversation); the worker reuses
            # what was already worked out for the same bytes
            mid = self._new(c, by, a["name"], artifact=artifact, size=a["size"], origin_artifact=artifact)
        self._ensure_thread()
        return self.get(mid)

    def of_message(self, msg_id: int) -> list:
        """Every file a message carries, as media ids. Files attached the old way (hub file ids in the
        message) become media items here, once, so they are watched and get links like any other."""
        with self.store.ro() as c:
            m = c.execute("SELECT sender, artifacts FROM messages WHERE id=?", (msg_id,)).fetchone()
            if not m:
                return []
            linked = {r["origin_artifact"]: r["id"] for r in c.execute(
                "SELECT m.id, m.origin_artifact FROM message_media mm JOIN media m ON m.id=mm.media WHERE mm.msg=?",
                (msg_id,))}
        new = []
        for aid in json.loads(m["artifacts"] or "[]"):
            if aid in linked:
                continue
            try:
                mid = self.from_artifact(m["sender"], aid)["id"]
            except MediaError:
                continue                                   # a file that's gone from the hub
            new.append(mid)
            with self.store.tx() as c:
                c.execute("INSERT OR IGNORE INTO message_media(msg,media) VALUES (?,?)", (msg_id, mid))
        with self.store.ro() as c:
            return [r["media"] for r in c.execute("SELECT media FROM message_media WHERE msg=? ORDER BY rowid", (msg_id,))]

    def download_link(self, mid: str, seconds: float = LINK_SECONDS) -> str:
        """A direct link to the exact file (photos without their location data), for a browser or a tool."""
        return self.link(mid, seconds) + "/original"

    def from_file(self, by: str, path: str, name: str) -> dict:
        aid, size = self._store(path, name, by, None, "upload", "application/octet-stream")
        return self.from_artifact(by, aid)

    def _store(self, path: str, name: str, by: str, parent: Optional[str], kind: str, mime: str) -> tuple:
        h, size = hashlib.sha256(), 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        aid = h.hexdigest()
        dest = self.store.blob_path(aid)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(path)
        else:
            shutil.move(path, dest)
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO artifacts(id,name,mime,size,created_by,created,meta,parent,kind)"
                      " VALUES (?,?,?,?,?,?,?,?,?)", (aid, safe_name(name), mime, size, by, self.now(), "{}", parent, kind))
        return aid, size

    # ------------------------------------------------------------ reading
    def row(self, mid: str):
        if not ID_RE.match(mid or ""):
            raise MediaError(f"No media {mid}. Media ids look like m_ followed by 16 letters and digits.")
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM media WHERE id=?", (mid,)).fetchone()
        if not r:
            raise MediaError(f"No media {mid}.")
        return r

    def get(self, mid: str) -> dict:
        r = self.row(mid)
        d = json.loads(r["digest"] or "{}")
        return {"id": r["id"], "kind": r["kind"], "name": r["name"], "size": r["size"], "status": r["status"],
                "stage": r["stage"], "error": r["error"], "from": r["created_by"], "created": r["created"],
                "revoked": bool(r["revoked"]), "digest": d,
                "has": {k: bool(r[k]) for k in ("playable", "poster", "sheet", "preview")}}

    def can_see(self, c, handle: str, mid: str) -> bool:
        """Its author, and anyone in a conversation it was shared in."""
        r = c.execute("SELECT created_by FROM media WHERE id=?", (mid,)).fetchone()
        if not r:
            return False
        if r["created_by"] == handle:
            return True
        return bool(c.execute(
            "SELECT 1 FROM message_media mm JOIN messages m ON m.id=mm.msg WHERE mm.media=? AND (m.sender=?"
            " OR EXISTS (SELECT 1 FROM json_each(m.mentions) WHERE value=?)"
            " OR m.thread IN (SELECT thread FROM follows WHERE agent=?)) LIMIT 1", (mid, handle, handle, handle)).fetchone())

    def check_access(self, handle: str, mid: str) -> dict:
        m = self.get(mid)
        with self.store.ro() as c:
            if not self.can_see(c, handle, mid):
                raise MediaError(f"Media {mid} wasn't shared with you.")
        if m["revoked"]:
            raise MediaError(f"Media {mid} was removed.")
        return m

    def card(self, m: dict, link: Optional[str] = None, transcript_chars: int = 400) -> str:
        """The few lines an agent reads in a conversation: what it is and what is in it."""
        d, kind = m["digest"], m["kind"] or "media"
        icon = {"video": "📹", "audio": "🎧", "image": "🖼", "file": "📄"}.get(kind, "📎")
        head = [f"{icon} {m['id']} · {m['name'] or kind}"]
        if d.get("duration"):
            head.append(hms(d["duration"]))
        if d.get("width"):
            head.append(f"{d['width']}×{d['height']}")
        if m.get("size"):
            head.append(size_text(m["size"]))
        lines = [" · ".join(head) + f" · from {m['from']}"]
        if m["status"] in ("queued", "working"):
            lines.append(f"   Still being processed ({m['stage'] or 'waiting'}); view_media {m['id']} will have the details.")
        elif m["status"] == "failed":
            lines.append(f"   Couldn't be processed: {m['error']}")
        else:
            if d.get("transcript"):
                text = " ".join(f"[{clock(x['t'])}] {x['text']}" for x in d["transcript"])
                lines.append("   Transcript: " + (text if len(text) <= transcript_chars else text[:transcript_chars - 1] + "…"))
            elif kind in ("video", "audio"):
                lines.append("   No speech found." if d.get("transcribed") else "   Not transcribed.")
            if kind == "video" and d.get("shots") is not None:
                lines.append(f"   {len(d['shots']) + 1} shot(s); contact sheet: view_media {m['id']}")
            if kind == "image":
                lines.append(f"   See it: view_media {m['id']}" + (" (location data removed)" if d.get("stripped") else ""))
            if d.get("sound_share") is not None:
                lines.append(f"   Sound for {int(d['sound_share'] * 100)}% of it; loudest {d.get('peak_db')} dB")
        if link:
            lines.append(f"   Watch/open: {link}")
        return "\n".join(lines)

    # ------------------------------------------------------------ viewer links (signed, stateless)
    def link(self, mid: str, seconds: float = LINK_SECONDS) -> str:
        exp = int(self.now() + seconds)
        body = f"{mid}.{exp}"
        mac = base64.urlsafe_b64encode(hmac.new(self.key, body.encode(), hashlib.sha256).digest()[:18]).decode()
        return self.url(f"/m/{mid[2:]}.{exp}.{mac}")

    def open_link(self, token: str):
        """token -> media row, or LookupError / TimeoutError. Checked by signature: nothing is stored."""
        parts = (token or "").split(".")
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{16}", parts[0]) or not parts[1].isdigit():
            raise LookupError("not found")
        mid, exp = "m_" + parts[0], int(parts[1])
        want = base64.urlsafe_b64encode(hmac.new(self.key, f"{mid}.{exp}".encode(), hashlib.sha256).digest()[:18]).decode()
        if not hmac.compare_digest(want, parts[2]):
            raise LookupError("not found")
        if exp < self.now():
            raise TimeoutError("expired")
        r = self.row(mid)
        if r["revoked"]:
            raise LookupError("not found")
        return r

    def revoke(self, handle: str, mid: str) -> None:
        with self.store.tx() as c:
            n = c.execute("UPDATE media SET revoked=? WHERE id=? AND created_by=?", (self.now(), mid, handle)).rowcount
        if not n:
            raise MediaError("Only the one who shared it can remove it.")

    def blob(self, artifact: Optional[str]) -> Optional[str]:
        if not artifact:
            return None
        p = self.store.blob_path(artifact)
        return p if os.path.exists(p) else None

    # ------------------------------------------------------------ looking closer
    def pictures(self, mid: str, at: Optional[float] = None) -> list:
        """JPEG bytes an agent can look at: the contact sheet / the image, or frames around `at`."""
        r = self.row(mid)
        if r["status"] != "ready":
            return []
        if at is None:
            p = self.blob(r["sheet"] or r["preview"])
            return [open(p, "rb").read()] if p else []
        if r["kind"] != "video":
            return []
        src = self.blob(r["playable"] or r["artifact"])
        dur = float(json.loads(r["digest"] or "{}").get("duration") or 0)
        work = os.path.join(self.tmp, "look_" + uuid.uuid4().hex[:8])
        os.makedirs(work)
        try:
            out = []
            for i, t in enumerate(sorted({min(max(at + d, 0), max(dur - 0.05, 0)) for d in (-1.0, 0.0, 1.0)})):
                p = os.path.join(work, f"f{i}.jpg")
                if src and grab(src, t, p, 768, label=clock(t)):
                    out.append(open(p, "rb").read())
            return out
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ------------------------------------------------------------ upload links (for people)
    def new_upload(self, handle: str, session: Optional[str], anchor: Optional[int], note: Optional[str]) -> str:
        token = secrets.token_urlsafe(24)
        with self.store.tx() as c:
            c.execute("INSERT INTO upload_links(id,token_hash,handle,session,anchor,note,created,expires)"
                      " VALUES (?,?,?,?,?,?,?,?)", ("u_" + uuid.uuid4().hex[:12], _hash(token), handle, session, anchor,
                                                    (note or "")[:300] or None, self.now(), self.now() + UPLOAD_SECONDS))
        return self.url(f"/up/{token}")

    def upload_link(self, token: str):
        if not TOKEN_RE.match(token or ""):
            raise LookupError("not found")
        with self.store.ro() as c:
            r = c.execute("SELECT * FROM upload_links WHERE token_hash=?", (_hash(token),)).fetchone()
        if not r:
            raise LookupError("not found")
        if r["expires"] < self.now():
            raise TimeoutError("expired")
        return r

    def start_file(self, token: str, name: str, size: int, mime: str) -> dict:
        """One file of an upload. The same name and size again resumes where it stopped."""
        link = self.upload_link(token)
        if not isinstance(size, int) or size <= 0:
            raise MediaError("That file is empty.")
        if size > self.max_upload:
            raise MediaError(f"That file is over {self.max_upload // 2 ** 20} MB.")
        name = safe_name(name)
        with self.store.tx() as c:
            same = c.execute("SELECT * FROM upload_files WHERE link=? AND name=? AND size=? AND media IS NULL"
                             " ORDER BY created DESC LIMIT 1", (link["id"], name, size)).fetchone()
            if same and os.path.exists(self._part(same["id"])):
                return {"file": same["id"], "received": os.path.getsize(self._part(same["id"])), "chunk": CHUNK}
            done = c.execute("SELECT COUNT(*) AS n FROM upload_files WHERE link=? AND media IS NOT NULL",
                             (link["id"],)).fetchone()["n"]
            if done >= UPLOAD_FILES:
                raise MediaError(f"This link has taken {UPLOAD_FILES} files already. Ask for another link.")
            fid = "f_" + uuid.uuid4().hex[:16]
            c.execute("INSERT INTO upload_files(id,link,name,mime,size,created) VALUES (?,?,?,?,?,?)",
                      (fid, link["id"], name, (mime or "")[:100], size, self.now()))
        open(self._part(fid), "wb").close()
        return {"file": fid, "received": 0, "chunk": CHUNK}

    def _part(self, fid: str) -> str:
        return os.path.join(self.tmp, f"{fid}.part")

    def _file(self, token: str, fid: str):
        link = self.upload_link(token)
        if not re.fullmatch(r"f_[0-9a-f]{16}", fid or ""):
            raise LookupError("not found")
        with self.store.ro() as c:
            f = c.execute("SELECT * FROM upload_files WHERE id=? AND link=?", (fid, link["id"])).fetchone()
        if not f:
            raise LookupError("not found")
        return link, f

    def put_chunk(self, token: str, fid: str, offset: int, data: bytes) -> int:
        """Append a piece at `offset`. A piece that doesn't follow on is refused with where to resume."""
        link, f = self._file(token, fid)
        part = self._part(fid)
        have = os.path.getsize(part) if os.path.exists(part) else None
        if have is None or f["media"]:
            raise MediaError("This file is already uploaded.")
        if offset != have:
            return have
        if len(data) > CHUNK + 1024 or have + len(data) > f["size"]:
            raise MediaError("That piece doesn't fit the file's size.")
        with open(part, "ab") as out:
            out.write(data)
        return have + len(data)

    def finish_file(self, token: str, fid: str) -> dict:
        link, f = self._file(token, fid)
        if f["media"]:
            return self.get(f["media"])
        part = self._part(fid)
        if not os.path.exists(part) or os.path.getsize(part) != f["size"]:
            raise MediaError("The file isn't complete yet.")
        m = self.from_file(link["handle"], part, f["name"])
        with self.store.tx() as c:
            c.execute("UPDATE upload_files SET media=?, received=? WHERE id=?", (m["id"], f["size"], fid))
            c.execute("UPDATE upload_links SET files=files+1 WHERE id=?", (link["id"],))
            if m["name"] != f["name"]:
                c.execute("UPDATE media SET name=? WHERE id=?", (f["name"], m["id"]))
        if self.on_upload:
            self.on_upload(dict(link), m["id"])
        return self.get(m["id"])

    # ------------------------------------------------------------ the worker
    def _queued(self) -> bool:
        with self.store.ro() as c:
            return bool(c.execute("SELECT 1 FROM media WHERE status='queued' LIMIT 1").fetchone())

    def _ensure_thread(self) -> None:
        if not self.threads:
            return
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.wake.set()
                return
            self.thread = threading.Thread(target=self._loop, name="media", daemon=True)
            self.thread.start()

    def resume(self) -> None:
        with self.store.tx() as c:
            for r in c.execute("SELECT id, attempts FROM media WHERE status='working'").fetchall():
                if r["attempts"] >= 2:
                    c.execute("UPDATE media SET status='failed', stage=NULL, error=?, updated=? WHERE id=?",
                              ("The hub restarted twice while processing this. Send it again.", self.now(), r["id"]))
                else:
                    c.execute("UPDATE media SET status='queued', stage='queued', updated=? WHERE id=?", (self.now(), r["id"]))
        for name in os.listdir(self.tmp):
            if name.startswith(("work_", "look_")):
                shutil.rmtree(os.path.join(self.tmp, name), ignore_errors=True)
        if self._queued():
            self._ensure_thread()

    def _loop(self) -> None:
        idle_since = None
        while True:
            try:
                did = self.run_once()
            except Exception as e:
                self.log(f"[relay] media: {type(e).__name__}: {str(e)[:200]}")
                did = False
            if did:
                idle_since = None
                continue
            idle_since = idle_since or time.monotonic()
            with self.lock:
                try:
                    waiting = self._queued()
                except Exception:
                    waiting = False
                if not waiting and time.monotonic() - idle_since >= self.idle:
                    self.thread = None
                    return
            if not waiting:
                self.wake.wait(min(5.0, max(self.idle, 0.05)))
                self.wake.clear()

    def run_once(self) -> bool:
        with self.store.tx() as c:
            r = c.execute("SELECT * FROM media WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return False
            c.execute("UPDATE media SET status='working', stage='starting', attempts=attempts+1, updated=? WHERE id=?",
                      (self.now(), r["id"]))
        self._process(self.row(r["id"]))
        return True

    def _set(self, mid: str, **cols) -> None:
        cols["updated"] = self.now()
        with self.store.tx() as c:
            c.execute(f"UPDATE media SET {', '.join(k + '=?' for k in cols)} WHERE id=?", (*cols.values(), mid))

    def _process(self, r) -> None:
        mid = r["id"]
        work = os.path.join(self.tmp, "work_" + mid)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        try:
            self._make(r, work)
        except (MediaError, captions.CaptionError) as e:
            self._set(mid, status="failed", stage=None, error=str(e)[:600])
        except subprocess.TimeoutExpired:
            self._set(mid, status="failed", stage=None, error="Processing took too long.")
        except Exception as e:
            self.log(f"[relay] media {mid}: {type(e).__name__}: {str(e)[:300]}")
            self._set(mid, status="failed", stage=None, error="Something went wrong processing this file.")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _make(self, r, work: str) -> None:
        mid, by = r["id"], r["created_by"]
        artifact = r["artifact"]
        if not artifact:                                 # a link: fetch it first
            self._set(mid, stage="downloading")
            dl = os.path.join(work, "download")
            captions.download(r["source_url"], dl, self.private_host, self.allow_private,
                              cap=min(self.max_upload, captions.MAX_DOWNLOAD * 4))
            artifact, size = self._store(dl, r["name"] or "link", by, None, "upload", "application/octet-stream")
            self._set(mid, artifact=artifact, size=size)
        with self.store.ro() as c:                       # the same bytes were understood already: reuse that
            twin = c.execute("SELECT * FROM media WHERE artifact=? AND status='ready' AND id!=? ORDER BY created LIMIT 1",
                             (artifact, mid)).fetchone()
        if twin:
            self._set(mid, status="ready", stage=None, kind=twin["kind"], mime=twin["mime"], digest=twin["digest"],
                      playable=twin["playable"], poster=twin["poster"], sheet=twin["sheet"], preview=twin["preview"],
                      artifact=twin["artifact"])
            return
        src = self.store.blob_path(artifact)
        self._set(mid, stage="checking")
        info = sniff(src)
        kind = info.pop("kind")
        mime = info.pop("mime")
        self._set(mid, kind=kind, mime=mime, size=os.path.getsize(src))
        {"video": self._video, "audio": self._audio, "image": self._image}.get(kind, self._other)(
            mid, by, src, info, work, artifact)

    def _keep(self, path: str, name: str, by: str, parent: str, kind: str, mime: str) -> str:
        return self._store(path, name, by, parent, kind, mime)[0]

    def _typed(self, artifact: str, mime: str) -> None:
        """The original plays as it is: give it its real type so browsers play it instead of downloading."""
        with self.store.tx() as c:
            c.execute("UPDATE artifacts SET mime=? WHERE id=? AND mime='application/octet-stream'", (mime, artifact))

    def _listen(self, mid: str, src: str, duration: float, work: str) -> dict:
        self._set(mid, stage="listening")
        d = sound(src, duration)
        if not self.transcriber or duration <= 0:
            return {**d, "transcribed": False}
        self._set(mid, stage="transcribing")
        pcm = os.path.join(work, "speech.pcm")
        _ff(900, "-i", src, "-t", f"{min(duration, MAX_TRANSCRIBE):.2f}", "-vn", "-ac", "1",
            "-ar", str(captions.SAMPLE_RATE), "-f", "s16le", pcm)
        try:
            words = self.transcriber.words(pcm)
        except captions.CaptionError:
            return {**d, "transcribed": False}
        return {**d, "transcribed": True, "transcript": transcript_lines(words),
                "transcript_cut_at": MAX_TRANSCRIBE if duration > MAX_TRANSCRIBE else None}

    def _video(self, mid, by, src, info, work, artifact) -> None:
        dur = info["duration"]
        self._set(mid, stage="finding shots")
        shots = shot_changes(src, dur)
        self._set(mid, stage="making the contact sheet")
        times = sheet_times(dur, shots)
        sheet = contact_sheet(src, times, work)
        poster = os.path.join(work, "poster.jpg")
        has_poster = grab(src, min(1.0, dur / 3), poster, 1280)
        d = {"duration": round(dur, 2), "width": info["width"], "height": info["height"], "fps": info.get("fps"),
             "video_codec": info.get("vcodec"), "audio_codec": info.get("acodec"), "has_audio": info["has_audio"],
             "shots": shots, "sheet_times": times}
        if info["has_audio"]:
            d.update(self._listen(mid, src, dur, work))
        playable = artifact
        native = info.get("vcodec") in PLAYABLE_VIDEO and (info.get("acodec") in PLAYABLE_AUDIO + (None,)) \
            and "mp4" in info.get("container", "")
        if not native:
            if dur <= MAX_TRANSCODE:
                self._set(mid, stage="making a copy every browser plays")
                out = os.path.join(work, "play.mp4")
                _ff(max(600, dur * 4), "-i", src, "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-vf", "scale='min(1280,iw)':-2", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", out)
                playable = self._keep(out, "play.mp4", by, artifact, "playable", "video/mp4")
            else:
                playable = None
                d["playable_note"] = "too long to convert; download the original"
        else:
            self._typed(artifact, "video/mp4")
        self._set(mid, status="ready", stage=None, digest=json.dumps(d), playable=playable,
                  poster=self._keep(poster, "poster.jpg", by, artifact, "poster", "image/jpeg") if has_poster else None,
                  sheet=self._keep(sheet, "sheet.jpg", by, artifact, "sheet", "image/jpeg") if sheet else None)

    def _audio(self, mid, by, src, info, work, artifact) -> None:
        dur = info["duration"]
        d = {"duration": round(dur, 2), "audio_codec": info.get("acodec"), "channels": info.get("channels")}
        d.update(self._listen(mid, src, dur, work))
        playable = artifact
        if info.get("acodec") not in PLAYABLE_AUDIO or not any(x in info.get("container", "") for x in ("mp3", "mp4", "mov", "aac")):
            self._set(mid, stage="making a copy every browser plays")
            out = os.path.join(work, "play.m4a")
            _ff(max(600, dur * 2), "-i", src, "-vn", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out)
            playable = self._keep(out, "play.m4a", by, artifact, "playable", "audio/mp4")
        else:
            self._typed(artifact, "audio/mpeg" if info.get("acodec") == "mp3" else "audio/mp4")
        self._set(mid, status="ready", stage=None, digest=json.dumps(d), playable=playable)

    def _image(self, mid, by, src, info, work, artifact) -> None:
        """Re-encode without metadata: location, camera and time never leave the hub. Only the clean copy is kept."""
        self._set(mid, stage="removing location data")
        with open(src, "rb") as f:
            png = f.read(8).startswith(b"\x89PNG")
        clean = os.path.join(work, "clean.png" if png else "clean.jpg")
        try:
            _ff(120, "-i", src, "-map_metadata", "-1", "-frames:v", "1", *([] if png else ["-q:v", "2"]), clean)
        except RuntimeError:
            raise MediaError("The hub can't read this image format. Send it as JPEG or PNG.") from None
        dims = _run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                     "-of", "json", clean], 60)
        st = (json.loads(dims.stdout).get("streams") or [{}])[0]
        w, h = int(st.get("width") or 0), int(st.get("height") or 0)
        prev = os.path.join(work, "preview.jpg")
        scale = f"scale='if(gt(iw,ih),min({PREVIEW_PX},iw),-2)':'if(gt(iw,ih),-2,min({PREVIEW_PX},ih))'"
        _ff(120, "-i", clean, "-frames:v", "1", "-vf", scale, "-q:v", "3", prev)
        name = (self.row(mid)["name"] or "image")
        clean_id = self._keep(clean, os.path.splitext(name)[0] + (".png" if png else ".jpg"), by, None, "image",
                              "image/png" if png else "image/jpeg")
        d = {"width": w, "height": h, "stripped": True}
        self._set(mid, status="ready", stage=None, digest=json.dumps(d), artifact=clean_id, playable=clean_id,
                  mime="image/png" if png else "image/jpeg", size=os.path.getsize(self.store.blob_path(clean_id)),
                  preview=self._keep(prev, "preview.jpg", by, clean_id, "preview", "image/jpeg"))
        if clean_id != artifact:
            self._forget_original(artifact)

    def _forget_original(self, artifact: str) -> None:
        """Drop an uploaded image's original (with its location data) unless something else uses it."""
        with self.store.tx() as c:
            a = c.execute("SELECT kind FROM artifacts WHERE id=?", (artifact,)).fetchone()
            used = c.execute("SELECT 1 FROM media WHERE artifact=? AND status!='failed' LIMIT 1", (artifact,)).fetchone() or \
                c.execute("SELECT 1 FROM messages WHERE artifacts LIKE ? LIMIT 1", (f'%"{artifact}"%',)).fetchone()
            if not a or a["kind"] != "upload" or used:
                return
            c.execute("DELETE FROM artifacts WHERE id=?", (artifact,))
        try:
            os.remove(self.store.blob_path(artifact))
        except OSError:
            pass

    def _other(self, mid, by, src, info, work, artifact) -> None:
        self._set(mid, status="ready", stage=None, digest=json.dumps({"note": "kept as a download"}))


# --------------------------------------------------------------------------- pages and routes
PAGE_CSS = """
:root{--bg:#0f1115;--fg:#eef0f4;--muted:#9aa3b2;--card:#181b22;--line:#262b36;--accent:#7cc4ff;--on-accent:#0b1220}
@media (prefers-color-scheme: light){:root{--bg:#f6f7f9;--fg:#14171d;--muted:#5b6472;--card:#fff;--line:#e2e5ea;--accent:#0a66c2;--on-accent:#fff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:20px 16px 48px}h1{font-size:1.25rem;margin:0 0 4px;word-break:break-word}
.meta{color:var(--muted);font-size:.9rem;margin-bottom:16px}.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px;margin:14px 0}
video,img.full{width:100%;max-height:75vh;border-radius:10px;background:#000;display:block}audio{width:100%}
.sheet{width:100%;border-radius:8px}.t{display:flex;gap:10px;padding:4px 0;border-bottom:1px solid var(--line)}
.t button{background:none;border:0;color:var(--accent);font:inherit;cursor:pointer;padding:0;min-width:3.6em;text-align:left}
.btn{display:inline-block;background:var(--accent);color:var(--on-accent);border:0;border-radius:10px;padding:12px 18px;font-weight:600;text-decoration:none;font-size:1rem;cursor:pointer}
.drop{border:2px dashed var(--line);border-radius:14px;padding:28px 16px;text-align:center}.bar{height:8px;background:var(--line);border-radius:4px;overflow:hidden;margin-top:8px}
.bar>i{display:block;height:100%;width:0;background:var(--accent);transition:width .2s}.file{margin:10px 0}.small{color:var(--muted);font-size:.85rem}
"""

UPLOAD_JS = r"""
const box=document.getElementById('files'),pick=document.getElementById('pick'),drop=document.getElementById('drop');
const base=location.pathname;
function row(f){const d=document.createElement('div');d.className='file';d.innerHTML='<div></div><div class="bar"><i></i></div><div class="small"></div>';
 d.children[0].textContent=f.name+' ('+(f.size/1048576).toFixed(1)+' MB)';box.appendChild(d);return d}
class Refused extends Error{}
async function j(url,opt){            // keeps trying through dropped connections; stops only if the hub says no
 for(let i=0;;i++){let r=null;try{r=await fetch(url,opt)}catch(e){}
  if(r&&r.ok)return r.json();
  if(r&&r.status<500){const b=await r.json().catch(()=>({}));throw new Refused(b.detail||('error '+r.status))}
  await new Promise(s=>setTimeout(s,Math.min(30000,1000*2**Math.min(i,5))))}}
async function send(f){const d=row(f),bar=d.querySelector('i'),msg=d.querySelector('.small');
 try{let s=await j(base+'/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:f.name,size:f.size,type:f.type})});
  let off=s.received;while(off<f.size){msg.textContent='Uploading… '+Math.floor(100*off/f.size)+'%';bar.style.width=(100*off/f.size)+'%';
   const r=await j(base+'/'+s.file+'?offset='+off,{method:'PUT',body:f.slice(off,off+s.chunk)});off=r.received}
  bar.style.width='100%';msg.textContent='Finishing…';const m=await j(base+'/'+s.file+'/done',{method:'POST'});
  msg.textContent='Sent ✓ Your agent has it now.'}catch(e){msg.textContent='Not sent: '+e.message}}
async function all(list){pick.disabled=true;for(const f of list)await send(f);pick.disabled=false}
pick.addEventListener('change',()=>all([...pick.files]));
drop.addEventListener('dragover',e=>{e.preventDefault()});drop.addEventListener('drop',e=>{e.preventDefault();all([...e.dataTransfer.files])});
"""

VIEW_JS = r"""
const v=document.querySelector('video,audio');document.querySelectorAll('[data-t]').forEach(b=>b.addEventListener('click',()=>{if(v){v.currentTime=+b.dataset.t;v.play()}}));
"""


class MediaIn(BaseModel):
    url: Optional[str] = None
    artifact: Optional[str] = None


class StartIn(BaseModel):
    name: str
    size: int
    type: Optional[str] = None


def page(title: str, body: str, nonce: str) -> str:
    return (f"<!doctype html><html lang=en><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<meta name=robots content=noindex><title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body><main>{body}</main></body></html>")


def mount(app, hub, media: Media) -> dict:
    auth = hub.auth

    def headers(nonce: Optional[str] = None) -> dict:
        script = f"'nonce-{nonce}'" if nonce else "'none'"
        return {"Content-Security-Policy": (f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; "
                                            "img-src 'self'; media-src 'self'; connect-src 'self'; base-uri 'none'; "
                                            "form-action 'none'; frame-ancestors 'none'"),
                "X-Content-Type-Options": "nosniff", "X-Robots-Tag": "noindex, nofollow",
                "Referrer-Policy": "no-referrer", "Cache-Control": "private, no-store"}

    def member(me) -> str:
        if me["role"] == "admin":
            raise HTTPException(403, "use your own key")
        return me["handle"]

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except MediaError as e:
            raise HTTPException(400, str(e)) from None

    def gone(msg="This link has expired. Ask for a new one.") -> HTMLResponse:
        return HTMLResponse(page("Link expired", f"<h1>{html.escape(msg)}</h1>", ""), status_code=410, headers=headers())

    # ------------------------------------------------------------ agents (REST)
    @app.post("/media")
    def add_media(body: MediaIn, me: dict = Depends(auth)):
        by = member(me)
        if bool(body.url) == bool(body.artifact):
            raise HTTPException(400, "send url or artifact")
        m = guard(media.from_url, by, body.url) if body.url else guard(media.from_artifact, by, body.artifact)
        return {**m, "link": media.link(m["id"])}

    @app.get("/media/{mid}")
    def get_media(mid: str, me: dict = Depends(auth)):
        m = guard(media.check_access, member(me), mid)
        return {**m, "link": media.link(mid), "card": media.card(m)}

    @app.delete("/media/{mid}")
    def remove_media(mid: str, me: dict = Depends(auth)):
        guard(media.revoke, member(me), mid)
        return {"removed": mid}

    # ------------------------------------------------------------ viewer pages (people, no key)
    def opened(token: str):
        try:
            return media.open_link(token)
        except (LookupError, MediaError):
            raise HTTPException(404, "not found") from None
        except TimeoutError:
            raise HTTPException(410, "This link has expired.") from None

    @app.get("/m/{token}", include_in_schema=False)
    def view(token: str):
        try:
            r = media.open_link(token)
        except (LookupError, MediaError):
            raise HTTPException(404, "not found") from None
        except TimeoutError:
            return gone()
        m = media.get(r["id"])
        d, kind, base = m["digest"], m["kind"], f"/m/{token}"
        nonce = secrets.token_urlsafe(12)
        name = html.escape(m["name"] or kind or "Media")
        bits = [x for x in (hms(d["duration"]) if d.get("duration") else None,
                            f"{d['width']}×{d['height']}" if d.get("width") else None,
                            size_text(m["size"]) if m.get("size") else None, f"shared by {m['from']}") if x]
        out = [f"<h1>{name}</h1><div class=meta>{html.escape(' · '.join(bits))}</div>"]
        if m["status"] != "ready":
            out.append("<div class=card>" + ("Still being processed. Refresh in a moment." if m["status"] in ("queued", "working")
                                             else html.escape("Couldn't be processed: " + (m["error"] or ""))) + "</div>")
        elif kind == "video" and m["has"]["playable"]:
            poster = f" poster='{base}/poster'" if m["has"]["poster"] else ""
            out.append(f"<video controls playsinline preload=metadata src='{base}/file'{poster}></video>")
        elif kind == "audio":
            out.append(f"<audio controls preload=metadata src='{base}/file'></audio>")
        elif kind == "image":
            out.append(f"<img class=full src='{base}/file' alt='{name}'>")
        if m["status"] == "ready":
            out.append(f"<p><a class=btn href='{base}/original'>Download</a></p>")
        if d.get("transcript"):
            rows = "".join(f"<div class=t><button data-t='{x['t']}'>{clock(x['t'])}</button><span>{html.escape(x['text'])}</span></div>"
                           for x in d["transcript"])
            out.append(f"<div class=card><b>Transcript</b>{rows}</div>")
        if m["has"]["sheet"]:
            out.append(f"<div class=card><b>Shots</b><img class=sheet src='{base}/sheet' alt='contact sheet'></div>")
        out.append(f"<script nonce='{nonce}'>{VIEW_JS}</script>")
        return HTMLResponse(page(m["name"] or "Media", "".join(out), nonce), headers=headers(nonce))

    def serve(token: str, which: str, download: bool = False):
        r = opened(token)
        aid = {"file": r["playable"] or r["artifact"], "poster": r["poster"], "sheet": r["sheet"],
               "preview": r["preview"]}.get(which)
        path = media.blob(aid)
        if not path or r["status"] != "ready":
            raise HTTPException(404, "not found")
        with media.store.ro() as c:
            a = c.execute("SELECT name, mime FROM artifacts WHERE id=?", (aid,)).fetchone()
        mime = (a["mime"] if a else None) or "application/octet-stream"
        inline = r["kind"] in ("video", "audio", "image") and mime.split("/")[0] in ("video", "audio", "image") \
            and mime != "image/svg+xml" and not download
        name = r["name"] or (a["name"] if a else "file")
        if which == "file" and r["playable"] and r["playable"] != r["artifact"]:
            name = os.path.splitext(name)[0] + os.path.splitext((a["name"] if a else "") or ".mp4")[1]
        return FileResponse(path, media_type=mime if inline else "application/octet-stream", filename=name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={**headers(), "Cache-Control": "private, max-age=3600"})

    @app.get("/m/{token}/file", include_in_schema=False)
    def view_file(token: str, download: int = 0):
        return serve(token, "file", bool(download))

    @app.get("/m/{token}/original", include_in_schema=False)
    def view_original(token: str):
        """The exact file as it was shared (a photo: the copy without location data), always as a download.
        Videos and other files are available at once; a photo only once its location data is gone."""
        r = opened(token)
        if r["status"] not in ("ready", "failed") and (r["kind"] in (None, "image")):
            raise HTTPException(409, "Still being prepared. Try again in a minute.")
        path = media.blob(r["artifact"])
        if not path:
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="application/octet-stream", filename=r["name"] or "file",
                            content_disposition_type="attachment",
                            headers={**headers(), "Cache-Control": "private, max-age=3600"})

    @app.get("/m/{token}/{which}", include_in_schema=False)
    def view_part(token: str, which: str):
        if which not in ("poster", "sheet", "preview"):
            raise HTTPException(404, "not found")
        return serve(token, which)

    # ------------------------------------------------------------ upload links (people, no key)
    @app.get("/up/{token}", include_in_schema=False)
    def upload_page(token: str):
        try:
            link = media.upload_link(token)
        except LookupError:
            raise HTTPException(404, "not found") from None
        except TimeoutError:
            return gone("This upload link has expired. Ask your agent for a new one.")
        nonce = secrets.token_urlsafe(12)
        note = f"<div class=card>{html.escape(link['note'])}</div>" if link["note"] else ""
        body = (f"<h1>Send files to {html.escape(link['handle'])}</h1>"
                f"<div class=meta>Photos, videos, audio or any file, up to {media.max_upload // 2 ** 20} MB each. "
                f"Big files keep going if your connection drops. This link works until "
                f"{time.strftime('%b %d, %H:%M UTC', time.gmtime(link['expires']))}.</div>{note}"
                f"<div id=drop class=drop><label class=btn>Choose files<input id=pick type=file multiple hidden></label>"
                f"<p class=small>or drop them here</p></div><div id=files></div>"
                f"<p class=small>Location data is removed from photos before anyone sees them.</p>"
                f"<script nonce='{nonce}'>{UPLOAD_JS}</script>")
        return HTMLResponse(page(f"Send files to {link['handle']}", body, nonce), headers=headers(nonce))

    def up_guard(fn, *a):
        try:
            return fn(*a)
        except LookupError:
            raise HTTPException(404, "This upload link isn't valid.") from None
        except TimeoutError:
            raise HTTPException(410, "This upload link has expired. Ask your agent for a new one.") from None
        except MediaError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/up/{token}/start", include_in_schema=False)
    def upload_start(token: str, body: StartIn):
        return up_guard(media.start_file, token, body.name, body.size, body.type or "")

    @app.put("/up/{token}/{fid}", include_in_schema=False)
    async def upload_chunk(token: str, fid: str, request: Request, offset: int = Query(..., ge=0)):
        data = b""
        async for chunk in request.stream():
            data += chunk
            if len(data) > CHUNK + 1024:
                raise HTTPException(413, "piece too large")
        from starlette.concurrency import run_in_threadpool
        return {"received": await run_in_threadpool(up_guard, media.put_chunk, token, fid, offset, data)}

    @app.post("/up/{token}/{fid}/done", include_in_schema=False)
    def upload_done(token: str, fid: str):
        m = up_guard(media.finish_file, token, fid)
        return {"media": m["id"], "status": m["status"]}

    return {}
