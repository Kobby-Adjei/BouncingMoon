"""Caption clip: turn a gameplay clip into a vertical short with word-by-word captions, on the hub.

Someone hands the hub a clip (a link to a video file, or a file already uploaded to the hub). The hub
cuts it to 9:16 at 1080x1920, dropping the edges of a 16:9 frame where a streamer's facecam usually
sits, transcribes the speech with word timings, and burns in big captions in the middle of the screen,
at most three words at a time, with the word being spoken highlighted. The result is stored as an
artifact and gets a share link: an unguessable URL that works without a key for 7 days, so a posting
app or another agent can fetch the MP4.

One job at a time, in a background thread of the hub process, from a queue table, so a restart picks
up where it left off (or says plainly that a job failed). Transcription runs here, in the cloud, with
a local speech model baked into the image; nobody sends a key. The transcriber sits behind a one-method
interface (`words(pcm_path)`) so tests use a fake.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any, Callable, Optional, Union

from fastapi import Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

OUT_W, OUT_H = 1080, 1920
MAX_SECONDS = 180.0                  # longest short we make
MAX_DOWNLOAD = 1024 ** 3             # 1 GB
DOWNLOAD_SECONDS = 600               # whole download, not per read: a slow drip can't hold the queue
RENDER_SECONDS = 1800
SHARE_SECONDS = 7 * 86400
MAX_WORDS = 3
MAX_QUEUED = 20
SAMPLE_RATE = 16000
HIGHLIGHT = "&H00E6FF&"              # ASS colours are BGR: this is #FFE600, a warm yellow
YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtube.googleapis.com")
YOUTUBE_MSG = ("YouTube blocks cloud servers from downloading video. "
               "Download the moment and send the file or a link to it instead.")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
ID_RE = re.compile(r"^c_[0-9a-f]{12}$")
# bold fonts the image ships (Dockerfile), best first: (file, family name for ASS, needs the Bold flag)
FONTS = (("/usr/share/fonts/opentype/montserrat/Montserrat-ExtraBold.otf", "Montserrat ExtraBold", False),
         ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", "Liberation Sans", True),
         ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVu Sans", True))
STAGES = {"queued": "Waiting", "downloading": "Downloading", "checking": "Checking", "transcribing": "Transcribing",
          "cutting": "Cutting", "saving": "Saving"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS captions(
  id TEXT PRIMARY KEY, by TEXT NOT NULL, status TEXT NOT NULL, stage TEXT, progress REAL,
  url TEXT, file_id TEXT, start_s REAL, end_s REAL, crop TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0, error TEXT, result TEXT, artifact TEXT,
  share TEXT, share_expires REAL, origin TEXT, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS captions_queue ON captions(status, created);
CREATE TABLE IF NOT EXISTS shares(
  token_hash TEXT PRIMARY KEY, caption TEXT NOT NULL, artifact TEXT NOT NULL, created REAL, expires REAL NOT NULL);
"""


class CaptionError(Exception):
    """A plain-language reason the short can't be made."""


# --------------------------------------------------------------------------- inputs
def seconds(v: Any) -> Optional[float]:
    """75, "75.5", "1:15", "1:02:03" -> seconds. None or "" -> None."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, bool):
        raise CaptionError("Times are seconds or mm:ss, like 75 or 1:15.")
    if isinstance(v, (int, float)):
        if v < 0 or v != v or v == float("inf"):
            raise CaptionError("Times can't be negative.")
        return float(v)
    parts = str(v).strip().split(":")
    if len(parts) > 3 or not all(re.fullmatch(r"\d+(\.\d+)?", p) for p in parts) \
            or any("." in p for p in parts[:-1]):
        raise CaptionError(f"'{str(v)[:20]}' isn't a time. Use seconds or mm:ss, like 75 or 1:15.")
    nums = [float(p) for p in parts]
    if len(nums) > 1 and nums[-1] >= 60 or len(nums) == 3 and nums[1] >= 60:
        raise CaptionError(f"'{v}' isn't a time: minutes and seconds go up to 59.")
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def crop_position(v: Any) -> tuple[str, float]:
    """center | left | right | a number 0-1 (where the 9:16 slice sits across the frame)."""
    if v is None or v == "":
        return "center", 0.5
    named = {"center": 0.5, "centre": 0.5, "middle": 0.5, "left": 0.0, "right": 1.0}
    if isinstance(v, str) and v.strip().lower() in named:
        k = v.strip().lower()
        return ("center" if named[k] == 0.5 else k), named[k]
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise CaptionError("crop is center, left, right, or a number from 0 (left) to 1 (right).") from None
    if isinstance(v, bool) or not 0 <= x <= 1:
        raise CaptionError("crop is center, left, right, or a number from 0 (left) to 1 (right).")
    return f"{x:g}", x


def _host(url: str) -> str:
    return (urllib.parse.urlparse(url).hostname or "").lower().rstrip(".")


def is_youtube(url: str) -> bool:
    h = _host(url if "://" in url else "https://" + url)
    return any(h == d or h.endswith("." + d) for d in YOUTUBE_HOSTS)


def direct_url(url: str) -> str:
    """The link to download: share pages of Google Drive and Dropbox become their direct-download form."""
    url = (url or "").strip()
    if is_youtube(url):
        raise CaptionError(YOUTUBE_MSG)
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise CaptionError("Send an http or https link to a video file.")
    host = _host(url)
    if host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        m = re.search(r"/(?:file/)?d/([A-Za-z0-9_-]{10,})", p.path)
        fid = m.group(1) if m else (urllib.parse.parse_qs(p.query).get("id") or [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{10,}", fid or ""):
            raise CaptionError("Couldn't find the file in that Google Drive link. Send the file's share link.")
        # confirm=t skips the "can't scan this file for viruses" page Drive shows for big files
        return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"
    if host == "dropbox.com" or host.endswith(".dropbox.com"):
        q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query, keep_blank_values=True) if k not in ("dl", "raw")]
        return urllib.parse.urlunparse(p._replace(query=urllib.parse.urlencode(q + [("dl", "1")])))
    return url


def shown_url(url: Optional[str]) -> Optional[str]:
    """A link as results show it: no query or fragment, which is where share keys and signatures live."""
    if not url:
        return None
    p = urllib.parse.urlparse(url)
    return urllib.parse.urlunparse((p.scheme, p.netloc.rsplit("@", 1)[-1], p.path, "", "", ""))   # no user:password either


# --------------------------------------------------------------------------- download, guarded
def _public(ip: str) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global


def check_host(host: str, private_host: Callable[[str], bool]) -> None:
    """Refuse hosts that are, or resolve to, this machine or a private network (cloud metadata included)."""
    if private_host(host):
        raise CaptionError("That link points at a private or local address.")
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        raise CaptionError(f"Couldn't find the server for that link ({host}).") from None
    if not infos or not all(_public(i[4][0]) for i in infos):
        raise CaptionError("That link points at a private or local address.")


def guarded_opener(private_host: Callable[[str], bool], allow_private: bool = False):
    """A urlopen that checks every hop: each redirect's host, and the address each connection really
    reached (so a name that resolves somewhere else a second time still can't reach the inside)."""

    def peer_ok(sock):
        if allow_private:
            return
        try:
            ip = sock.getpeername()[0]
        except OSError:
            return
        if not _public(ip):
            sock.close()
            raise CaptionError("That link points at a private or local address.")

    class HTTPConn(http.client.HTTPConnection):
        def connect(self):
            super().connect()
            peer_ok(self.sock)

    class HTTPSConn(http.client.HTTPSConnection):
        def connect(self):
            super().connect()
            peer_ok(self.sock)

    class HTTPHandler(urllib.request.HTTPHandler):
        def http_open(self, req):
            return self.do_open(HTTPConn, req)

    class HTTPSHandler(urllib.request.HTTPSHandler):
        def https_open(self, req):
            return self.do_open(HTTPSConn, req, context=self._context)

    class Redirects(urllib.request.HTTPRedirectHandler):
        max_redirections = 8

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if is_youtube(newurl):
                raise CaptionError(YOUTUBE_MSG)
            if urllib.parse.urlparse(newurl).scheme not in ("http", "https"):
                raise CaptionError("That link redirects somewhere that isn't a web address.")
            if not allow_private:
                check_host(_host(newurl), private_host)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    # no proxy handler: a proxy would hide which address the connection really reached
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), HTTPHandler, HTTPSHandler, Redirects)


def download(url: str, dest: str, private_host: Callable[[str], bool], allow_private: bool = False,
             cap: int = MAX_DOWNLOAD, budget: float = DOWNLOAD_SECONDS, opener=None) -> str:
    url = direct_url(url)
    if not allow_private:
        check_host(_host(url), private_host)
    opener = opener or guarded_opener(private_host, allow_private)
    req = urllib.request.Request(url, headers={"User-Agent": "Relay/1.0 (caption clip)", "Accept": "video/*,*/*;q=0.5"})
    deadline = time.monotonic() + budget
    drive = "drive.usercontent.google.com" in url
    try:
        with opener.open(req, timeout=30) as r:
            if r.headers.get_content_type() in ("text/html", "application/xhtml+xml"):
                raise CaptionError("That Google Drive file isn't shared publicly. Set it to 'Anyone with the link' and "
                                   "send it again." if drive else
                                   "That link opens a web page, not a video file. Send a direct link to the video.")
            length = int(r.headers.get("Content-Length") or 0)
            if length > cap:
                raise CaptionError(f"That video is over {cap // 2**20} MB. Trim it first or send a shorter part.")
            total = 0
            with open(dest, "wb") as f:
                while True:
                    chunk = r.read(1 << 20)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > cap:
                        raise CaptionError(f"That video is over {cap // 2**20} MB. Trim it first or send a shorter part.")
                    if time.monotonic() > deadline:
                        raise CaptionError("Downloading that video took too long. Try a faster link or a shorter clip.")
                    f.write(chunk)
            if total == 0:
                raise CaptionError("That link gave back an empty file.")
    except urllib.error.HTTPError as e:
        raise CaptionError(f"The link answered {e.code} ({e.reason}). Check it opens without signing in.") from None
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, CaptionError):
            raise reason from None
        raise CaptionError(f"Couldn't download that link ({str(reason)[:120]}).") from None
    except (TimeoutError, socket.timeout):
        raise CaptionError("The link stopped answering while downloading. Try again or send another link.") from None
    except (http.client.HTTPException, OSError) as e:
        if isinstance(e, CaptionError):
            raise
        raise CaptionError(f"Couldn't download that link ({type(e).__name__}).") from None
    return dest


# --------------------------------------------------------------------------- media
def _run(cmd: list, timeout: float, cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {(p.stderr or p.stdout)[-800:]}")
    return p


def probe(path: str) -> dict:
    try:
        p = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], 120)
        info = json.loads(p.stdout)
    except (RuntimeError, ValueError):
        raise CaptionError("That file isn't a video the hub can read.") from None
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
              and not (s.get("disposition") or {}).get("attached_pic")), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v or not v.get("width") or not v.get("height"):
        raise CaptionError("That file has no video in it.")
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int(float((v.get("tags") or {}).get("rotate", 0) or 0))
    if abs(rot) % 180 == 90:                 # a phone video stored sideways: ffmpeg turns it upright
        w, h = h, w
    dur = float((info.get("format") or {}).get("duration") or v.get("duration") or 0)
    return {"duration": dur, "width": w, "height": h, "has_audio": a is not None,
            "vcodec": v.get("codec_name"), "acodec": a and a.get("codec_name")}


def crop_box(w: int, h: int, pos: float) -> tuple[int, int, int, int]:
    """The 9:16 slice of a w x h frame, `pos` of the way across (0 left, 0.5 centre, 1 right)."""
    if w * 16 > h * 9:                        # wider than 9:16: keep the height, slice across
        cw, ch = max(2, int(h * 9 / 16) // 2 * 2), h // 2 * 2
        x, y = int(round((w - cw) * pos)) // 2 * 2, 0
    else:                                     # taller: keep the width, take the middle
        cw, ch = w // 2 * 2, max(2, int(w * 16 / 9) // 2 * 2)
        x, y = 0, (h - ch) // 2 // 2 * 2
    return cw, ch, min(x, w - cw), max(0, y)


def font() -> tuple[Optional[str], str, bool]:
    for path, name, bold in FONTS:
        if os.path.exists(path):
            return path, name, bold
    return None, "Sans", True


# --------------------------------------------------------------------------- captions (ASS)
def clean_word(w: str) -> str:
    """What a caption shows: upper case, no ASS override characters, no trailing commas or full stops."""
    t = re.sub(r"[{}\\\n\r\t]", "", w or "").strip()
    t = t.strip("\"“”‘’`,.;:…-–— ")
    return t.upper()


def group_words(words: list, max_words: int = MAX_WORDS, gap: float = 0.6, max_chars: int = 20) -> list:
    """Words in, caption groups out: at most `max_words` each, a new group after a pause or a sentence end."""
    groups, cur = [], []
    for w in words:
        text = clean_word(w.get("word", ""))
        if not text:
            continue
        item = {"start": float(w["start"]), "end": max(float(w["end"]), float(w["start"])), "text": text,
                "stop": bool(re.search(r"[.!?…]\s*$", (w.get("word") or "").strip()))}
        if cur and (len(cur) >= max_words or item["start"] - cur[-1]["end"] > gap or cur[-1]["stop"]
                    or len(" ".join(x["text"] for x in cur + [item])) > max_chars):
            groups.append(cur)
            cur = []
        cur.append(item)
    if cur:
        groups.append(cur)
    return groups


def _t(sec: float) -> str:
    cs = max(0, int(round(sec * 100)))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def build_ass(words: list, length: float, font_name: str = "Montserrat ExtraBold", bold: bool = False,
              size: int = 124) -> str:
    """Word-by-word captions in the middle of a 1080x1920 frame. Each group (<= 3 words) is one line or
    two; while a word is spoken it turns yellow. Under every group sits a soft, blurred shadow."""
    b = -1 if bold else 0
    head = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {OUT_W}", f"PlayResY: {OUT_H}", "WrapStyle: 0",
        "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
        "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
        "MarginR, MarginV, Encoding",
        f"Style: Word,{font_name},{size},&H00FFFFFF,&H0000E6FF,&H00000000,&H00000000,{b},0,0,0,100,100,1,0,1,9,0,5,70,70,0,1",
        f"Style: Shadow,{font_name},{size},&H66000000,&H66000000,&H66000000,&H00000000,{b},0,0,0,100,100,1,0,1,14,0,5,70,70,0,1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    groups = group_words(words)
    events = []
    cx, cy = OUT_W // 2, OUT_H // 2
    for gi, g in enumerate(groups):
        nxt = groups[gi + 1][0]["start"] if gi + 1 < len(groups) else length
        g_end = min(max(g[-1]["end"] + 0.4, g[-1]["end"]), nxt, length)
        g_start = min(g[0]["start"], length)
        if g_end - g_start < 0.05:
            continue
        longest = max(len(w["text"]) for w in g)
        fs = f"\\fs{max(60, int(size * 10 / longest))}" if longest > 10 else ""   # one long word must still fit
        events.append(f"Dialogue: 0,{_t(g_start)},{_t(g_end)},Shadow,,0,0,0,,"
                      f"{{\\an5\\pos({cx},{cy + 12})\\blur12{fs}}}" + " ".join(w["text"] for w in g))
        for i, w in enumerate(g):
            s = g_start if i == 0 else max(w["start"], g_start)
            e = g[i + 1]["start"] if i + 1 < len(g) else g_end
            e = min(max(e, s), g_end)
            if e - s < 0.01:
                continue
            text = " ".join(f"{{\\c{HIGHLIGHT}}}{x['text']}{{\\c&HFFFFFF&}}" if j == i else x["text"]
                            for j, x in enumerate(g))
            events.append(f"Dialogue: 1,{_t(s)},{_t(e)},Word,,0,0,0,,{{\\an5\\pos({cx},{cy}){fs}}}{text}")
    return "\n".join(head + events) + "\n"


# --------------------------------------------------------------------------- transcription
class Transcriber:
    """words(pcm_path) -> [{"start", "end", "word"}], times in seconds from the start of the audio.
    The audio is raw mono 16 kHz signed 16-bit PCM."""

    def words(self, pcm_path: str) -> list:      # pragma: no cover - interface
        raise NotImplementedError

    def release(self) -> None:
        pass


class WhisperTranscriber(Transcriber):
    """A local speech model (faster-whisper, CPU, int8), loaded on first use and dropped when idle.
    The image bakes the model into RELAY_WHISPER_DIR/<model>; anything else is downloaded on first use."""

    def __init__(self, model: Optional[str] = None, model_dir: Optional[str] = None, download_root: Optional[str] = None):
        self.name = model or os.environ.get("RELAY_WHISPER_MODEL", "small.en")
        base = model_dir or os.environ.get("RELAY_WHISPER_DIR", "/opt/whisper")
        baked = os.path.join(base, self.name)
        self.source = baked if os.path.exists(os.path.join(baked, "model.bin")) else self.name
        self.download_root = download_root
        self.model = None
        self.lock = threading.Lock()

    def _load(self):
        with self.lock:
            if self.model is None:
                try:
                    from faster_whisper import WhisperModel    # only here: the hub runs without it
                except Exception:
                    raise CaptionError("Transcription isn't installed on this hub.") from None
                self.model = WhisperModel(self.source, device="cpu", compute_type="int8",
                                          download_root=self.download_root)
            return self.model

    def words(self, pcm_path: str) -> list:
        import numpy as np
        audio = np.fromfile(pcm_path, dtype=np.int16).astype(np.float32) / 32768.0
        if not len(audio):
            return []
        model = self._load()
        kw = {"language": "en"} if self.name.endswith(".en") else {}
        segs, _info = model.transcribe(audio, word_timestamps=True, vad_filter=True, beam_size=5,
                                       condition_on_previous_text=False, **kw)
        out = []
        for s in segs:
            for w in s.words or []:
                out.append({"start": round(float(w.start), 3), "end": round(float(w.end), 3), "word": w.word})
        return out

    def release(self) -> None:
        with self.lock:
            self.model = None


# --------------------------------------------------------------------------- the queue + worker
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def hms(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Captioner:
    def __init__(self, store, private_host: Callable[[str], bool], transcriber: Optional[Transcriber] = None,
                 threads: bool = True, allow_private: bool = False, public_url: Optional[str] = None,
                 idle_seconds: float = 600, now: Callable[[], float] = time.time, log=print):
        self.store, self.private_host, self.threads = store, private_host, threads
        self.transcriber = transcriber or WhisperTranscriber(download_root=os.path.join(store.dir, "models"))
        self.allow_private, self.now, self.log, self.idle = allow_private, now, log, idle_seconds
        self.public_url = (public_url or os.environ.get("RELAY_PUBLIC_URL") or
                           ("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
                           ).rstrip("/") or None
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.wake = threading.Event()
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)

    # ------------------------------------------------------------ submit / read
    def submit(self, by: str, url: Optional[str] = None, file_id: Optional[str] = None, start: Any = None,
               end: Any = None, crop: Any = None, origin: Optional[str] = None) -> dict:
        url = (url or "").strip() or None
        file_id = (file_id or "").strip() or None
        if bool(url) == bool(file_id):
            raise CaptionError("Send either a link to a video file (url) or a file already on the hub (file_id).")
        if url:
            if len(url) > 2000:
                raise CaptionError("That link is too long.")
            target = direct_url(url)                   # YouTube and non-web links are refused here
            if not self.allow_private and self.private_host(_host(target)):
                raise CaptionError("That link points at a private or local address.")
        s, e = seconds(start), seconds(end)
        if s is not None and e is not None and e <= s:
            raise CaptionError("end must be after start.")
        label, _ = crop_position(crop)
        cid = "c_" + uuid.uuid4().hex[:12]
        now = self.now()
        with self.store.tx() as c:
            if file_id and not c.execute("SELECT 1 FROM artifacts WHERE id=?", (file_id,)).fetchone():
                raise CaptionError(f"There's no file {file_id[:80]} on the hub. Upload it first (POST /artifacts).")
            if c.execute("SELECT COUNT(*) n FROM captions WHERE status IN ('queued','working')").fetchone()["n"] >= MAX_QUEUED:
                raise CaptionError("The hub has a lot of shorts waiting. Try again in a few minutes.")
            c.execute("INSERT INTO captions(id,by,status,stage,progress,url,file_id,start_s,end_s,crop,origin,created,updated)"
                      " VALUES (?,?,'queued','queued',0,?,?,?,?,?,?,?,?)",
                      (cid, by, url, file_id, s, e, label, origin, now, now))
        self._ensure_thread()
        return self.get(cid)

    def row(self, cid: str):
        with self.store.ro() as c:
            return c.execute("SELECT * FROM captions WHERE id=?", (cid,)).fetchone()

    def get(self, cid: str) -> dict:
        r = self.row(cid) if ID_RE.match(cid or "") else None
        if not r:
            raise CaptionError(f"No short {str(cid)[:40]} on this hub.")
        return self.out(r)

    def list(self, by: Optional[str] = None, limit: int = 20) -> list:
        q, a = "SELECT * FROM captions", []
        if by:
            q += " WHERE by=?"; a.append(by)
        q += " ORDER BY created DESC LIMIT ?"; a.append(max(1, min(int(limit), 100)))
        with self.store.ro() as c:
            return [self.out(r) for r in c.execute(q, a)]

    def base(self, r) -> Optional[str]:
        return self.public_url or r["origin"]

    def out(self, r) -> dict:
        d = {"id": r["id"], "status": r["status"], "stage": r["stage"],
             "doing": STAGES.get(r["stage"] or "", None) if r["status"] in ("queued", "working") else None,
             "progress": round(r["progress"] or 0, 3), "by": r["by"], "source": shown_url(r["url"]) or r["file_id"],
             "start": r["start_s"], "end": r["end_s"], "crop": r["crop"], "error": r["error"],
             "created": r["created"], "updated": r["updated"]}
        res = json.loads(r["result"]) if r["result"] else None
        if r["status"] == "done" and res:
            expired = (r["share_expires"] or 0) <= self.now()
            path = f"/s/{r['share']}.mp4"
            base = self.base(r)
            d.update(res)
            d.update({"artifact": r["artifact"], "share_expires": r["share_expires"], "share_expired": expired,
                      "share_path": None if expired else path,
                      "share_url": None if expired or not base else base + path})
        return d

    # ------------------------------------------------------------ worker
    def _queued(self) -> bool:
        with self.store.ro() as c:
            return bool(c.execute("SELECT 1 FROM captions WHERE status='queued' LIMIT 1").fetchone())

    def _ensure_thread(self) -> None:
        if not self.threads:
            return
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                self.wake.set()
                return
            self.thread = threading.Thread(target=self._loop, name="captions", daemon=True)
            self.thread.start()

    def resume(self) -> None:
        """After a restart: a job that was mid-way goes back in the queue once; twice, and it fails plainly."""
        now = self.now()
        with self.store.tx() as c:
            for r in c.execute("SELECT id, attempts FROM captions WHERE status='working'").fetchall():
                if r["attempts"] >= 2:
                    c.execute("UPDATE captions SET status='failed', stage=NULL, error=?, updated=? WHERE id=?",
                              ("The hub restarted twice while making this short. Send it again.", now, r["id"]))
                else:
                    c.execute("UPDATE captions SET status='queued', stage='queued', progress=0, error=?, updated=?"
                              " WHERE id=?", ("The hub restarted; this short was started again.", now, r["id"]))
        shutil.rmtree(os.path.join(self.store.tmp, "captions"), ignore_errors=True)   # leftovers of the old run
        if self._queued():
            self._ensure_thread()

    def _loop(self) -> None:
        idle_since = None
        while True:
            try:
                did = self.run_once()
            except Exception as e:                  # never let one job take the worker down
                self.log(f"[relay] captions: {type(e).__name__}: {str(e)[:200]}")
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
                    self.transcriber.release()      # give the memory back until the next short
                    return
            if not waiting:
                self.wake.wait(min(5.0, max(self.idle, 0.05)))
                self.wake.clear()

    def run_once(self) -> bool:
        """Make the oldest waiting short. False when nothing is waiting."""
        with self.store.tx() as c:
            r = c.execute("SELECT * FROM captions WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
            if not r:
                return False
            c.execute("UPDATE captions SET status='working', stage='checking', progress=0, attempts=attempts+1,"
                      " updated=? WHERE id=?", (self.now(), r["id"]))
        self._process(self.row(r["id"]))
        return True

    def _stage(self, cid: str, stage: str, progress: float = 0.0) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE captions SET stage=?, progress=?, updated=? WHERE id=?",
                      (stage, round(progress, 3), self.now(), cid))

    def _fail(self, cid: str, msg: str) -> None:
        with self.store.tx() as c:
            c.execute("UPDATE captions SET status='failed', stage=NULL, error=?, updated=? WHERE id=?",
                      (msg[:600], self.now(), cid))

    def _process(self, r) -> None:
        cid = r["id"]
        work = os.path.join(self.store.tmp, "captions", cid)
        shutil.rmtree(work, ignore_errors=True)
        os.makedirs(work)
        try:
            self._make(r, work)
        except CaptionError as e:
            self._fail(cid, str(e))
        except subprocess.TimeoutExpired:
            self._fail(cid, "Making this short took too long. Try a shorter part of the video.")
        except Exception as e:
            self.log(f"[relay] captions {cid}: {type(e).__name__}: {str(e)[:300]}")
            self._fail(cid, "Something went wrong making this short. Try again, or send a different file.")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def _make(self, r, work: str) -> None:
        cid = r["id"]
        if r["url"]:
            self._stage(cid, "downloading")
            src = download(r["url"], os.path.join(work, "source"), self.private_host, self.allow_private)
            name = os.path.basename(urllib.parse.urlparse(r["url"]).path) or "clip"
        else:
            with self.store.ro() as c:
                a = c.execute("SELECT name FROM artifacts WHERE id=?", (r["file_id"],)).fetchone()
            src = self.store.blob_path(r["file_id"])
            if not a or not os.path.exists(src):
                raise CaptionError("That file is no longer on the hub. Upload it again.")
            name = a["name"] or "clip"
        src = os.path.abspath(src)                 # ffmpeg runs inside the work dir
        self._stage(cid, "checking")
        spec = probe(src)
        dur = spec["duration"]
        start = r["start_s"] or 0.0
        if dur and start >= dur:
            raise CaptionError(f"start ({hms(start)}) is past the end of the video ({hms(dur)}).")
        end = r["end_s"] if r["end_s"] is not None else dur or start + MAX_SECONDS
        end = min(end, dur) if dur else end
        capped = end - start > MAX_SECONDS
        end = min(end, start + MAX_SECONDS)
        length = round(end - start, 3)
        if length < 0.5:
            raise CaptionError("That part of the video is too short to make a short.")

        self._stage(cid, "transcribing")
        words: list = []
        if spec["has_audio"]:
            pcm = os.path.join(work, "audio.pcm")
            _run(["ffmpeg", "-nostdin", "-y", "-v", "error", "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src,
                  "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", pcm], 900)
            words = [w for w in self.transcriber.words(pcm) if 0 <= float(w["start"]) < length]
        path, font_name, bold = font()
        fonts = os.path.join(work, "fonts")
        os.makedirs(fonts)
        if path:
            shutil.copyfile(path, os.path.join(fonts, os.path.basename(path)))
        with open(os.path.join(work, "captions.ass"), "w", encoding="utf-8") as f:
            f.write(build_ass(words, length, font_name, bold))

        self._stage(cid, "cutting")
        _, pos = crop_position(r["crop"])
        cw, ch, x, y = crop_box(spec["width"], spec["height"], pos)
        vf = (f"crop={cw}:{ch}:{x}:{y},scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1,"
              "ass=captions.ass:fontsdir=fonts")              # relative names: nothing to escape
        cmd = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-v", "error", "-progress", "pipe:1", "-nostats",
               "-ss", f"{start:.3f}", "-t", f"{length:.3f}", "-i", src]
        if spec["has_audio"]:
            amap = ["-map", "0:a:0"]
        else:                                     # always deliver an audio track; posting apps expect one
            cmd += ["-f", "lavfi", "-t", f"{length:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
            amap = ["-map", "1:a:0"]
        out = os.path.join(work, "short.mp4")
        cmd += ["-map", "0:v:0", *amap, "-vf", vf, "-fpsmax", "60",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2", "-shortest",
                "-movflags", "+faststart", "short.mp4"]
        self._render(cmd, work, length, lambda p: self._stage(cid, "cutting", p))

        self._stage(cid, "saving", 1.0)
        got = probe(out)
        if (got["width"], got["height"]) != (OUT_W, OUT_H):
            raise RuntimeError(f"unexpected output size {got['width']}x{got['height']}")
        stem = re.sub(r"[^\w.-]+", "-", os.path.splitext(name)[0])[:60].strip("-.") or "clip"
        transcript = " ".join((w.get("word") or "").strip() for w in words).strip()
        meta = {"caption": cid, "transcript": transcript, "duration": round(got["duration"], 2),
                "start": start, "end": round(end, 3), "crop": r["crop"]}
        aid, size = self._store(out, f"{stem}-short.mp4", r["by"], r["file_id"], meta)
        token = secrets.token_urlsafe(32)
        now = self.now()
        result = {"transcript": transcript, "words": len(words), "duration": round(got["duration"], 2),
                  "width": OUT_W, "height": OUT_H, "size": size, "name": f"{stem}-short.mp4",
                  "trimmed_to_max": capped, "captions": bool(words)}
        with self.store.tx() as c:
            c.execute("INSERT INTO shares(token_hash,caption,artifact,created,expires) VALUES (?,?,?,?,?)",
                      (_hash(token), cid, aid, now, now + SHARE_SECONDS))
            c.execute("UPDATE captions SET status='done', stage=NULL, progress=1, error=NULL, result=?, artifact=?,"
                      " share=?, share_expires=?, updated=? WHERE id=?",
                      (json.dumps(result), aid, token, now + SHARE_SECONDS, now, cid))

    def _render(self, cmd: list, work: str, length: float, progress: Callable[[float], None]) -> None:
        """ffmpeg with a wall-clock budget, reporting how far it got."""
        err_path = os.path.join(work, "ffmpeg.log")
        with open(err_path, "w") as err:
            p = subprocess.Popen(cmd, cwd=work, stdout=subprocess.PIPE, stderr=err, text=True)
            last = [0.0]

            def read():
                for line in p.stdout:
                    if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                        try:
                            frac = min(1.0, int(line.split("=", 1)[1]) / 1e6 / max(length, 0.1))
                        except ValueError:
                            continue
                        if frac - last[0] >= 0.05:
                            last[0] = frac
                            try:
                                progress(frac)
                            except Exception:
                                pass
            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            try:
                code = p.wait(timeout=RENDER_SECONDS)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=30)
                raise
            finally:
                reader.join(timeout=5)
        if code != 0:
            with open(err_path) as f:
                raise RuntimeError("ffmpeg failed: " + f.read()[-800:])

    def _store(self, path: str, name: str, by: str, parent: Optional[str], meta: dict) -> tuple[str, int]:
        h, size = hashlib.sha256(), 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
                size += len(chunk)
        aid = h.hexdigest()
        dest = self.store.blob_path(aid)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if not os.path.exists(dest):
            shutil.move(path, dest)
        with self.store.tx() as c:
            c.execute("INSERT OR IGNORE INTO artifacts(id,name,mime,size,created_by,created,meta,parent,kind)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (aid, name, "video/mp4", size, by, self.now(), json.dumps(meta), parent, "short"))
        return aid, size

    # ------------------------------------------------------------ share links
    def shared_file(self, token: str) -> tuple[str, str]:
        """token -> (blob path, file name). Only caption outputs have tokens; ids are never accepted here."""
        if not TOKEN_RE.match(token or ""):
            raise LookupError("not found")
        with self.store.ro() as c:
            s = c.execute("SELECT s.expires, s.artifact, a.name FROM shares s JOIN artifacts a ON a.id=s.artifact"
                          " WHERE s.token_hash=?", (_hash(token),)).fetchone()
        if not s:
            raise LookupError("not found")
        if s["expires"] <= self.now():
            raise TimeoutError("expired")
        path = self.store.blob_path(s["artifact"])
        if not os.path.exists(path):
            raise LookupError("not found")
        return path, s["name"] or "short.mp4"


# --------------------------------------------------------------------------- routes + connector tools
class CaptionIn(BaseModel):
    url: Optional[str] = None
    file_id: Optional[str] = None
    start: Optional[Union[float, str]] = None
    end: Optional[Union[float, str]] = None
    crop: Optional[Union[float, str]] = None


TOOL_WAIT = 45


def mount(app, hub, cap: Captioner, tool_wait: float = TOOL_WAIT) -> dict:
    auth = hub.auth
    origin_re = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")

    def member(me) -> str:
        if me["role"] == "admin":
            raise HTTPException(403, "use your own key")
        return me["handle"]

    def origin_of(request: Request) -> Optional[str]:
        o = f"{request.url.scheme}://{request.url.netloc}"
        return o if origin_re.match(o) else None

    def guard(fn, *a, **kw):
        try:
            return fn(*a, **kw)
        except CaptionError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/captions")
    def start(body: CaptionIn, request: Request, me: dict = Depends(auth)):
        return guard(cap.submit, member(me), body.url, body.file_id, body.start, body.end, body.crop,
                     origin=origin_of(request))

    @app.get("/captions")
    def recent(limit: int = 20, me: dict = Depends(auth)):
        everyone = me["role"] == "admin" or me.get("owner")
        return cap.list(None if everyone else me["handle"], limit)

    @app.get("/captions/{cid}")
    def one(cid: str, me: dict = Depends(auth)):
        try:
            return cap.get(cid)
        except CaptionError as e:
            raise HTTPException(404, str(e)) from None

    share_headers = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer",
                     "Content-Security-Policy": "default-src 'none'; media-src 'self'; sandbox",
                     "X-Robots-Tag": "noindex, nofollow", "Cache-Control": "private, max-age=3600"}

    @app.get("/s/{name}", include_in_schema=False)
    def shared(name: str):
        """A short, by its share link: no key, byte ranges (phones won't play video without them), 7 days."""
        if not name.endswith(".mp4"):
            raise HTTPException(404, "not found")
        try:
            path, fname = cap.shared_file(name[:-4])
        except LookupError:
            raise HTTPException(404, "not found") from None
        except TimeoutError:
            raise HTTPException(410, "This link has expired. Ask for a new one.") from None
        return FileResponse(path, media_type="video/mp4", filename=fname, content_disposition_type="inline",
                            headers=share_headers)

    # ------------------------------------------------------------ connector tools
    def describe(s: dict) -> str:
        if s["status"] == "done":
            lines = [f"Short {s['id']} is ready: {s['duration']:.1f} s, {s['width']}x{s['height']} with captions"
                     + ("" if s.get("captions") else " (no speech found, so no captions)") + "."]
            if s.get("share_url"):
                lines.append(f"Share link (works without a key until {time.strftime('%b %d', time.gmtime(s['share_expires']))}): {s['share_url']}")
            elif s.get("share_expired"):
                lines.append("Its share link has expired.")
            if s.get("trimmed_to_max"):
                lines.append(f"It was cut to the first {int(MAX_SECONDS)} s of what you asked for.")
            lines.append(f"Transcript: {s['transcript'] or '(none)'}")
            lines.append("Nothing has been posted. Show it to a person and get their OK before posting it anywhere.")
            return "\n".join(lines)
        if s["status"] == "failed":
            return f"Short {s['id']} failed: {s['error']}"
        return (f"Short {s['id']} is still being made ({(s.get('doing') or 'working').lower()}). "
                f"Call clip_status with id {s['id']} in a minute.")

    def tool_caption(me, args):
        try:
            s = cap.submit(me, args.get("url"), args.get("file_id"), args.get("start"), args.get("end"), args.get("crop"))
        except CaptionError as e:
            raise HTTPException(400, str(e)) from None
        deadline = time.monotonic() + tool_wait
        while s["status"] in ("queued", "working") and time.monotonic() < deadline:
            time.sleep(1.0)
            s = cap.get(s["id"])
        return describe(s), s

    def tool_status(me, args):
        try:
            s = cap.get(str(args.get("id", "")))
        except CaptionError as e:
            raise HTTPException(404, str(e)) from None
        return describe(s), s

    time_schema = {"type": "string", "description": "Seconds, or mm:ss / h:mm:ss, in the source video (e.g. \"75\" or \"1:15\")"}
    tools = [
        {"name": "caption_clip",
         "title": "Make a captioned short",
         "description": "Turn a gameplay clip into a vertical short ready to post: 9:16 at 1080x1920, the streamer's "
                        "facecam cropped out (a slice from the middle of the frame by default), and big word-by-word "
                        "captions burned into the centre of the screen, timed to the speech. The hub does it in the "
                        "cloud. Send `url`: a link to a video FILE (a direct .mp4 link, or a Google Drive or Dropbox "
                        "share link set to anyone-with-the-link), or `file_id`: a video already uploaded to the hub. "
                        "YouTube links do not work: YouTube blocks cloud servers from downloading video. Optional "
                        "`start`/`end` trim the source; the short is at most 180 s. Waits about 45 s; if it is not "
                        "done by then you get an id for clip_status. The result has the transcript, the duration and "
                        "a share link to the MP4 that works without a key for 7 days. Nothing is posted anywhere: a "
                        "person must see the short and approve it before it is posted.",
         "inputSchema": {"type": "object", "properties": {
             "url": {"type": "string", "description": "Link to the video file (not YouTube)"},
             "file_id": {"type": "string", "description": "Id of a video already uploaded to the hub"},
             "start": time_schema, "end": time_schema,
             "crop": {"type": "string", "default": "center",
                      "description": "Where the 9:16 slice sits: center (drops corner facecams), left, right, "
                                     "or a number from \"0\" (far left) to \"1\" (far right)"}},
             "additionalProperties": False}},
        {"name": "clip_status",
         "title": "Short status",
         "description": "Check a short started with caption_clip: still working (and at which step), failed (and why), "
                        "or done with the transcript, duration and share link. A person must approve before it is posted.",
         "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"],
                         "additionalProperties": False},
         "annotations": {"readOnlyHint": True}},
    ]
    return {"tools": tools, "handlers": {"caption_clip": tool_caption, "clip_status": tool_status}}
