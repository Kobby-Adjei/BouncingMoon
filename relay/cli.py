#!/usr/bin/env python3
"""relay - client and worker node for a Relay hub. Python standard library only.

Copy this one file to any machine. Configure with two environment variables:

    RELAY_URL=https://your-hub      RELAY_TOKEN=rly_...

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
"""
from __future__ import annotations

import argparse
import glob
import json
import mimetypes
import os
import shutil
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


class Hub:
    def __init__(self, url: str, token: str):
        if not url or not token:
            raise RelayError("set RELAY_URL and RELAY_TOKEN")
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

    def download(self, aid, dest) -> str:
        req = self._req("GET", f"/artifacts/{aid}")
        try:
            with urllib.request.urlopen(req, timeout=3600) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, CHUNK)
        except urllib.error.HTTPError as e:
            raise RelayError(f"download -> {e.code}") from None
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


def contact_sheet(path, shots, specs, out, cols=6, max_tiles=60) -> int:
    """One labelled thumbnail per shot, tiled. This is what lets an agent that cannot
    watch video still see what is in it."""
    if not shots:
        return 0
    if len(shots) > max_tiles:
        step = len(shots) / max_tiles
        shots = [shots[int(i * step)] for i in range(max_tiles)]
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
def handle_fetch(hub: Hub, job: dict, work: str):
    """Fetch one URL. Fixed argv, no shell, no arbitrary commands: the only input a
    requester controls is the URL, and it is passed after `--`."""
    url = job["params"]["url"]
    if not url.startswith(("http://", "https://")):
        raise RelayError("fetch only accepts http(s) URLs")
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


HANDLERS = {"fetch": handle_fetch, "digest": handle_digest}


def run_node(hub: Hub, caps: list[str], once=False, poll=5.0, work=None, log=print):
    caps = sorted(set(caps))
    unknown = [c for c in caps if c not in HANDLERS]
    if unknown:
        raise RelayError(f"this node has no handler for: {', '.join(unknown)}")
    for tool in (["yt-dlp"] if "fetch" in caps else []) + (["ffmpeg", "ffprobe"] if "digest" in caps else []):
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
    sub = ap.add_subparsers(dest="cmd", required=True)

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
        hub = Hub(a.url, a.token)
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
