"""Media: share videos, audio, images and files between agents and people, already understood.

Any agent or person can put media into a conversation: an agent attaches a link or a file already on
the hub, or a person uses an upload link an agent asked for (a page that works on any phone, picks from
the camera roll, and keeps going when the connection drops). No Drive, no command line.

The hub does the watching once, when the media arrives, so no agent has to:
  video  specs, shot changes, a labelled contact sheet, a timed transcript, speech/silence, loudness,
         a copy every browser plays (H.264/AAC MP4) and a poster frame
  audio  specs, a timed transcript, speech/silence, loudness, a copy every browser plays
  image  location and other metadata stripped (the original with it is not kept), a preview an agent
         can look at. JPEG and PNG lose only the metadata blocks: the pixels stay bit-for-bit the same,
         so the exact photo can be posted. Other formats are re-saved without metadata.
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
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.parse
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


# ---------------------------------------------------------------- lossless metadata removal
# A photo's location, camera and time live in labelled blocks beside the pixels. Cutting those blocks
# out (instead of re-saving the picture) keeps every pixel exactly as it was, so the clean copy is the
# one to post. Only what a picture needs to look right is kept: in a JPEG the JFIF header, the colour
# profile, Adobe's colour flag, and the orientation (rewritten as a one-entry EXIF block, so a phone
# photo isn't shown sideways); in a PNG everything but its text, time and EXIF chunks.
def _orientation(exif: bytes) -> int:
    """The EXIF orientation (1-8) from an APP1 'Exif' payload, or 0."""
    try:
        t = exif[6:]
        le = t[:2] == b"II"
        if not le and t[:2] != b"MM":
            return 0
        u16 = lambda b: int.from_bytes(b, "little" if le else "big")
        ifd = int.from_bytes(t[4:8], "little" if le else "big")
        for i in range(u16(t[ifd:ifd + 2])):
            e = t[ifd + 2 + 12 * i: ifd + 14 + 12 * i]
            if u16(e[0:2]) == 0x0112:
                v = u16(e[8:10])
                return v if 1 <= v <= 8 else 0
    except (IndexError, ValueError):
        return 0
    return 0


def _exif_orientation_block(v: int) -> bytes:
    body = (b"Exif\0\0" + b"MM\0\x2a" + (8).to_bytes(4, "big") + (1).to_bytes(2, "big")
            + (0x0112).to_bytes(2, "big") + (3).to_bytes(2, "big") + (1).to_bytes(4, "big")
            + v.to_bytes(2, "big") + b"\0\0" + (0).to_bytes(4, "big"))
    return b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body


def strip_jpeg(data: bytes) -> Optional[bytes]:
    """The same JPEG without its metadata blocks, or None if it isn't a JPEG this can walk."""
    if data[:2] != b"\xff\xd8":
        return None
    out, i, turn = [b"\xff\xd8"], 2, 0
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            return None
        marker = data[i + 1]
        if marker == 0xFF:                      # fill byte
            i += 1
            continue
        if marker == 0xDA:                      # start of scan: the picture itself, copied untouched
            if turn > 1:                        # after the JFIF header, which must come first
                out.insert(2 if len(out) > 1 and out[1][:2] == b"\xff\xe0" else 1, _exif_orientation_block(turn))
            out.append(data[i:])
            return b"".join(out)
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            out.append(data[i:i + 2]); i += 2
            continue
        n = int.from_bytes(data[i + 2:i + 4], "big")
        if n < 2 or i + 2 + n > len(data):
            return None
        seg, payload = data[i:i + 2 + n], data[i + 4:i + 2 + n]
        if marker == 0xE1 and payload.startswith(b"Exif\0\0"):
            turn = turn or _orientation(payload)
        keep = (marker < 0xE0 and marker != 0xFE) \
            or (marker == 0xE0 and payload.startswith(b"JFIF\0")) \
            or (marker == 0xE2 and payload.startswith(b"ICC_PROFILE\0")) \
            or (marker == 0xEE and payload.startswith(b"Adobe"))
        if keep:
            out.append(seg)
        i += 2 + n
    return None


# types the exact-file link may say out loud; anything else (HTML, SVG, ...) is served as a plain download
EXACT_TYPES = {"video/mp4", "video/quicktime", "video/webm", "video/x-matroska", "audio/mpeg", "audio/mp4",
               "audio/aac", "audio/wav", "audio/x-wav", "audio/ogg", "audio/flac", "image/jpeg", "image/png",
               "image/webp", "image/gif", "image/heic", "image/heif"}

PNG_DROP = {b"tEXt", b"iTXt", b"zTXt", b"tIME", b"eXIf"}


def strip_png(data: bytes) -> Optional[bytes]:
    """The same PNG without its text, time and EXIF chunks, or None if it isn't a PNG this can walk."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    out, i = [data[:8]], 8
    while i + 12 <= len(data):
        n = int.from_bytes(data[i:i + 4], "big")
        kind = data[i + 4:i + 8]
        end = i + 12 + n
        if end > len(data):
            return None
        if kind not in PNG_DROP:
            out.append(data[i:end])
        i = end
        if kind == b"IEND":
            return b"".join(out)
    return None


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


# --------------------------------------------------------------------------- reading documents
# A document is read the way a video is watched: its text is pulled out once, so an agent on the other side
# reads it (view_media) instead of getting a file name and a link it can't open.
DOC_TEXT_MAX = 400_000           # characters of a document's text kept
DOC_PAGES_MAX = 300              # pages of a PDF read
TEXT_TYPES = {".txt": "text/plain", ".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv",
              ".tsv": "text/tab-separated-values", ".json": "application/json", ".yaml": "application/yaml",
              ".yml": "application/yaml", ".log": "text/plain", ".xml": "application/xml", ".html": "text/html",
              ".htm": "text/html", ".rtf": "application/rtf", ".tex": "text/plain", ".py": "text/plain",
              ".js": "text/plain", ".ts": "text/plain", ".sql": "text/plain", ".ini": "text/plain", ".toml": "text/plain"}
OFFICE = {".docx": ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
          ".pptx": ("slides", "application/vnd.openxmlformats-officedocument.presentationml.presentation")}
_XML_PART_MAX = 60 * 2**20      # an unpacked part bigger than this is a zip bomb, not a document


def _xml_text(xml: str, para: str, run: str) -> str:
    """The words in an Office XML part: one line per paragraph. Regex, not an XML parser: nothing
    in the file can make it expand entities or reach out anywhere."""
    xml = re.sub(r"<w:tab/>|<a:tab/>", "\t", xml)
    xml = re.sub(r"<w:br/>|<a:br/>", "\n", xml)
    out = []
    for p in re.split(rf"</{para}>", xml):
        words = "".join(re.findall(rf"<{run}(?:\s[^>]*)?>([^<]*)</{run}>", p))
        if words.strip():
            out.append(html.unescape(words))
    return "\n".join(out)


def read_document(src: str, name: str, work: str) -> Optional[dict]:
    """{doc, mime, text, pages?, poster?} for a PDF, a Word or PowerPoint file, or plain text; None otherwise."""
    import zipfile
    ext = os.path.splitext(name or "")[1].lower()
    with open(src, "rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF"):
        out = os.path.join(work, "doc.txt")
        try:
            _run(["pdftotext", "-q", "-enc", "UTF-8", "-l", str(DOC_PAGES_MAX), src, out], 120)
            text = open(out, encoding="utf-8", errors="replace").read()
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            text = ""
        pages = None
        try:
            m = re.search(r"^Pages:\s+(\d+)", _run(["pdfinfo", src], 30).stdout, re.M)
            pages = int(m.group(1)) if m else None
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            pass
        poster = os.path.join(work, "page1")
        try:
            _run(["pdftoppm", "-q", "-jpeg", "-f", "1", "-l", "1", "-scale-to", "800", "-singlefile", src, poster], 60)
            poster += ".jpg"
        except (RuntimeError, OSError, subprocess.TimeoutExpired):
            poster = None
        return {"doc": "pdf", "mime": "application/pdf", "text": text.replace("\f", "\n\n"), "pages": pages,
                "poster": poster if poster and os.path.exists(poster) else None}
    if head.startswith(b"PK\x03\x04") and ext in OFFICE:
        doc, mime = OFFICE[ext]
        try:
            with zipfile.ZipFile(src) as z:
                def part(n):
                    i = z.getinfo(n)
                    if i.file_size > _XML_PART_MAX:
                        raise ValueError("too big")
                    return z.read(n).decode("utf-8", "replace")
                if doc == "word":
                    text, pages = _xml_text(part("word/document.xml"), "w:p", "w:t"), None
                else:
                    slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                    key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
                    text = "\n\n".join(f"Slide {i}\n" + _xml_text(part(n), "a:p", "a:t") for i, n in enumerate(slides, 1))
                    pages = len(slides)
        except (zipfile.BadZipFile, KeyError, ValueError, OSError):
            return None
        return {"doc": doc, "mime": mime, "text": text, "pages": pages}
    with open(src, "rb") as f:
        raw = f.read(DOC_TEXT_MAX * 4 + 1)
    if b"\x00" in raw[:65536]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:               # cut mid-character at the end is still text
        if e.start < len(raw) - 4:
            return None
        text = raw[:e.start].decode("utf-8")
    if ext not in TEXT_TYPES and not text.strip():
        return None
    return {"doc": "text", "mime": TEXT_TYPES.get(ext, "text/plain"), "text": text}


VIEW_TEXT_MAX = 60_000           # characters of a document shown on its viewer page
VIEW_PAGES_MAX = 40              # pages of a PDF shown as pictures on its viewer page
CODE_EXT = {".py", ".js", ".ts", ".sql", ".ini", ".toml", ".yaml", ".yml", ".xml", ".html", ".htm", ".log", ".tex", ".json"}


def _inline(t: str) -> str:
    """Escaped text with **bold**, *italic*, `code` and [links](https://…) (web links only)."""
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", t)
    return re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
                  lambda m: f"<a href='{m.group(2)}' rel='noopener noreferrer nofollow' target=_blank>{m.group(1)}</a>", t)


def markdown_html(text: str) -> str:
    """A small, safe Markdown: headings, lists, quotes, code blocks, paragraphs. Everything is escaped first."""
    out, para, items, kind, code, table = [], [], [], None, None, []
    cells = lambda l: [c.strip() for c in l.strip().strip("|").split("|")]
    def flush():
        nonlocal para, items, kind, table
        if table:                                   # | a | b | rows: a table when the second row is | --- | --- |
            if len(table) >= 2 and all(re.fullmatch(r":?-{3,}:?", c) for c in cells(table[1])):
                head = "".join(f"<th>{_inline(c)}</th>" for c in cells(table[0]))
                body = "".join("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells(r)) + "</tr>" for r in table[2:])
                out.append(f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>")
            else:
                para.extend(table)
            table = []
        if para:
            out.append("<p>" + "<br>".join(_inline(x) for x in para) + "</p>")
        if items:
            out.append(f"<{kind}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{kind}>")
        para, items, kind = [], [], None
    for line in text.splitlines():
        if code is not None:
            if line.strip().startswith("```"):
                out.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
                code = None
            else:
                code.append(line)
            continue
        if line.strip().startswith("```"):
            flush(); code = []; continue
        if line.strip().startswith("|") and line.strip().endswith("|") and line.count("|") >= 2:
            if not table:
                flush()
            table.append(line); continue
        if table:
            flush()
        m = re.match(r"(#{1,6})\s+(.*)", line)
        if m:
            flush(); level = min(len(m.group(1)) + 1, 4)
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>"); continue
        m = re.match(r"\s*(?:[-*+]|(\d+)[.)])\s+(.*)", line)
        if m:
            k = "ol" if m.group(1) else "ul"
            if para or (items and kind != k):
                flush()
            kind = k; items.append(m.group(2)); continue
        if line.startswith(">"):
            flush(); out.append("<blockquote>" + _inline(line.lstrip("> ")) + "</blockquote>"); continue
        if re.fullmatch(r"\s*(-{3,}|\*{3,})\s*", line):
            flush(); out.append("<hr>"); continue
        if not line.strip():
            flush(); continue
        if items:
            flush()
        para.append(line)
    if code is not None:
        out.append("<pre>" + html.escape("\n".join(code)) + "</pre>")
    flush()
    return "".join(out)


def table_html(text: str, sep: str) -> str:
    import csv, io
    rows = []
    for i, row in enumerate(csv.reader(io.StringIO(text), delimiter=sep)):
        if i >= 300:
            break
        rows.append(row[:30])
    if not rows:
        return ""
    numeric = lambda v: bool(re.fullmatch(r"\s*[-+$€£]?\s*[\d,]*\.?\d+\s*%?\s*", v))
    cols = max(len(r) for r in rows)
    num = [bool(rows[1:]) and all(numeric(r[i]) for r in rows[1:] if i < len(r) and r[i].strip()) for i in range(cols)]
    cell = lambda tag, i, v: f"<{tag}{' class=num' if i < len(num) and num[i] else ''}>{html.escape(v)}</{tag}>"
    head = "".join(cell("th", i, c) for i, c in enumerate(rows[0]))
    body = "".join("<tr>" + "".join(cell("td", i, c) for i, c in enumerate(r)) + "</tr>" for r in rows[1:])
    return f"<div class=tablewrap><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>"


def table_size(text: str, sep: str) -> str:
    """'12 rows · 3 columns' for a CSV: what a table's length is, instead of a word count."""
    import csv, io
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=sep) if any(c.strip() for c in r)]
    if not rows:
        return ""
    n, cols = len(rows) - 1, max(len(r) for r in rows)
    return f"{n:,} {'row' if n == 1 else 'rows'} · {cols} {'column' if cols == 1 else 'columns'}"


def document_html(doc: str, name: str, text: str) -> str:
    """A document's text as a page people read: formatted Markdown, a CSV as a table, slides by slide, prose in paragraphs."""
    ext = os.path.splitext(name or "")[1].lower()
    if ext in (".md", ".markdown"):
        return markdown_html(text)
    if ext in (".csv", ".tsv"):
        return table_html(text, "\t" if ext == ".tsv" else ",") or "<p></p>"
    if ext == ".json":
        try:
            text = json.dumps(json.loads(text), indent=2, ensure_ascii=False)
        except ValueError:
            pass
    if ext in CODE_EXT:
        return "<pre>" + html.escape(text) + "</pre>"
    if doc == "slides":
        parts = re.split(r"(?m)^(Slide \d+)$", text)
        out = []
        for i in range(1, len(parts), 2):
            body = "".join(f"<p>{html.escape(l)}</p>" for l in parts[i + 1].splitlines() if l.strip())
            out.append(f"<section class=slide><h2>{html.escape(parts[i])}</h2>{body}</section>")
        return "".join(out)
    blocks = re.split(r"\n\s*\n", text)          # prose: blank lines make paragraphs, single lines stay lines
    if len(blocks) == 1:                            # one line per paragraph (Word, and most extracted text)
        blocks = text.splitlines()
    # a short line with no closing punctuation that starts a block reads as a heading ("Timeline", "1. Methods")
    heading = lambda l: 0 < len(l) <= 60 and not re.search(r"[.,;:!?…)\"'”]$", l) and len(l.split()) <= 8
    out = []
    for b in blocks:
        lines = [l.strip() for l in b.strip().splitlines() if l.strip()]
        if not lines:
            continue
        if heading(lines[0]) and (len(lines) > 1 or len(blocks) > 1):
            out.append(f"<h2 class=sub>{html.escape(lines[0])}</h2>")
            lines = lines[1:]
        if lines:
            out.append("<p>" + "<br>".join(html.escape(l) for l in lines) + "</p>")
    return "".join(out)


PDF_FONTS = ("/usr/share/fonts/truetype/liberation/LiberationSans-{}.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans{}.ttf")
PDF_TEXT_MAX = 400_000           # characters of text a PDF is made from


def text_to_pdf(name: str, text: str) -> bytes:
    """A real PDF from a document's text (Markdown formatted, CSV as a table, prose in paragraphs). Small and quick:
    fpdf2, pure Python, with the fonts already on the hub. Raises MediaError when it can't."""
    try:
        from fpdf import FPDF
        from fpdf.fonts import FontFace
    except ImportError:
        raise MediaError("Making PDFs isn't available on this hub.")
    ext = os.path.splitext(name or "")[1].lower()
    text = text[:PDF_TEXT_MAX]
    if ext in (".md", ".markdown", ".pdf", ""):
        body = markdown_html(text)                    # a .pdf an agent writes: its text is Markdown
    else:
        body = document_html("slides" if ext == ".pptx" else "text", name, text)
    body = re.sub(r"</?(?:section|div|article)[^>]*>", "", body)        # layout wrappers the PDF doesn't need
    body = re.sub(r"<(h\d|th|td|tr|table|thead|tbody|p)\s+class=[^>]*>", r"<\1>", body)
    pdf = FPDF(format="Letter")
    pdf.set_margins(22, 20, 22)
    pdf.set_auto_page_break(True, 18)
    pdf.set_title(os.path.splitext(name or "Document")[0])
    pdf.set_creator("Relay")
    family = "Helvetica"
    for pattern, names in ((PDF_FONTS[0], ("Regular", "Bold", "Italic", "BoldItalic")), (PDF_FONTS[1], ("", "-Bold", "-Oblique", "-BoldOblique"))):
        paths = [pattern.format(n) for n in names]
        if all(os.path.exists(p) for p in paths):
            for style, path in zip(("", "B", "I", "BI"), paths):
                pdf.add_font("Body", style, path)
            family = "Body"
            break
    if family == "Helvetica":                        # the PDF core font knows Latin-1 only
        body = body.encode("latin-1", "replace").decode("latin-1")
    pdf.add_page()
    pdf.set_font(family, size=11)
    ink = (24, 23, 29)
    styles = {"h1": FontFace(emphasis="BOLD", size_pt=20, color=ink), "h2": FontFace(emphasis="BOLD", size_pt=16, color=ink),
              "h3": FontFace(emphasis="BOLD", size_pt=13, color=ink), "h4": FontFace(emphasis="BOLD", size_pt=11.5, color=ink),
              "a": FontFace(color=(79, 68, 224), emphasis="UNDERLINE"), "blockquote": FontFace(color=(107, 105, 114)),
              "code": FontFace(family="Courier", size_pt=10, color=ink), "pre": FontFace(family="Courier", size_pt=9.5, color=ink)}
    try:
        pdf.write_html(body or "<p></p>", font_family=family, tag_styles=styles, li_prefix_color=ink)
        return bytes(pdf.output())
    except Exception as e:                           # a shape the converter can't lay out: say so, don't crash
        raise MediaError(f"Couldn't make a PDF of this: {str(e)[:200]}")


def title_html(name: str) -> str:
    """A file name that breaks at its _ - . instead of in the middle of a word."""
    return re.sub(r"([_.\-])", r"\1<wbr>", html.escape(name))


VERSIONED = set(TEXT_TYPES) | set(OFFICE) | {".pdf"}     # sent again under the same name: a new version, never a replacement


def excerpt(text: str, n: int = 300) -> str:
    """The start of a document on one line, for a card."""
    one = " ".join(text.split())
    return one if len(one) <= n else one[:n - 1].rstrip() + "…"


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
KEEP_FREE = float(os.environ.get("RELAY_KEEP_FREE", 0.15))     # new files stop when less than this share is free


def disk(path: str) -> tuple:
    """(free bytes, total bytes) of the disk `path` is on."""
    u = shutil.disk_usage(path)
    return u.free, u.total


def room(path: str, nbytes: int, keep: float = None) -> bool:
    free, total = disk(path)
    return free - max(nbytes, 0) >= (KEEP_FREE if keep is None else keep) * total


class Media:
    def __init__(self, store, private_host: Callable[[str], bool], secret: str,
                 transcriber: Optional[captions.Transcriber] = None, threads: bool = True,
                 allow_private: bool = False, public_url: Optional[str] = None, max_upload: int = 4 * 2 ** 30,
                 now: Callable[[], float] = time.time, log=print, idle_seconds: float = 300):
        self.store, self.private_host, self.threads = store, private_host, threads
        self.key = hashlib.sha256(b"relay-media-links:" + secret.encode()).digest()
        self.old_keys: list = []
        self.transcriber = transcriber
        self.allow_private, self.max_upload, self.now, self.log, self.idle = allow_private, max_upload, now, log, idle_seconds
        self.public_url = (public_url or os.environ.get("RELAY_PUBLIC_URL") or
                           ("https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"] if os.environ.get("RAILWAY_PUBLIC_DOMAIN") else "")
                           ).rstrip("/") or None
        self.on_upload: Optional[Callable[[dict, str], None]] = None     # the hub tells the asking chat
        self.may_use: Optional[Callable] = None      # (db, who, file id) -> may `who` share that hub file?
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.wake = threading.Event()
        self.tmp = os.path.join(store.tmp, "media")
        os.makedirs(self.tmp, exist_ok=True)
        with store.tx() as c:
            for stmt in SCHEMA.split(";"):
                if stmt.strip():
                    c.execute(stmt)
            mcols = {r["name"] for r in c.execute("PRAGMA table_info(media)")}
            if "origin_artifact" not in mcols:
                c.execute("ALTER TABLE media ADD COLUMN origin_artifact TEXT")   # the hub file it was made from
            if "version" not in mcols:          # a document sent again under the same name in a conversation: v2, v3…
                c.execute("ALTER TABLE media ADD COLUMN version INTEGER")
                c.execute("ALTER TABLE media ADD COLUMN prev TEXT")


    def also_accept(self, secret: str) -> None:
        """Keep honouring links signed with an older secret (they expire within days anyway)."""
        self.old_keys.append(hashlib.sha256(b"relay-media-links:" + secret.encode()).digest())

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

    def from_artifact(self, by: str, artifact: str, check: bool = True) -> dict:
        with self.store.tx() as c:
            a = c.execute("SELECT * FROM artifacts WHERE id=?", (artifact,)).fetchone()
            if not a or (check and self.may_use and not self.may_use(c, by, artifact)):
                raise MediaError(f"No file {artifact} on the hub.")
            # always a media item of its own (who shared it, in which conversation); the worker reuses
            # what was already worked out for the same bytes
            mid = self._new(c, by, a["name"], artifact=artifact, size=a["size"], origin_artifact=artifact)
        self._ensure_thread()
        return self.get(mid)

    def from_text(self, by: str, name: str, text: str) -> dict:
        """A document an agent wrote (a name and its text) becomes a real file: people download it, agents read it."""
        name = safe_name(name, "note.txt")
        if not os.path.splitext(name)[1]:
            name += ".txt"
        data = text_to_pdf(name, text) if name.lower().endswith(".pdf") else text.encode("utf-8")   # .pdf: made from the text
        fd, tmp = tempfile.mkstemp(dir=self.tmp)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        aid, size = self._store(tmp, name, by, None, "upload", "application/octet-stream")
        with self.store.tx() as c:
            mid = self._new(c, by, name, artifact=aid, size=size, origin_artifact=aid)
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
                mid = self.from_artifact(m["sender"], aid, check=False)["id"]     # it was checked when sent
            except MediaError:
                continue                                   # a file that's gone from the hub
            new.append(mid)
            with self.store.tx() as c:
                c.execute("INSERT OR IGNORE INTO message_media(msg,media) VALUES (?,?)", (msg_id, mid))
        with self.store.ro() as c:
            return [r["media"] for r in c.execute("SELECT media FROM message_media WHERE msg=? ORDER BY rowid", (msg_id,))]

    def download_link(self, mid: str, seconds: float = LINK_SECONDS) -> str:
        """A direct link to the exact file (photos without their location data), for a browser or a tool.
        It ends in the file's name, since some posting services want a URL that ends in .mp4."""
        name = safe_name(self.row(mid)["name"] or "file")
        return self.link(mid, seconds) + "/original/" + urllib.parse.quote(name, safe="")

    def from_file(self, by: str, path: str, name: str) -> dict:
        aid, size = self._store(path, name, by, None, "upload", "application/octet-stream")
        return self.from_artifact(by, aid, check=False)

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
                "revoked": bool(r["revoked"]), "digest": d, "version": r["version"] or 1, "prev": r["prev"],
                "has": {k: bool(r[k]) for k in ("playable", "poster", "sheet", "preview")}}

    def brief(self, mid: str) -> dict:
        """What a conversation in the web app shows for a file: a card with a picture, linking to its page.
        Links are paths on this hub (the page loads them itself)."""
        m = self.get(mid)
        d = m["digest"]
        out = {"id": mid, "kind": m["kind"] or "file", "name": m["name"], "status": m["status"], "size": m["size"],
               "duration": d.get("duration"), "width": d.get("width"), "height": d.get("height"),
               "shots": len(d["shots"]) + 1 if d.get("shots") is not None else None,
               "transcribed": bool(d.get("transcript")),
               "doc": d.get("doc"), "pages": d.get("pages"), "words": d.get("words"), "read": bool(d.get("text")),
               "version": m["version"]}
        if m["revoked"]:
            return {**out, "status": "removed"}
        token = self.link(mid)[len(self.public_url or ""):]
        out["page"] = token
        if m["has"]["poster"]:
            out["poster"] = token + "/poster"
        return out

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
        icon = {"video": "📹", "audio": "🎧", "image": "🖼", "file": "📄", "document": "📄"}.get(kind, "📎")
        head = [f"{icon} {m['id']} · {m['name'] or kind}"
                + (f" (version {m['version']}; the earlier one is {m['prev']})" if m.get("version", 1) > 1 and m.get("prev") else "")]
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
            if kind == "document":
                size = [f"{d['pages']} {'slides' if d.get('doc') == 'slides' else 'pages'}" if d.get("pages") else None,
                        f"{d.get('words', 0):,} words"]
                if d.get("text"):
                    lines.append(f"   Starts: \"{d.get('excerpt', '')}\"")
                    lines.append(f"   Read it all ({', '.join(x for x in size if x)}): view_media {m['id']}")
                else:
                    lines.append("   No text in it (a scan or pictures only): open the link to see it.")
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
        good = False
        for key in [self.key, *self.old_keys]:       # links signed before the hub had its own secret still work
            want = base64.urlsafe_b64encode(hmac.new(key, f"{mid}.{exp}".encode(), hashlib.sha256).digest()[:18]).decode()
            good = hmac.compare_digest(want, parts[2]) or good
        if not good:
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
            p = self.blob(r["sheet"] or r["preview"] or (r["poster"] if r["kind"] == "document" else None))
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

    def room_for(self, nbytes: int) -> None:
        """Refuse new files that would leave the disk under KEEP_FREE free: the database lives on the same
        disk, and a full one stops everything. A video takes about twice its size once it's processed."""
        if not room(self.store.dir, nbytes):
            raise MediaError("The hub is nearly full. Ask its owner to make room, then try again.")

    def start_file(self, token: str, name: str, size: int, mime: str) -> dict:
        """One file of an upload. The same name and size again resumes where it stopped."""
        link = self.upload_link(token)
        if not isinstance(size, int) or size <= 0:
            raise MediaError("That file is empty.")
        if size > self.max_upload:
            raise MediaError(f"That file is over {self.max_upload // 2 ** 20} MB.")
        self.room_for(2 * size)
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

    # ------------------------------------------------------------ forgetting
    BLOBS = ("artifact", "playable", "poster", "sheet", "preview")

    def _in_use(self, c, aid: str) -> bool:
        if c.execute("SELECT 1 FROM artifacts WHERE id=?", (aid,)).fetchone():
            return True
        return any(c.execute(f"SELECT 1 FROM media WHERE {col}=? LIMIT 1", (aid,)).fetchone() for col in self.BLOBS)

    def drop_blobs(self, aids) -> int:
        """Delete stored files nothing refers to any more (the same bytes may be shared: files are stored once)."""
        with self.store.ro() as c:
            gone = [a for a in set(aids) if a and not self._in_use(c, a)]
        n = 0
        for a in gone:
            p = self.store.blob_path(a)
            if os.path.exists(p):
                os.remove(p)
                n += 1
        return n

    def erase(self, handles: list) -> int:
        """Everything these handles shared, gone: each media item keeps only a 'removed' stub (no name, no
        transcript, no pictures) so conversations still make sense, and its files are deleted."""
        if not handles:
            return 0
        q = ",".join("?" * len(handles))
        with self.store.tx() as c:
            rows = c.execute(f"SELECT * FROM media WHERE created_by IN ({q})", handles).fetchall()
            aids = [r[col] for r in rows for col in self.BLOBS]
            aids += [r["id"] for r in c.execute(f"SELECT id FROM artifacts WHERE created_by IN ({q})", handles)]
            c.execute(f"UPDATE media SET revoked=COALESCE(revoked, ?), name=NULL, source_url=NULL, digest='{{}}', error=NULL,"
                      f" artifact=NULL, playable=NULL, poster=NULL, sheet=NULL, preview=NULL, origin_artifact=NULL"
                      f" WHERE created_by IN ({q})", [self.now(), *handles])
            c.execute(f"DELETE FROM artifacts WHERE created_by IN ({q})", handles)
            links = [r["id"] for r in c.execute(f"SELECT id FROM upload_links WHERE handle IN ({q})", handles)]
            for lid in links:
                for f in c.execute("SELECT id FROM upload_files WHERE link=?", (lid,)).fetchall():
                    if os.path.exists(self._part(f["id"])):
                        os.remove(self._part(f["id"]))
                c.execute("DELETE FROM upload_files WHERE link=?", (lid,))
            c.execute(f"DELETE FROM upload_links WHERE handle IN ({q})", handles)
        return self.drop_blobs(aids)

    def tidy(self, older_than: float = 2 * 86400) -> int:
        """Housekeeping: half-finished uploads whose link has expired, and work files left behind by a
        restart. Never touches anything someone shared."""
        n, cutoff = 0, self.now() - older_than
        with self.store.tx() as c:
            stale = c.execute("SELECT f.id FROM upload_files f JOIN upload_links l ON l.id=f.link"
                              " WHERE f.media IS NULL AND l.expires<?", (cutoff,)).fetchall()
            for f in stale:
                c.execute("DELETE FROM upload_files WHERE id=?", (f["id"],))
            c.execute("DELETE FROM upload_links WHERE expires<? AND NOT EXISTS"
                      " (SELECT 1 FROM upload_files f WHERE f.link=upload_links.id)", (cutoff,))
        for f in stale:
            if os.path.exists(self._part(f["id"])):
                os.remove(self._part(f["id"]))
                n += 1
        for name in os.listdir(self.tmp) if os.path.isdir(self.tmp) else []:
            p = os.path.join(self.tmp, name)
            if (name.startswith(("work_", "look_")) or name.endswith(".part")) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True) if os.path.isdir(p) else os.remove(p)
                n += 1
        return n

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
                    if self.transcriber:          # nothing left to listen to: give the speech model's memory back
                        self.transcriber.release()
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
            self.room_for(2 * min(self.max_upload, captions.MAX_DOWNLOAD * 4))
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
            raw = f.read() if os.path.getsize(src) <= 200 * 2 ** 20 else f.read(8)
        png = raw.startswith(b"\x89PNG")
        clean = os.path.join(work, "clean.png" if png else "clean.jpg")
        exact = (strip_png(raw) if png else strip_jpeg(raw)) if len(raw) > 8 else None
        if exact is not None:                   # pixels untouched: only the metadata blocks are gone
            with open(clean, "wb") as f:
                f.write(exact)
        else:
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
        d = {"width": w, "height": h, "stripped": True, "exact_pixels": exact is not None}
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
        self._set(mid, stage="reading")
        doc = read_document(src, self.row(mid)["name"] or "", work)
        if not doc:
            self._set(mid, status="ready", stage=None, digest=json.dumps({"note": "kept as a download"}))
            return
        text = doc["text"][:DOC_TEXT_MAX]
        d = {"doc": doc["doc"], "pages": doc.get("pages"), "words": len(text.split()), "chars": len(text),
             "excerpt": excerpt(text), "text_cut": len(doc["text"]) > DOC_TEXT_MAX}
        if text.strip():
            path = os.path.join(work, "text.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            d["text"] = self._keep(path, "text.txt", by, artifact, "text", "text/plain; charset=utf-8")
        # no _typed() here: a document is only ever served as a download (an HTML file must never render)
        poster = doc.get("poster")
        self._set(mid, status="ready", stage=None, kind="document", mime=doc["mime"], digest=json.dumps(d),
                  poster=self._keep(poster, "page1.jpg", by, artifact, "poster", "image/jpeg") if poster else None)

    def page_image(self, mid: str, n: int) -> Optional[str]:
        """Page n of a PDF as a picture for its viewer page, drawn once and kept beside the hub's other temp files."""
        r = self.row(mid)
        d = json.loads(r["digest"] or "{}")
        if r["kind"] != "document" or d.get("doc") != "pdf" or not (1 <= n <= min(d.get("pages") or 0, VIEW_PAGES_MAX)):
            return None
        src = self.blob(r["artifact"])
        if not src:
            return None
        folder = os.path.join(self.tmp, "pages", r["artifact"])
        out = os.path.join(folder, f"{n}.jpg")
        if not os.path.exists(out):
            os.makedirs(folder, exist_ok=True)
            stem = os.path.join(folder, f"{n}-{uuid.uuid4().hex[:8]}")
            try:
                _run(["pdftoppm", "-q", "-jpeg", "-jpegopt", "quality=82", "-f", str(n), "-l", str(n), "-scale-to", "1400",
                      "-singlefile", src, stem], 60)
                os.replace(stem + ".jpg", out)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                return None
        return out

    def as_pdf(self, mid: str) -> Optional[str]:
        """A text document (Markdown, text, Word, slides, CSV) as a PDF, made once on request and kept."""
        r = self.row(mid)
        d = json.loads(r["digest"] or "{}")
        if r["kind"] != "document" or d.get("doc") == "pdf" or not d.get("text"):
            return None
        folder = os.path.join(self.tmp, "pages", r["artifact"])
        out = os.path.join(folder, "as.pdf")
        if not os.path.exists(out):
            os.makedirs(folder, exist_ok=True)
            data = text_to_pdf(r["name"] or "document.txt", self.doc_text(mid))
            tmp = out + "." + uuid.uuid4().hex[:8]
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, out)
        return out

    def doc_text(self, mid: str) -> str:
        """A document's text as read, or ''."""
        p = self.blob((self.get(mid)["digest"] or {}).get("text"))
        return open(p, encoding="utf-8", errors="replace").read() if p else ""


# --------------------------------------------------------------------------- pages and routes
PAGE_CSS = """
:root{--rh:#18171d;--rm:#f4f3f6;--bg:#f6f4ef;--card:#fff;--ink:#18171d;--ink2:#6b6972;--ink3:#c4c1c9;--ghost:#a19ea8;--fill:rgba(24,23,29,.05);--accent:#4f44e0;
 --accent-hover:#4338d0;--on-accent:#fff;--accent-soft:#e6e4fb;--glow:rgba(79,68,224,.16);--red:#c42838;--green:#22a45d;--seal:#22a45d;--grain:.07;--white-half:#fff;--edge:rgba(24,23,29,.08);
 --ease:cubic-bezier(.22,.8,.24,1);--spring:cubic-bezier(.34,1.5,.55,1);--glide:cubic-bezier(.32,.72,0,1);
 --font:-apple-system,BlinkMacSystemFont,"SF Pro Text","Inter","Segoe UI",system-ui,sans-serif;
 --display:-apple-system,BlinkMacSystemFont,"SF Pro Display","Inter","Segoe UI",system-ui,sans-serif;color-scheme:light dark}
@media (prefers-color-scheme:dark){:root{--rh:#f4f3f6;--rm:#18171d;--bg:#111014;--card:#201f26;--ink:#f4f3f6;--ink2:#a3a1ab;--ink3:#45434c;--ghost:#6c6975;--fill:rgba(255,255,255,.07);
 --accent:#9a93ff;--accent-hover:#aba5ff;--on-accent:#0e0d12;--accent-soft:#2b2944;--glow:rgba(154,147,255,.14);--red:#ff7076;--green:#46d58b;--grain:.05;--white-half:#f4f3f6;--edge:rgba(255,255,255,.16)}}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;background:var(--bg);color:var(--ink);min-height:100%}
body{font:16px/1.45 var(--font);-webkit-font-smoothing:antialiased;letter-spacing:-.012em;overflow-x:hidden}
.grain{position:fixed;inset:0;width:100%;height:100%;pointer-events:none;opacity:var(--grain);z-index:0}
.glow{position:fixed;left:50%;top:-140px;width:720px;height:560px;margin-left:-360px;pointer-events:none;
 background:radial-gradient(closest-side,var(--glow),transparent);z-index:0}
main{position:relative;z-index:1;min-height:100vh;display:flex;justify-content:center;
 padding:max(40px,env(safe-area-inset-top)) max(20px,env(safe-area-inset-right)) 44px max(20px,env(safe-area-inset-left))}
@keyframes rise{from{opacity:0;transform:translateY(12px)}to{opacity:1;transform:none}}
@keyframes seal{0%{opacity:0;transform:scale(.2)}65%{opacity:1;transform:scale(1.12)}100%{opacity:1;transform:none}}
.shell{width:100%;max-width:400px;min-width:0;display:flex;flex-direction:column;align-items:center;text-align:center;animation:rise .65s var(--ease) .1s both}
.shell.wide{max-width:760px;align-items:stretch;text-align:left}
.title{font:700 30px/1.08 var(--display);letter-spacing:-.04em;margin:0;text-wrap:balance;overflow-wrap:anywhere}
.wide .title{font-size:26px;letter-spacing:-.035em}
.lede{color:var(--ink2);margin:10px 0 0;text-wrap:pretty}
.meta{color:var(--ink2);font-size:14px;margin:8px 0 0;overflow-wrap:anywhere}
.foot{color:var(--ink2);font-size:12.5px;line-height:1.5;margin:34px 0 0;opacity:.8;text-wrap:pretty}
/* Relay's mark: two halves that make a whole */
.brand{display:flex;align-items:center;gap:9px;margin:0 0 22px;color:var(--ink2);font:600 13.5px var(--font);letter-spacing:-.01em}
.mini{display:flex;width:22px;height:22px;gap:1.5px}.mini i{flex:1}
.mini i:first-child{border-radius:99px 0 0 99px;background:var(--white-half);box-shadow:0 0 0 .5px rgba(24,23,29,.14),0 3px 6px -3px rgba(24,23,29,.35)}
.mini i:last-child{border-radius:0 99px 99px 0;background:var(--rh);display:grid;place-items:center;box-shadow:0 0 0 .5px var(--edge)}
.mini i:last-child:after{content:"";width:4px;height:8px;border-radius:0 8px 8px 0;background:var(--rm);margin-right:4px}
.orb{position:relative;width:92px;height:92px;display:flex;gap:6px;margin:0 0 26px;transition:gap .3s var(--glide)}
.half{flex:1;display:grid;place-items:center;overflow:hidden;position:relative}
.half.l{border-radius:999px 0 0 999px;border:1.5px dashed var(--ghost);border-right-style:solid;border-right-color:transparent;
 transition:background-color .3s var(--ease),border-color .3s var(--ease),box-shadow .3s var(--ease)}
.half.r{border-radius:0 999px 999px 0;background:var(--rh);box-shadow:0 1px 0 rgba(255,255,255,.08) inset,0 14px 30px -14px rgba(24,23,29,.5),0 0 0 .5px var(--edge)}
.half.r .mk{display:block;width:18px;height:36px;border-radius:0 36px 36px 0;background:var(--rm);margin-right:18px;transition:opacity .2s var(--ease)}
.seal{position:absolute;left:50%;top:50%;width:46px;height:46px;margin:-23px 0 0 -23px;border-radius:50%;background:var(--seal);color:#fff;
 display:grid;place-items:center;opacity:0;box-shadow:0 0 0 5px var(--bg),0 12px 26px -10px rgba(34,164,93,.6)}
.seal svg{width:24px;height:24px}
.orb.sealed{gap:0}.orb.sealed .half.l{background:var(--white-half);border-color:transparent;
 box-shadow:0 1px 0 rgba(255,255,255,.6) inset,0 14px 30px -14px rgba(24,23,29,.35),0 0 0 .5px var(--edge)}
.orb.sealed .mk{opacity:0}.orb.sealed .seal{animation:seal .45s var(--spring) .15s both}
.note{font:500 17px/1.4 var(--display);letter-spacing:-.02em;color:var(--ink);margin:22px 0 0;overflow-wrap:anywhere;text-wrap:pretty}
/* buttons: pills */
.btn{appearance:none;display:flex;align-items:center;justify-content:center;gap:10px;width:100%;height:48px;padding:0 26px;border:0;border-radius:999px;
 background:var(--accent);color:var(--on-accent);font:600 16px var(--font);letter-spacing:-.01em;cursor:pointer;text-decoration:none;
 box-shadow:0 1px 0 rgba(255,255,255,.22) inset,0 12px 26px -12px var(--accent);transition:transform .45s var(--spring),background-color .2s,opacity .2s}
.btn:hover{background:var(--accent-hover)}.btn:active{transform:scale(.96);transition:transform .12s var(--ease)}
.btn svg{width:20px;height:20px;flex:none}
.vh{position:absolute;width:1px;height:1px;opacity:0;overflow:hidden;clip-path:inset(50%)}
.drop{width:100%;margin-top:30px;display:flex;flex-direction:column;align-items:center}
.drop .btn:has(input:disabled){opacity:.5;pointer-events:none}.drop .btn:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:3px}
.hint{color:var(--ink2);font-size:13.5px;margin:12px 0 0}
/* each file: name, a thin pill of progress, then its status */
.files{width:100%;text-align:left}
.file{margin-top:22px;animation:rise .45s var(--ease) both}
.file>div:first-child{font-weight:600;font-size:15px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.bar{height:6px;border-radius:999px;background:var(--fill);overflow:hidden;margin-top:9px}
.bar>i{display:block;height:100%;width:0;border-radius:999px;background:var(--accent);transition:width .2s,background-color .3s}
.file.ok .bar>i{background:var(--green)}
.small{color:var(--ink2);font-size:13px;margin-top:6px;overflow-wrap:anywhere}.file.bad .small{color:var(--red)}
/* the viewer */
.stage{margin-top:22px}
video,img.full{display:block;width:100%;max-height:72vh;border-radius:20px;box-shadow:0 26px 50px -30px rgba(24,23,29,.55)}
video{background:#000}img.full{object-fit:contain;background:var(--fill)}
audio{display:block;width:100%}
.wait{color:var(--ink2);margin:22px 0 0}
.actions{margin-top:18px}.actions .btn{width:auto;display:inline-flex;height:48px;font-size:16px}
.sect{margin-top:40px}
.sect h2{font:700 20px/1.2 var(--display);letter-spacing:-.03em;margin:0 0 12px}
.t{display:flex;gap:12px;align-items:baseline;padding:5px 0}
.t span{min-width:0;overflow-wrap:anywhere}
.t button{flex:none;appearance:none;border:0;border-radius:999px;background:var(--accent-soft);color:var(--accent);cursor:pointer;
 font:600 13px/1 var(--font);font-variant-numeric:tabular-nums;padding:6px 10px;min-width:4.4em;text-align:center;transition:transform .3s var(--spring)}
.t button:active{transform:scale(.94)}
.sheet{display:block;width:100%;border-radius:14px}
.meta a.prev{color:var(--accent);font-weight:600;text-decoration:none}
.wide .title.long{font-size:22px;letter-spacing:-.025em;line-height:1.15}
.btn.ghost{background:var(--fill);color:var(--ink);box-shadow:none}.btn.ghost:hover{background:var(--edge)}
.btn.done{background:var(--green);color:#fff}
.actions.two,.actions.three{display:flex;gap:10px;flex-wrap:wrap}
/* a document, read on the page itself: no box to scroll inside a page that scrolls */
.doc{margin-top:26px;font:17px/1.62 var(--font);letter-spacing:-.008em;overflow-wrap:break-word;text-wrap:pretty}
.doc p{margin:0 0 .95em}.doc h2,.doc h3,.doc h4{font-family:var(--display);letter-spacing:-.025em;line-height:1.2;margin:1.5em 0 .5em}
.doc h2{font-size:24px}.doc h2.sub{font-size:19px;margin-top:1.2em}.doc h3{font-size:20px}.doc h4{font-size:17px}.doc>:first-child{margin-top:0}
.doc ul,.doc ol{margin:0 0 .95em;padding-left:1.3em}.doc li{margin:.25em 0}
.doc blockquote{margin:0 0 .95em;padding:2px 0 2px 14px;border-left:3px solid var(--edge);color:var(--ink2)}
.doc code{font:14.5px ui-monospace,SFMono-Regular,Menlo,monospace;background:var(--fill);border-radius:5px;padding:1px 5px}
.doc pre{font:13.5px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;overflow-wrap:anywhere;background:var(--card);
 border-radius:14px;padding:14px 16px;margin:0 0 .95em;box-shadow:0 0 0 1px var(--edge)}
.doc a{color:var(--accent)}.doc hr{border:0;border-top:1px solid var(--edge);margin:1.6em 0}
.doc .slide{background:var(--card);border-radius:16px;padding:16px 18px 6px;margin:0 0 12px;box-shadow:0 0 0 1px var(--edge)}
.doc .slide h2{margin:0 0 .4em;font-size:13px;letter-spacing:.02em;text-transform:uppercase;color:var(--ink2)}
.tablewrap{overflow-x:auto;border-radius:14px;box-shadow:0 0 0 1px var(--edge);background:var(--card)}
.tablewrap table{border-collapse:collapse;font:14.5px/1.4 var(--font);font-variant-numeric:tabular-nums;min-width:100%}
.tablewrap th,.tablewrap td{padding:9px 14px;text-align:left;white-space:nowrap;border-bottom:1px solid var(--edge)}
.tablewrap th{font-weight:650;background:var(--fill);position:sticky;top:0}.tablewrap tr:last-child td{border-bottom:0}
.tablewrap .num{text-align:right}
.doc table{border-collapse:collapse;width:100%;margin:0 0 .95em;font-size:15px;display:block;overflow-x:auto}
.doc th,.doc td{padding:8px 12px;text-align:left;border-bottom:1px solid var(--edge)}.doc th{background:var(--fill);font-weight:650}
/* a PDF: its own pages, one under the other */
.pages{margin-top:26px;display:grid;gap:14px}
.pdfpage{display:block;width:100%;height:auto;aspect-ratio:8.5/11;object-fit:contain;background:#fff;border-radius:6px;
 box-shadow:0 0 0 .5px var(--edge),0 18px 36px -26px rgba(24,23,29,.55)}
@media (max-width:520px){.actions .btn{display:flex;width:100%}.actions.two .btn,.actions.three .btn{flex:1 1 0;width:auto;padding:0 14px}.actions.three .btn:first-child{flex-basis:100%}.wide .title{font-size:24px}.wide .title.long{font-size:20px}.doc{font-size:16.5px}}
@media (prefers-reduced-motion:reduce){*,*:before,*:after{animation-duration:.01ms!important;animation-delay:0s!important;transition-duration:.01ms!important}}
:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
"""

# the paper texture, drawn inline: the pages' policy allows images only from the hub itself
GRAIN_SVG = ("<svg class=grain aria-hidden=true xmlns='http://www.w3.org/2000/svg'><filter id=grain>"
             "<feTurbulence type=fractalNoise baseFrequency='.85' numOctaves=3 stitchTiles=stitch /><feColorMatrix "
             "values='0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 .55 0'/></filter><rect width='100%' height='100%' filter='url(#grain)'/></svg>")
BRAND = "<div class=brand aria-hidden=true><span class=mini><i></i><i></i></span>Relay</div>"
TICK = ("<svg viewBox='0 0 30 30' fill=none><path d='M8 15.5l4.6 4.6L22.5 10' stroke=currentColor stroke-width=3 "
        "stroke-linecap=round stroke-linejoin=round /></svg>")
DOWN = ("<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
        "<path d='M12 4v11M7 10.5l5 5 5-5M5 19.5h14'/></svg>")

PDF_ICON = ("<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
            "<path d='M7 3h7l5 5v13H7zM14 3v5h5'/><path d='M10 14.5h4M10 17.5h2.5'/></svg>")
COPY = ("<svg viewBox='0 0 24 24' fill=none stroke=currentColor stroke-width=2 stroke-linecap=round stroke-linejoin=round aria-hidden=true>"
        "<rect x='8.5' y='8.5' width='11' height='11' rx='2.5'/><path d='M15.5 8.5V6.5a2 2 0 0 0-2-2h-7a2 2 0 0 0-2 2v7a2 2 0 0 0 2 2h2'/></svg>")

UPLOAD_JS = r"""
const box=document.getElementById('files'),pick=document.getElementById('pick'),drop=document.getElementById('drop'),orb=document.getElementById('orb');
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
  msg.textContent='Sent ✓ Your agent has it now.';d.classList.add('ok');return true}catch(e){msg.textContent='Not sent: '+e.message;d.classList.add('bad');return false}}
async function all(list){if(!list.length)return;pick.disabled=true;orb.classList.remove('sealed');let good=true;
 for(const f of list)good=(await send(f))&&good;pick.disabled=false;if(good)orb.classList.add('sealed')}   // both halves: the files are with the agent
pick.addEventListener('change',()=>all([...pick.files]));
drop.addEventListener('dragover',e=>{e.preventDefault()});drop.addEventListener('drop',e=>{e.preventDefault();all([...e.dataTransfer.files])});
"""

VIEW_JS = r"""
const v=document.querySelector('video,audio');document.querySelectorAll('[data-t]').forEach(b=>b.addEventListener('click',()=>{if(v){v.currentTime=+b.dataset.t;v.play()}}));
const cb=document.getElementById('copytext'),dt=document.getElementById('doctext');
if(cb&&dt){const label=cb.innerHTML;cb.addEventListener('click',async()=>{const t=JSON.parse(dt.textContent);
try{await navigator.clipboard.writeText(t)}catch(e){const a=document.createElement('textarea');a.value=t;a.style.position='fixed';a.style.opacity='0';document.body.append(a);a.select();try{document.execCommand('copy')}catch(_){}a.remove()}
cb.textContent='Copied';cb.classList.add('done');setTimeout(()=>{cb.innerHTML=label;cb.classList.remove('done')},1600)})}
document.querySelectorAll('img.pdfpage').forEach(i=>i.addEventListener('load',()=>{i.style.aspectRatio=i.naturalWidth+'/'+i.naturalHeight}));
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
            f"<meta name=robots content=noindex><link rel=icon href='/icon.svg' type='image/svg+xml'>"
            f"<link rel=apple-touch-icon href='/apple-touch-icon.png'><title>{html.escape(title)}</title><style>{PAGE_CSS}</style></head>"
            f"<body>{GRAIN_SVG}<div class=glow aria-hidden=true></div><main>{body}</main></body></html>")


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
        return HTMLResponse(page("Link expired", f"<div class=shell>{BRAND}<h1 class=title>{html.escape(msg)}</h1></div>", ""),
                            status_code=410, headers=headers())

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
        if kind == "document":
            ext = os.path.splitext(m["name"] or "")[1].lower()
            words = (table_size(media.doc_text(r["id"]), "\t" if ext == ".tsv" else ",") if ext in (".csv", ".tsv")
                     else f"{d.get('words', 0):,} words" if d.get("words") else None)
            pages = (f"{d['pages']} {'slides' if d.get('doc') == 'slides' else 'pages' if d['pages'] != 1 else 'page'}"
                     if d.get("pages") else None)
            bits = [x for x in (pages, words, *bits) if x]
        if m.get("version", 1) > 1:
            bits.insert(0, f"Version {m['version']}")
        long = " long" if len(m["name"] or "") > 26 else ""
        out = [f"<div class='shell wide'>{BRAND}<h1 class='title{long}'>{title_html(m['name'] or kind or 'Media')}</h1>"
               f"<p class=meta>{html.escape(' · '.join(bits))}</p>"]
        if m["status"] != "ready":
            out.append("<p class=wait>" + ("Still being processed. Refresh in a moment." if m["status"] in ("queued", "working")
                                           else html.escape("Couldn't be processed: " + (m["error"] or ""))) + "</p>")
        elif kind == "video" and m["has"]["playable"]:
            poster = f" poster='{base}/poster'" if m["has"]["poster"] else ""
            out.append(f"<div class=stage><video controls playsinline preload=metadata src='{base}/file'{poster}></video></div>")
        elif kind == "audio":
            out.append(f"<div class=stage><audio controls preload=metadata src='{base}/file'></audio></div>")
        elif kind == "image":
            out.append(f"<div class=stage><img class=full src='{base}/file' alt='{name}'></div>")
        doc_text = media.doc_text(r["id"]) if kind == "document" and m["status"] == "ready" and d.get("text") else ""
        if m["status"] == "ready":
            copy = (f"<button class='btn ghost' type=button id=copytext>{COPY}Copy text</button>"
                    if doc_text and len(doc_text) <= 200_000 else "")
            aspdf = (f"<a class='btn ghost' href='{base}/pdf'>{PDF_ICON}Save as PDF</a>"
                     if doc_text and d.get("doc") != "pdf" else "")
            n = 1 + bool(copy) + bool(aspdf)
            out.append(f"<p class='actions{' two' if n == 2 else ' three' if n == 3 else ''}'>"
                       f"<a class=btn href='{base}/original'>{DOWN}Download</a>{aspdf}{copy}</p>")
            if m.get("version", 1) > 1 and m.get("prev"):
                try:
                    earlier = media.link(m["prev"])[len(media.public_url or ""):]
                    out.append(f"<p class=meta><a class=prev href='{earlier}'>See version {m['version'] - 1}</a></p>")
                except Exception:
                    pass
        if kind == "document" and m["status"] == "ready":
            if d.get("doc") == "pdf" and d.get("pages"):
                shown = min(d["pages"], VIEW_PAGES_MAX)
                out.append("<div class=pages>" + "".join(
                    f"<img class=pdfpage loading=lazy src='{base}/page/{i}' alt='Page {i} of {shown}'>" for i in range(1, shown + 1)) + "</div>")
                if d["pages"] > shown:
                    out.append(f"<p class=meta>The first {shown} pages. Download for all {d['pages']}.</p>")
            elif doc_text:
                cut = len(doc_text) > VIEW_TEXT_MAX
                out.append("<article class=doc>" + document_html(d.get("doc"), m["name"] or "", doc_text[:VIEW_TEXT_MAX]) + "</article>")
                if cut:
                    out.append("<p class=meta>That's the first part. Download for all of it.</p>")
            elif not d.get("text"):
                out.append("<p class=meta>No text inside (a scan or pictures only). Download to see it.</p>")
            if doc_text and len(doc_text) <= 200_000:      # what Copy text copies: data, never run
                out.append("<script type=application/json id=doctext>" + json.dumps(doc_text).replace("</", "<\\/") + "</script>")
        if d.get("transcript"):
            rows = "".join(f"<div class=t><button data-t='{x['t']}'>{clock(x['t'])}</button><span>{html.escape(x['text'])}</span></div>"
                           for x in d["transcript"])
            out.append(f"<section class=sect><h2>Transcript</h2>{rows}</section>")
        if m["has"]["sheet"]:
            out.append(f"<section class=sect><h2>Shots</h2><img class=sheet src='{base}/sheet' alt='contact sheet'></section>")
        out.append("</div>")
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
        inline = (r["kind"] in ("video", "audio", "image") or which in ("poster", "sheet")) \
            and mime.split("/")[0] in ("video", "audio", "image") and mime != "image/svg+xml" and not download
        name = r["name"] or (a["name"] if a else "file")
        if which == "file" and r["playable"] and r["playable"] != r["artifact"]:
            name = os.path.splitext(name)[0] + os.path.splitext((a["name"] if a else "") or ".mp4")[1]
        return FileResponse(path, media_type=mime if inline else "application/octet-stream", filename=name,
                            content_disposition_type="inline" if inline else "attachment",
                            headers={**headers(), "Cache-Control": "private, max-age=3600"})

    @app.get("/m/{token}/pdf", include_in_schema=False)
    def view_as_pdf(token: str):
        r = opened(token)
        try:
            path = media.as_pdf(r["id"]) if r["status"] == "ready" else None
        except MediaError as e:
            return HTMLResponse(page("Couldn't make a PDF", f"<div class=shell>{BRAND}<h1 class=title>{html.escape(str(e))}</h1></div>", ""),
                                status_code=422, headers=headers())
        if not path:
            raise HTTPException(404, "not found")
        name = os.path.splitext(r["name"] or "document")[0] + ".pdf"
        return FileResponse(path, media_type="application/pdf", filename=name, content_disposition_type="attachment",
                            headers={**headers(), "Cache-Control": "private, max-age=3600"})

    @app.get("/m/{token}/page/{n}", include_in_schema=False)
    def view_page(token: str, n: int):
        r = opened(token)
        path = media.page_image(r["id"], n) if r["status"] == "ready" else None
        if not path:
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="image/jpeg", headers={**headers(), "Cache-Control": "private, max-age=3600"})

    @app.get("/m/{token}/file", include_in_schema=False)
    def view_file(token: str, download: int = 0):
        return serve(token, "file", bool(download))

    @app.get("/m/{token}/original", include_in_schema=False)
    @app.get("/m/{token}/original/{filename}", include_in_schema=False)
    def view_original(token: str, filename: Optional[str] = None):
        """The exact file as it was shared (a photo: the copy without location data), always as a download.
        Videos and other files are available at once; a photo only once its location data is gone.
        Pictures, video and audio carry their real type, so a posting service that fetches the link
        (Instagram, TikTok and others take a public URL) accepts it; the name on the end is cosmetic,
        for services that want a URL ending in .mp4. Anything else stays a plain download."""
        r = opened(token)
        if r["status"] not in ("ready", "failed") and (r["kind"] in (None, "image")):
            raise HTTPException(409, "Still being prepared. Try again in a minute.")
        path = media.blob(r["artifact"])
        if not path:
            raise HTTPException(404, "not found")
        with media.store.ro() as c:
            a = c.execute("SELECT mime FROM artifacts WHERE id=?", (r["artifact"],)).fetchone()
        mime = (a and a["mime"]) or r["mime"] or ""
        if mime not in EXACT_TYPES and r["kind"] in ("video", "audio", "image"):
            mime = mimetypes.guess_type(r["name"] or "")[0] or mime      # e.g. a phone's .mov: video/quicktime
        return FileResponse(path, media_type=mime if mime in EXACT_TYPES else "application/octet-stream",
                            filename=r["name"] or "file", content_disposition_type="attachment",
                            headers={**headers(), "Cache-Control": "private, max-age=3600",
                                     "Digest": "sha-256=" + base64.b64encode(bytes.fromhex(r["artifact"])).decode()})

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
        note = f"<p class=note>“{html.escape(link['note'])}”</p>" if link["note"] else ""
        body = (f"<div class=shell><div class=orb id=orb aria-hidden=true><span class='half l'></span>"
                f"<span class='half r'><i class=mk></i></span><span class=seal>{TICK}</span></div>"
                f"<h1 class=title>Send files to {html.escape(link['handle'])}</h1>"
                f"<p class=lede>Photos, videos, audio or any file, up to {media.max_upload // 2 ** 20}&nbsp;MB each.</p>{note}"
                f"<section id=drop class=drop><label class=btn>Choose files<input id=pick class=vh type=file multiple></label>"
                f"<p class=hint>or drop them here</p></section><div id=files class=files aria-live=polite></div>"
                f"<p class=foot>Works until {time.strftime('%b %-d', time.gmtime(link['expires']))}"
                f" · location removed from photos</p></div>"
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
