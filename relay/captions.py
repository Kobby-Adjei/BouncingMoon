"""Fetching and listening: the safe download and the speech model that media uses.

  download     fetches a link with limits (size, time) and a guard on every hop: never this machine or
               a private network, and a page that asks to sign in is refused, not saved as video
  direct_url   turns Drive and Dropbox share links into direct downloads; YouTube is refused plainly
  Transcriber  words with timings from 16 kHz mono PCM; WhisperTranscriber is the local model baked
               into the image (faster-whisper on the CPU), loaded on first use and dropped when idle

(This file once also made captioned vertical shorts. That feature was cut; media.py is its only user.)
"""
from __future__ import annotations
import http.client
import ipaddress
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

MAX_DOWNLOAD = 1024 ** 3             # 1 GB


DOWNLOAD_SECONDS = 600               # whole download, not per read: a slow drip can't hold the queue


SAMPLE_RATE = 16000


YOUTUBE_HOSTS = ("youtube.com", "youtu.be", "youtube-nocookie.com", "youtube.googleapis.com")


YOUTUBE_MSG = ("YouTube blocks cloud servers from downloading video. "
               "Download the moment and send the file or a link to it instead.")


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


def hms(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"
