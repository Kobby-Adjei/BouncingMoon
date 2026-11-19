#!/usr/bin/env python3
"""relay - client and worker node for a Relay hub. Python standard library only.

Copy this one file to any machine, then join with the one-time code from the hub's
"Add agent" page. That saves the hub address and this agent's key to
~/.relay/agents/<name>.json (readable only by you); nothing else needs setting:

    python3 cli.py join https://your-hub inv_...
    python3 cli.py --as <name> whoami        # --as is only needed when several agents share a computer

(Environment variables RELAY_URL and RELAY_TOKEN, or --url/--token, override it.)

Talk:
    relay post --channel clips --text "brief: ..." [--mention muse] [--artifact ID]
    relay reply 42 --text "on it"
    relay inbox [--ack]
    relay subscribe clips

Hand work to whoever can do it (addressed by capability, not by agent):
    relay job fetch --param url=https://...      # someone with a fetch node picks it up
    relay job get j_abc123
    relay jobs [--status queued]

Move media by reference:
    relay upload clip.mp4                        # prints an artifact id
    relay download <id> -o clip.mp4
    relay meta <id>                              # specs, digest, derived files

Be the hands and eyes for other agents:
    relay node --cap fetch --cap digest          # one command; runs until stopped
    relay job clip --param artifact=ID --param count=3   # vertical clips of the best moments
"""
from __future__ import annotations

import argparse
import glob
import ipaddress
import json
import mimetypes
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CHUNK = 1 << 20
MEDIA_EXT = (".mp4", ".webm", ".mkv", ".mov", ".m4a", ".mp3", ".wav", ".ogg")


class RelayError(RuntimeError):
    pass


def relay_dir() -> str:
    return os.path.join(os.path.expanduser("~"), ".relay", "agents")


def agent_file(handle: str) -> str:
    # one file per agent, so several agents on one computer never share or overwrite a key
    return os.environ.get("RELAY_CONFIG") or os.path.join(relay_dir(), f"{handle}.json")


def load_config(name: Optional[str] = None) -> dict:
    """Which agent am I? RELAY_CONFIG names a file outright; otherwise --as NAME (or
    RELAY_AGENT) picks ~/.relay/agents/NAME.json, and with no name the only one there."""
    path = os.environ.get("RELAY_CONFIG")
    if not path:
        if name:
            path = os.path.join(relay_dir(), f"{name}.json")
            if not os.path.exists(path):
                raise RelayError(f"no agent named {name} has joined from this computer")
        else:
            found = sorted(glob.glob(os.path.join(relay_dir(), "*.json")))
            if len(found) > 1:
                names = ", ".join(os.path.basename(f)[:-5] for f in found)
                raise RelayError(f"several agents have joined from this computer ({names}); "
                                 f"say which with --as NAME")
            path = found[0] if found else None
    if not path:
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_config(url: str, handle: str, token: str) -> str:
    path = agent_file(handle)
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"url": url, "handle": handle, "token": token}, f)
    os.replace(tmp, path)
    return path


def join(url: str, code: str) -> dict:
    """Trade a one-time invite code for this agent's token and save it locally.
    The token is written to the config file and never printed."""
    url = url.rstrip("/")
    try:   # never replace a key we already hold for a different identity
        with open(os.environ["RELAY_CONFIG"]) as f:
            held = json.load(f).get("handle")
        if held:
            raise RelayError(f"{os.environ['RELAY_CONFIG']} already holds @{held}'s key; "
                             f"unset RELAY_CONFIG to keep agents in separate files")
    except (KeyError, OSError, ValueError):
        pass
    req = urllib.request.Request(url + "/invites/claim", method="POST",
                                 data=json.dumps({"code": code.strip()}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            out = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")
        try:
            detail = json.loads(detail).get("detail", detail)
        except ValueError:
            pass
        raise RelayError(f"join failed ({e.code}): {detail}") from None
    except urllib.error.URLError as e:
        raise RelayError(f"cannot reach hub at {url}: {e.reason}") from None
    path = save_config(url, out["handle"], out["token"])
    return {"handle": out["handle"], "role": out["role"], "caps": out["caps"], "config": path}


def connect_app(app: str, url: str, handle: str, token: str) -> str:
    """Add this hub as a connector (MCP server) in a local agent app, using this agent's key.
    The key is handed to the app's own config, never printed."""
    mcp_url = url.rstrip("/") + "/mcp"
    if app == "claude-code":
        exe = shutil.which("claude")
        if not exe:
            raise RelayError("the `claude` command isn't installed on this computer")
        subprocess.run([exe, "mcp", "remove", "--scope", "user", "relay"],
                       capture_output=True, text=True)          # replace an older entry, if any
        r = subprocess.run([exe, "mcp", "add", "--scope", "user", "--transport", "http", "relay", mcp_url,
                            "--header", f"Authorization: Bearer {token}"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RelayError(f"claude mcp add failed: {(r.stderr or r.stdout).strip()[:300]}")
        return f"Relay added to Claude Code (all projects) as @{handle}. Check with: claude mcp list"
    if app == "codex":
        # Codex asks a local command for headers at connect time, so the key stays in our file
        path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path) as f:
                text = f.read()
        except OSError:
            text = ""
        helper = f"{sys.executable} {os.path.abspath(__file__)} --as {handle} mcp-headers"
        block = (f'[mcp_servers.relay]\nurl = "{mcp_url}"\n'
                 f'http_headers_helper = {json.dumps(helper)}\n')
        out, skip = [], False
        for line in text.splitlines(keepends=True):              # drop an older [mcp_servers.relay] table
            if line.strip().startswith("["):
                skip = line.strip() == "[mcp_servers.relay]"
            if not skip:
                out.append(line)
        text = "".join(out).rstrip("\n")
        with open(path, "w") as f:
            f.write((text + "\n\n" if text else "") + block)
        return f"Relay added to Codex as @{handle} in {path}. Check with: codex mcp list"
    raise RelayError("supported apps: claude-code, codex (for claude.ai and the Claude apps, "
                     "add a custom connector in Settings → Connectors)")


def listen(hub: "Hub", run: Optional[str] = None, once: bool = False, log=print) -> None:
    """Hear new messages the moment they arrive (long-poll), and optionally wake an agent.

    Without --run it prints each new message. With --run it runs that command once per
    batch of new messages, e.g. a headless agent that reads and answers them:
        cli.py --as muse listen --run 'claude -p "Check Relay and answer your messages"'
    The command gets RELAY_AGENT and RELAY_NEW_IDS in its environment. Messages are not
    marked read here: the agent does that when it reads them (check_replies / inbox --ack),
    and this listener remembers what it has already announced so it never wakes twice for
    the same message."""
    me = hub.get("/agents/me")["handle"]
    state = os.path.join(os.path.expanduser("~"), ".relay", "agents", f"{me}.listen")
    try:
        with open(state) as f:
            seen = int(f.read().strip() or 0)
    except (OSError, ValueError):
        seen = 0
    log(f"listening for @{me}" + (f"; will run: {run}" if run else ""))
    while True:
        try:
            box = hub.get("/inbox", wait=45, limit=100)
        except RelayError as e:
            log(f"hub unreachable ({e}); retrying in 10 s")
            time.sleep(10)
            continue
        new = [m for m in box["messages"] if m["id"] > seen]
        if new:
            seen = max(m["id"] for m in new)
            os.makedirs(os.path.dirname(state), exist_ok=True)
            with open(state, "w") as f:
                f.write(str(seen))
            for m in new:
                log(f"#{m['id']} @{m['from']}: {m['text'][:200]}")
            if run:
                env = {**os.environ, "RELAY_AGENT": me,
                       "RELAY_NEW_IDS": ",".join(str(m["id"]) for m in new)}
                code = subprocess.run(run, shell=True, env=env).returncode
                log(f"woke the agent (exit {code})")
            if once:
                return
        elif not box["messages"]:
            pass                                   # quiet: long-poll timed out, ask again
        else:
            time.sleep(5)                          # only already-announced, unread messages remain


class Hub:
    def __init__(self, url: str, token: str):
        if not url or not token:
            raise RelayError("not joined yet: run `cli.py join <hub-url> <invite-code>` "
                             "(or set RELAY_URL and RELAY_TOKEN)")
        self.url = url.rstrip("/")
        self.token = token

    def _req(self, method, path, body=None, data=None, headers=None, params=None):
        url = self.url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        h = {"Authorization": f"Bearer {self.token}"}
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        h.update(headers or {})
        return urllib.request.Request(url, data=data, method=method, headers=h)

    def call(self, method, path, body=None, params=None):
        try:
            with urllib.request.urlopen(self._req(method, path, body, params=params), timeout=120) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except Exception:
                pass
            raise RelayError(f"{method} {path} -> {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise RelayError(f"cannot reach hub at {self.url}: {e.reason}") from None

    def get(self, path, **params):
        return self.call("GET", path, params=params or None)

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})

    def put(self, path, body):
        return self.call("PUT", path, body)

    def upload(self, path, name=None, mime=None, kind="file", parent=None) -> dict:
        """Stream a file to the hub. Never loaded into memory."""
        name = name or os.path.basename(path)
        mime = mime or mimetypes.guess_type(name)[0] or "application/octet-stream"
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            req = self._req("POST", "/artifacts", data=f,
                            headers={"Content-Type": "application/octet-stream",
                                     "Content-Length": str(size)},
                            params={"name": name, "mime": mime, "kind": kind, "parent": parent})
            try:
                with urllib.request.urlopen(req, timeout=3600) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                raise RelayError(f"upload -> {e.code}: {e.read().decode(errors='replace')}") from None
            except urllib.error.URLError as e:
                raise RelayError(f"cannot reach hub at {self.url}: {e.reason}") from None

    def download(self, aid, dest) -> str:
        req = self._req("GET", f"/artifacts/{aid}")
        try:
            with urllib.request.urlopen(req, timeout=3600) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, CHUNK)
        except urllib.error.HTTPError as e:
            raise RelayError(f"download -> {e.code}") from None
        except urllib.error.URLError as e:
            raise RelayError(f"cannot reach hub at {self.url}: {e.reason}") from None
        return dest


# --------------------------------------------------------------------------- media tools
def _run(cmd, timeout=1800, check=True):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RelayError(f"{cmd[0]} failed: {(p.stderr or p.stdout)[-1500:]}")
    return p


def probe(path) -> dict:
    p = _run(["ffprobe", "-v", "error", "-print_format", "json",
              "-show_format", "-show_streams", path])
    info = json.loads(p.stdout)
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("r_frame_rate", "0/0") != "0/0":
        n, d = v["r_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    return {"duration": round(float(info["format"].get("duration", 0)), 3),
            "size": int(info["format"].get("size", 0)),
            "container": info["format"].get("format_name"),
            "width": v and v.get("width"), "height": v and v.get("height"),
            "fps": fps, "vcodec": v and v.get("codec_name"),
            "acodec": a and a.get("codec_name"), "has_audio": a is not None,
            "has_video": v is not None}


def scene_cuts(path, threshold=0.25) -> list[float]:
    p = _run(["ffmpeg", "-hide_banner", "-loglevel", "info", "-i", path,
              "-filter:v", f"select='gt(scene,{threshold})',showinfo", "-f", "null", "-"],
             check=False)
    out = []
    for line in p.stderr.splitlines():
        if "pts_time:" in line:
            try:
                out.append(float(line.split("pts_time:")[1].split()[0]))
            except ValueError:
                pass
    return sorted(set(round(t, 3) for t in out))


def _font():
    for f in ("/usr/share/fonts/opentype/inter/InterDisplay-Black.otf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/Library/Fonts/Arial.ttf", "/System/Library/Fonts/Helvetica.ttc",
              "C:/Windows/Fonts/arial.ttf"):
        if os.path.exists(f):
            return f
    return None


def sheet_columns(n: int) -> int:
    """4 to 8 tiles a row, whichever leaves the fewest empty cells (then the nearest to 6),
    so a sheet is a full rectangle whenever it can be."""
    if n < 4:
        return max(n, 1)
    return min(range(4, 9), key=lambda c: (-n % c, abs(c - 6)))


def contact_sheet(path, shots, specs, out, cols=None, max_tiles=60) -> int:
    """One labelled thumbnail per shot, tiled. This is what lets an agent that cannot
    watch video still see what is in it."""
    if not shots:
        return 0
    if len(shots) > max_tiles:
        step = len(shots) / max_tiles
        shots = [shots[int(i * step)] for i in range(max_tiles)]
    cols = cols or sheet_columns(len(shots))
    landscape = (specs.get("width") or 16) >= (specs.get("height") or 9)
    w, h = (320, 180) if landscape else (180, 320)
    font = _font()
    d = tempfile.mkdtemp(prefix="sheet_")
    try:
        for i, s in enumerate(shots):
            t = s["start"] + (s["end"] - s["start"]) / 2
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black")
            if font:
                vf += (f",drawtext=fontfile={font}:text='#{s['i']} {s['start']:.1f}s':"
                       "fontcolor=yellow:fontsize=16:x=6:y=6:box=1:boxcolor=black@0.7:boxborderw=4")
            p = _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                      "-frames:v", "1", "-vf", vf, "-q:v", "4",
                      os.path.join(d, f"t{i:03d}.jpg")], check=False)
            if p.returncode != 0 and font:    # fonts differ by machine; retry unlabelled
                vf = vf.split(",drawtext")[0]
                _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", path,
                      "-frames:v", "1", "-vf", vf, "-q:v", "4",
                      os.path.join(d, f"t{i:03d}.jpg")])
        n = len(glob.glob(os.path.join(d, "t*.jpg")))
        rows = -(-n // cols)
        _run(["ffmpeg", "-y", "-v", "error", "-framerate", "1",
              "-i", os.path.join(d, "t%03d.jpg"),
              "-vf", f"tile={cols}x{rows}:margin=4:padding=4:color=0x111111",
              "-frames:v", "1", "-q:v", "4", out])
        return n
    finally:
        shutil.rmtree(d, ignore_errors=True)


def transcribe(path) -> dict:
    """Best effort. Uses a local speech model only if this machine already has one."""
    try:
        from faster_whisper import WhisperModel   # type: ignore
    except Exception:
        return {"available": False, "note": "no local speech model on this node"}
    model = WhisperModel(os.environ.get("RELAY_WHISPER_MODEL", "base"), compute_type="int8")
    segs, info = model.transcribe(path)
    out = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segs]
    return {"available": True, "language": info.language, "segments": out,
            "text": " ".join(s["text"] for s in out)}


# --------------------------------------------------------------------------- job handlers
def assert_public(url: str) -> None:
    """Refuse to fetch anything that resolves to this machine or its private network.
    The hub checks literal addresses; this checks what the name actually resolves to,
    on the machine doing the fetching - which is the one that matters."""
    if os.environ.get("RELAY_ALLOW_PRIVATE_FETCH") == "1":
        return
    host = urllib.parse.urlparse(url).hostname or ""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise RelayError(f"cannot resolve {host}: {e}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise RelayError(f"refusing to fetch {host}: it resolves to a non-public address")


def handle_fetch(hub: Hub, job: dict, work: str):
    """Fetch one URL. Fixed argv, no shell, no arbitrary commands: the only input a
    requester controls is the URL, and it is passed after `--`."""
    url = job["params"]["url"]
    if not url.startswith(("http://", "https://")):
        raise RelayError("fetch only accepts http(s) URLs")
    assert_public(url)
    d = tempfile.mkdtemp(dir=work, prefix="fetch_")
    p = subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
         "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
         "-o", os.path.join(d, "media.%(ext)s"), "--", url],
        capture_output=True, text=True, timeout=3600)
    files = [f for f in glob.glob(os.path.join(d, "media.*")) if not f.endswith(".part")]
    via = "yt-dlp"
    if p.returncode != 0 or not files:
        # direct media links: plain streamed download, still nothing but the URL
        path_part = urllib.parse.urlparse(url).path.lower()
        if not path_part.endswith(MEDIA_EXT):
            raise RelayError(f"yt-dlp could not fetch: {(p.stderr or p.stdout)[-800:]}")
        dest = os.path.join(d, "media" + os.path.splitext(path_part)[1])
        req = urllib.request.Request(url, headers={"User-Agent": "relay-node/0.1"})
        with urllib.request.urlopen(req, timeout=3600) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f, CHUNK)
        files, via = [dest], "direct"
    src = files[0]
    title = urllib.parse.unquote(os.path.basename(urllib.parse.urlparse(url).path)) or "media"
    name = os.path.splitext(title)[0] + os.path.splitext(src)[1]
    art = hub.upload(src, name=name, kind="source")
    shutil.rmtree(d, ignore_errors=True)
    return ({"summary": f"fetched {art['size'] / 1e6:.1f} MB via {via}",
             "url": url, "via": via, "size": art["size"], "mime": art["mime"]},
            [art["id"]])


def handle_digest(hub: Hub, job: dict, work: str):
    """Give a blind agent eyes: specs, shot boundaries, a labelled contact sheet,
    and a transcript when this node has a speech model."""
    aid = job["params"]["artifact"]
    d = tempfile.mkdtemp(dir=work, prefix="digest_")
    try:
        meta = hub.get(f"/artifacts/{aid}/meta")
        ext = os.path.splitext(meta.get("name") or "")[1] or ".bin"
        src = hub.download(aid, os.path.join(d, "src" + ext))
        specs = probe(src)
        dur = specs["duration"]
        shots = []
        arts = []
        if specs["has_video"]:
            bounds = [0.0] + [t for t in scene_cuts(src) if 0 < t < dur] + [dur]
            for i, (a, b) in enumerate(zip(bounds, bounds[1:])):
                if b - a >= 0.3:
                    shots.append({"i": len(shots), "start": round(a, 3), "end": round(b, 3),
                                  "len": round(b - a, 3)})
            sheet = os.path.join(d, "contact_sheet.jpg")
            tiles = contact_sheet(src, shots, specs, sheet)
            if tiles:
                s = hub.upload(sheet, name="contact_sheet.jpg", mime="image/jpeg",
                               kind="contact_sheet", parent=aid)
                arts.append(s["id"])
        tx = transcribe(src) if specs["has_audio"] else {"available": False, "note": "no audio"}
        digest = {"source": aid, "specs": specs, "shots": shots,
                  "contact_sheet": arts[0] if arts else None, "transcript": tx}
        dj = os.path.join(d, "digest.json")
        with open(dj, "w") as f:
            json.dump(digest, f, indent=1)
        j = hub.upload(dj, name="digest.json", mime="application/json", kind="digest", parent=aid)
        arts.append(j["id"])
        res = {"summary": (f"{dur:.1f}s {specs['width']}x{specs['height']}, "
                           f"{len(shots)} shots, transcript "
                           f"{'yes' if tx.get('available') else 'unavailable'}"),
               "duration": dur, "shots": len(shots), "width": specs["width"],
               "height": specs["height"], "has_audio": specs["has_audio"],
               "contact_sheet": arts[0] if len(arts) > 1 else None, "digest": j["id"]}
        return res, arts
    finally:
        shutil.rmtree(d, ignore_errors=True)


CLIP_ASPECTS = {"9:16": (9, 16, 720, 1280)}        # whitelist: never interpolated from a request


def clip_params(params: dict) -> dict:
    """The only knobs a requester has. Checked here as well as on the hub."""
    try:
        count = int(params.get("count", 3))
        seconds = float(params.get("seconds", 10))
    except (TypeError, ValueError):
        raise RelayError("clip: count and seconds must be numbers") from None
    aspect = str(params.get("aspect", "9:16"))
    if not 1 <= count <= 5:
        raise RelayError("clip: count must be 1 to 5")
    if not 6 <= seconds <= 20:
        raise RelayError("clip: seconds must be 6 to 20")
    if aspect not in CLIP_ASPECTS:
        raise RelayError(f"clip: aspect must be one of {', '.join(CLIP_ASPECTS)}")
    return {"artifact": str(params.get("artifact", "")), "count": count,
            "seconds": seconds, "aspect": aspect}


def motion(path) -> list[tuple[float, float]]:
    """How much the picture changes from each frame to the next (mean luma difference,
    0-255), measured on a small copy so it is quick. Best effort: [] if it cannot."""
    p = _run(["ffmpeg", "-hide_banner", "-v", "error", "-i", path, "-an", "-vf",
              "scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-",
              "-f", "null", "-"], check=False)
    out, t = [], None
    for line in p.stdout.splitlines():
        if "pts_time:" in line:
            try:
                t = float(line.split("pts_time:")[1].split()[0])
            except ValueError:
                t = None
        elif "YDIF=" in line and t is not None:
            try:
                out.append((t, float(line.split("YDIF=")[1])))
            except ValueError:
                pass
    return out


def pick_moments(shots: list[dict], duration: float, count: int, seconds: float,
                 activity: Optional[list] = None) -> list[dict]:
    """The clip rule: the busiest moments, never overlapping.

    Every shot offers one window of `seconds`: centred inside the shot when the shot is
    long enough, otherwise starting on the shot's cut so the clip opens cleanly, and kept
    inside the video. Windows are ranked by how much happens in them - the mean frame-to-
    frame change in the picture, ignoring the jumps at the cuts themselves - so a bunny
    chasing a butterfly beats a slow pan over a sky. Without a motion reading (or on a tie)
    the longer shot wins. Busiest first, a window that would overlap one already chosen is
    skipped, so a short video simply yields fewer clips. Returned in film order."""
    length = round(min(seconds, duration), 3)
    if length <= 0:
        return []
    cuts = [s["start"] for s in shots[1:]]
    cands = []
    for s in shots:
        span = s["end"] - s["start"]
        start = s["start"] + (span - length) / 2 if span >= length else s["start"]
        start = round(max(0.0, min(start, duration - length)), 3)
        end = round(start + length, 3)
        score = 0.0
        if activity:
            w = [y for t, y in activity if start <= t < end and all(not 0 <= t - c < 0.15 for c in cuts)]
            score = round(sum(w) / len(w), 3) if w else 0.0
        cands.append({"start": start, "end": end, "seconds": length, "shot": s.get("i"),
                      "activity": score, "_len": span})
    cands.sort(key=lambda c: (-c["activity"], -c["_len"], c["start"]))
    picked: list[dict] = []
    for c in cands:
        if len(picked) >= count:
            break
        if any(c["start"] < p["end"] and p["start"] < c["end"] for p in picked):
            continue
        picked.append({k: v for k, v in c.items() if k != "_len"})
    return sorted(picked, key=lambda p: p["start"])


def _shots_for(hub: Hub, meta: dict, src: str, d: str) -> tuple[list[dict], str]:
    """Reuse the digest's shot list when the hub has one, else find the cuts here."""
    dig = ((meta.get("meta") or {}).get("digest") or {}).get("result") or {}
    if dig.get("digest"):
        try:
            with open(hub.download(dig["digest"], os.path.join(d, "digest.json"))) as f:
                shots = json.load(f).get("shots") or []
            if shots:
                return shots, "digest"
        except (RelayError, OSError, ValueError):
            pass
    dur = probe(src)["duration"]
    bounds = [0.0] + [t for t in scene_cuts(src) if 0 < t < dur] + [dur]
    shots = [{"i": i, "start": round(a, 3), "end": round(b, 3), "len": round(b - a, 3)}
             for i, (a, b) in enumerate(zip(bounds, bounds[1:])) if b - a >= 0.3]
    return [dict(s, i=n) for n, s in enumerate(shots)], "scene detection"


def handle_clip(hub: Hub, job: dict, work: str):
    """Cut the strongest distinct moments of a video into short vertical clips (H.264, no
    audio, fast start) with a poster frame each, stored as artifacts of the source."""
    p = clip_params(job.get("params") or {})
    aid = p["artifact"]
    wn, hn, out_w, out_h = CLIP_ASPECTS[p["aspect"]]
    d = tempfile.mkdtemp(dir=work, prefix="clip_")
    try:
        meta = hub.get(f"/artifacts/{aid}/meta")
        ext = os.path.splitext(meta.get("name") or "")[1] or ".bin"
        src = hub.download(aid, os.path.join(d, "src" + ext))
        specs = probe(src)
        if not specs["has_video"] or not specs["width"] or not specs["height"]:
            raise RelayError("clip needs a video")
        shots, via = _shots_for(hub, meta, src, d)
        if not shots:
            shots, via = [{"i": 0, "start": 0.0, "end": specs["duration"]}], "whole video"
        moments = pick_moments(shots, specs["duration"], p["count"], p["seconds"], motion(src))
        if not moments:
            raise RelayError("clip: the video is too short to cut")
        # centre crop to the target shape; even sizes, whatever the source's shape
        sw, sh = specs["width"], specs["height"]
        cw, ch = sw, sh
        if sw * hn > sh * wn:
            cw = int(sh * wn / hn) // 2 * 2
        else:
            ch = int(sw * hn / wn) // 2 * 2
        cx, cy = (sw - cw) // 2, (sh - ch) // 2
        vf = f"crop={cw}:{ch}:{cx}:{cy},scale={out_w}:{out_h}:flags=lanczos,setsar=1"
        stem = os.path.splitext(os.path.basename(meta.get("name") or "video"))[0][:60] or "video"
        clips, arts = [], []
        for n, m in enumerate(moments, 1):
            mp4 = os.path.join(d, f"clip{n}.mp4")
            _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{m['start']:.3f}", "-i", src,
                  "-t", f"{m['seconds']:.3f}", "-vf", vf, "-an",
                  "-c:v", "libx264", "-preset", "slow", "-crf", "28", "-profile:v", "high",
                  "-maxrate", "2M", "-bufsize", "4M", "-pix_fmt", "yuv420p",
                  "-movflags", "+faststart", mp4])
            jpg = os.path.join(d, f"clip{n}.jpg")
            # the poster is the clip's own first frame, so pressing play never jumps
            _run(["ffmpeg", "-y", "-v", "error", "-i", mp4, "-frames:v", "1",
                  "-vf", f"scale={out_w // 2}:{out_h // 2}", "-q:v", "4", jpg])
            v = hub.upload(mp4, name=f"{stem}_clip{n}.mp4", mime="video/mp4", kind="clip", parent=aid)
            j = hub.upload(jpg, name=f"{stem}_clip{n}.jpg", mime="image/jpeg", kind="poster", parent=aid)
            arts += [v["id"], j["id"]]
            clips.append({"artifact": v["id"], "poster": j["id"], "start": m["start"],
                          "end": m["end"], "seconds": m["seconds"], "shot": m["shot"],
                          "activity": m["activity"], "size": v["size"]})
        secs = f"{moments[0]['seconds']:g}"
        res = {"summary": (f"{len(clips)} vertical clip{'s' if len(clips) != 1 else ''} "
                           f"({p['aspect']}, {secs} s each) from {len(shots)} shots"),
               "clips": clips, "aspect": p["aspect"], "width": out_w, "height": out_h,
               "shots_from": via,
               "rule": "one window per shot, busiest (most frame-to-frame change) first, no overlaps, in film order"}
        return res, arts
    finally:
        shutil.rmtree(d, ignore_errors=True)


HANDLERS = {"fetch": handle_fetch, "digest": handle_digest, "clip": handle_clip}


def run_node(hub: Hub, caps: list[str], once=False, poll=5.0, work=None, log=print):
    caps = sorted(set(caps))
    unknown = [c for c in caps if c not in HANDLERS]
    if unknown:
        raise RelayError(f"this node has no handler for: {', '.join(unknown)}")
    media = bool({"digest", "clip"} & set(caps))
    for tool in (["yt-dlp"] if "fetch" in caps else []) + (["ffmpeg", "ffprobe"] if media else []):
        if not shutil.which(tool):
            raise RelayError(f"{tool} is not installed on this machine")
    work = work or tempfile.mkdtemp(prefix="relay_node_")
    os.makedirs(work, exist_ok=True)      # a fresh machine has no work folder yet
    me = hub.put("/agents/me/caps", {"caps": caps})
    log(f"[node] @{me['handle']} serving {', '.join(caps)}")
    done = 0
    while True:
        got = hub.post("/jobs/claim", {"kinds": caps})
        job = got.get("job") if got else None
        if not job:
            if once:
                return done
            time.sleep(poll)
            continue
        lease = float(got.get("lease_seconds", 300))
        log(f"[node] claimed {job['id']} [{job['kind']}]")
        stop = threading.Event()

        def beat(jid=job["id"]):
            while not stop.wait(max(1.0, lease / 3)):
                try:
                    hub.post(f"/jobs/{jid}/heartbeat")
                except RelayError:
                    return
        t = threading.Thread(target=beat, daemon=True)
        t.start()
        try:
            result, arts = HANDLERS[job["kind"]](hub, job, work)
            stop.set()
            hub.post(f"/jobs/{job['id']}/complete", {"result": result, "artifacts": arts})
            log(f"[node] done {job['id']}: {result.get('summary', '')}")
            done += 1
        except Exception as e:
            stop.set()
            log(f"[node] failed {job['id']}: {e}")
            try:
                hub.post(f"/jobs/{job['id']}/fail", {"error": str(e)[:3800], "retry": True})
            except RelayError as e2:
                log(f"[node] could not report failure: {e2}")


# --------------------------------------------------------------------------- CLI
def _params(pairs):
    out = {}
    for p in pairs or []:
        k, _, v = p.partition("=")
        out[k] = v
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relay", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("RELAY_URL"))
    ap.add_argument("--token", default=os.environ.get("RELAY_TOKEN"))
    ap.add_argument("--as", dest="agent", default=os.environ.get("RELAY_AGENT"),
                    help="which agent to act as, when several have joined from this computer")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("join", help="join a hub with a one-time invite code (saves your token locally)")
    p.add_argument("hub_url"); p.add_argument("code")

    p = sub.add_parser("connect", help="add this hub as a connector in a local app: claude-code or codex")
    p.add_argument("app", choices=["claude-code", "codex"])
    sub.add_parser("mcp-headers", help="print this agent's Authorization header as JSON (for app header helpers)")
    p = sub.add_parser("listen", help="hear new messages instantly; --run a command to wake your agent")
    p.add_argument("--run", help="command to run when new messages arrive")
    p.add_argument("--once", action="store_true", help="stop after the first batch")
    r = sub.add_parser("register", help="admin: create an agent, print its token once")
    r.add_argument("handle"); r.add_argument("--cap", action="append", default=[])
    r.add_argument("--human", action="store_true")
    sub.add_parser("whoami"); sub.add_parser("agents")

    p = sub.add_parser("post"); p.add_argument("--channel", required=True)
    p.add_argument("--text", required=True); p.add_argument("--mention", action="append", default=[])
    p.add_argument("--artifact", action="append", default=[])
    p = sub.add_parser("reply"); p.add_argument("id", type=int); p.add_argument("--text", required=True)
    p.add_argument("--mention", action="append", default=[]); p.add_argument("--artifact", action="append", default=[])
    p = sub.add_parser("inbox"); p.add_argument("--ack", action="store_true"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("subscribe"); p.add_argument("channel")
    p = sub.add_parser("thread"); p.add_argument("id", type=int)

    p = sub.add_parser("job", help="create a job addressed to a capability, or `job get ID`")
    p.add_argument("kind"); p.add_argument("id", nargs="?")
    p.add_argument("--param", action="append", default=[]); p.add_argument("--approval", action="store_true")
    p.add_argument("--note")
    p = sub.add_parser("jobs"); p.add_argument("--status"); p.add_argument("--kind")
    p = sub.add_parser("approve"); p.add_argument("id")

    p = sub.add_parser("upload"); p.add_argument("file"); p.add_argument("--kind", default="file")
    p = sub.add_parser("download"); p.add_argument("id"); p.add_argument("-o", "--out", required=True)
    p = sub.add_parser("meta"); p.add_argument("id")

    p = sub.add_parser("node", help="serve capabilities for other agents")
    p.add_argument("--cap", action="append", required=True)
    p.add_argument("--once", action="store_true"); p.add_argument("--poll", type=float, default=5.0)

    a = ap.parse_args(argv)
    try:
        if a.cmd == "join":
            out = join(a.hub_url, a.code)
            caps = f" (serves: {', '.join(out['caps'])})" if out["caps"] else ""
            print(f"joined as @{out['handle']}{caps}. Token saved to {out['config']} - keep it private.")
            print(f"next: python3 cli.py --as {out['handle']} whoami")
            return 0
        cfg = {} if (a.url and a.token) else load_config(a.agent)
        hub = Hub(a.url or cfg.get("url"), a.token or cfg.get("token"))
        if a.cmd == "connect":
            me = hub.get("/agents/me")                            # proves the key works first
            print(connect_app(a.app, hub.url, me["handle"], hub.token))
            return 0
        if a.cmd == "listen":
            listen(hub, run=a.run, once=a.once)
            return 0
        if a.cmd == "mcp-headers":
            print(json.dumps({"Authorization": f"Bearer {hub.token}"}))
            return 0
        show = lambda x: print(json.dumps(x, indent=1))
        if a.cmd == "register":
            show(hub.post("/agents", {"handle": a.handle, "caps": a.cap,
                                      "role": "human" if a.human else "agent"}))
        elif a.cmd == "whoami":
            show(hub.get("/agents/me"))
        elif a.cmd == "agents":
            show(hub.get("/agents"))
        elif a.cmd == "post":
            show(hub.post("/messages", {"channel": a.channel, "text": a.text,
                                        "mentions": a.mention, "artifacts": a.artifact}))
        elif a.cmd == "reply":
            show(hub.post("/messages", {"reply_to": a.id, "text": a.text,
                                        "mentions": a.mention, "artifacts": a.artifact}))
        elif a.cmd == "inbox":
            box = hub.get("/inbox", limit=a.limit)
            for m in box["messages"]:
                extra = f"  [{len(m['artifacts'])} artifact(s)]" if m["artifacts"] else ""
                print(f"#{m['id']} @{m['from']} in {m['channel']} (thread {m['thread']}): {m['text']}{extra}")
            if a.ack and box["messages"]:
                hub.post("/inbox/ack", {"cursor": box["cursor"]})
            if not box["messages"]:
                print("(nothing new)")
        elif a.cmd == "subscribe":
            show(hub.post("/subscriptions", {"channel": a.channel}))
        elif a.cmd == "thread":
            for m in hub.get("/messages", thread=a.id, limit=200):
                print(f"#{m['id']} @{m['from']}: {m['text']}")
        elif a.cmd == "job":
            if a.kind == "get":
                show(hub.get(f"/jobs/{a.id}"))
            else:
                show(hub.post("/jobs", {"kind": a.kind, "params": _params(a.param),
                                        "requires_approval": a.approval, "note": a.note}))
        elif a.cmd == "jobs":
            for j in hub.get("/jobs", status=a.status, kind=a.kind):
                print(f"{j['id']}  {j['kind']:<7} {j['status']:<15} by @{j['requested_by']}"
                      f"  {(j['result'] or {}).get('summary', j['error'] or '')}")
        elif a.cmd == "approve":
            show(hub.post(f"/jobs/{a.id}/approve"))
        elif a.cmd == "upload":
            show(hub.upload(a.file, kind=a.kind))
        elif a.cmd == "download":
            print(hub.download(a.id, a.out))
        elif a.cmd == "meta":
            show(hub.get(f"/artifacts/{a.id}/meta"))
        elif a.cmd == "node":
            run_node(hub, a.cap, once=a.once, poll=a.poll)
    except RelayError as e:
        print(f"relay: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
