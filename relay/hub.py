#!/usr/bin/env python3
"""Relay hub.

Agents on different providers (Claude, Codex, Muse, anything that can make an HTTP
request) talk through one hub. Three things live here:

  * messages  - channels, flat threads, mentions, a cursor-acknowledged inbox.
                Model borrowed from opencrawl-core's board (Apache-2.0): flat threads
                keyed by root id, idempotent posts, mentions resolved at post time,
                at-least-once delivery, client polling.
  * jobs      - work addressed to a CAPABILITY ("fetch", "digest", "clip"), not to a named
                agent. Any node that declares the capability can claim it. Claims are
                leases: a node that goes to sleep mid-job loses it back to the queue.
  * artifacts - large media moved by reference. Bytes stream straight to and from
                disk, content-addressed by sha256. Messages carry ids, never bytes.

One process. SQLite for state, the filesystem for blobs.

    RELAY_ADMIN_TOKEN=... uvicorn relay.hub:app --host 0.0.0.0 --port 8787
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import ipaddress
import html
import io
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
import zipfile
from contextlib import asynccontextmanager, contextmanager
from typing import Any, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from . import media as media_mod

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  handle TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
  caps TEXT NOT NULL DEFAULT '[]', role TEXT NOT NULL DEFAULT 'agent',
  created REAL, last_seen REAL);
CREATE TABLE IF NOT EXISTS messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, sender TEXT NOT NULL, idem TEXT,
  channel TEXT NOT NULL, thread INTEGER, reply_to INTEGER, time REAL NOT NULL,
  text TEXT NOT NULL, mentions TEXT NOT NULL DEFAULT '[]',
  artifacts TEXT NOT NULL DEFAULT '[]', job TEXT, UNIQUE(sender, idem));
CREATE INDEX IF NOT EXISTS messages_thread ON messages(thread);
-- one row per conversation, kept as messages arrive: its newest message and how many it has. The list of
-- conversations reads this instead of scanning every message ever sent.
CREATE TABLE IF NOT EXISTS convs(thread INTEGER PRIMARY KEY, last INTEGER NOT NULL, n INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS convs_last ON convs(last);
-- everyone who has been in a conversation (wrote in it or was named in it): who may see it, without
-- reading the mentions of every message on every request
CREATE TABLE IF NOT EXISTS members(thread INTEGER NOT NULL, agent TEXT NOT NULL, PRIMARY KEY(thread, agent));
CREATE INDEX IF NOT EXISTS members_agent ON members(agent, thread);
-- when an agent first read a message (its chat took it): what "Read by claude · 3:29" says
CREATE TABLE IF NOT EXISTS reads(msg INTEGER NOT NULL, handle TEXT NOT NULL, time REAL NOT NULL, PRIMARY KEY(msg, handle));
-- the newest message a person has seen in each conversation: what makes a row bold until they open it
-- a person's saved hand-offs: one tap sends the same request to the same agents again
CREATE TABLE IF NOT EXISTS shortcuts(id TEXT PRIMARY KEY, handle TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
  mentions TEXT NOT NULL, created REAL NOT NULL, used REAL, uses INTEGER NOT NULL DEFAULT 0);
-- a person's small settings (the daily email on or off, when it last went)
CREATE TABLE IF NOT EXISTS prefs(handle TEXT NOT NULL, key TEXT NOT NULL, value TEXT, PRIMARY KEY(handle, key));
CREATE TABLE IF NOT EXISTS seen(handle TEXT NOT NULL, thread INTEGER NOT NULL, last INTEGER NOT NULL, PRIMARY KEY(handle, thread));
-- a person's pinned conversations (kept on top) and muted ones (no notifications, not counted as new)
CREATE TABLE IF NOT EXISTS marks(handle TEXT NOT NULL, thread INTEGER NOT NULL, pinned REAL, muted INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(handle, thread));
CREATE INDEX IF NOT EXISTS messages_channel ON messages(channel);
CREATE TABLE IF NOT EXISTS subs(agent TEXT, channel TEXT, PRIMARY KEY(agent, channel));
CREATE TABLE IF NOT EXISTS follows(agent TEXT, thread INTEGER, PRIMARY KEY(agent, thread));
CREATE TABLE IF NOT EXISTS cursors(agent TEXT PRIMARY KEY, last INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, params TEXT NOT NULL,
  requested_by TEXT NOT NULL, status TEXT NOT NULL, claimed_by TEXT,
  lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0, result TEXT,
  artifacts TEXT NOT NULL DEFAULT '[]', error TEXT, thread INTEGER,
  requires_approval INTEGER NOT NULL DEFAULT 0, approved_by TEXT,
  parent_job TEXT, not_before REAL, created REAL, updated REAL);
CREATE INDEX IF NOT EXISTS jobs_queue ON jobs(status, kind, created);
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, name TEXT, mime TEXT, size INTEGER, created_by TEXT,
  created REAL, meta TEXT NOT NULL DEFAULT '{}', parent TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS invites(
  id TEXT PRIMARY KEY, code_hash TEXT UNIQUE NOT NULL, handle TEXT NOT NULL,
  role TEXT NOT NULL, caps TEXT NOT NULL DEFAULT '[]', note TEXT,
  created REAL NOT NULL, expires REAL NOT NULL, claimed REAL, revoked REAL);
CREATE TABLE IF NOT EXISTS people_emails(
  email TEXT PRIMARY KEY, handle TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS sessions(
  token_hash TEXT PRIMARY KEY, handle TEXT NOT NULL, created REAL, expires REAL NOT NULL, last_used REAL);
CREATE TABLE IF NOT EXISTS contacts(
  owner TEXT NOT NULL, name TEXT NOT NULL, address TEXT NOT NULL, created REAL,
  PRIMARY KEY(owner, name));
CREATE TABLE IF NOT EXISTS agent_keys(
  token_hash TEXT PRIMARY KEY, handle TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS routes(
  msg INTEGER NOT NULL, handle TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(msg, handle));
CREATE TABLE IF NOT EXISTS thread_sessions(
  thread INTEGER NOT NULL, handle TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(thread, handle));
CREATE TABLE IF NOT EXISTS session_cursors(
  handle TEXT NOT NULL, session TEXT NOT NULL, last INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(handle, session));
CREATE TABLE IF NOT EXISTS delivered(
  msg INTEGER NOT NULL, handle TEXT NOT NULL, session TEXT NOT NULL, PRIMARY KEY(msg, handle, session));
-- routes and deliveries are looked up by who (and which chat) far more than by message: without these, every
-- check grows with every message ever delivered
CREATE INDEX IF NOT EXISTS routes_handle ON routes(handle, session);
CREATE INDEX IF NOT EXISTS delivered_handle ON delivered(handle, session);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
# Apps that send no id at all (Anthropic's hosted connector, used by claude.ai and Claude Desktop) are
# known by the conversation instead: an `ask` from such a chat gets the session "thread:<id>", and the
# chat's model, which keeps that id in its own context, opens the answers with check_replies(thread).
#
# Routing by session. One identity (say `claude`) is used from many chats at once, so a message is
# also routed to the exact chat that should handle it:
#   messages.origin_session  the chat that sent it (never shown to anyone; only used for routing)
#   routes(msg, handle)      which session of `handle` owns this message. No row: nobody yet, and
#                            the first session of that identity to read it claims it. '' marks a
#                            claim by an old client that sends no session id.
#   thread_sessions          the session of an identity that is taking part in a thread, so a later
#                            message in the thread reaches that chat and not a fresh one.
#   session_cursors          each session's own read position (it starts at the identity's cursor),
#                            when it last called (`seen`) and whether its app said it ended (`ended`).
#   delivered                messages a chat already got by asking for a whole thread, so they are
#                            not handed to it a second time.
#   meta.session_floor       the newest message when session routing arrived: older messages that no
#                            chat took are not handed to new chats (identity-wide readers still get them).

log = logging.getLogger("relay")

HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
PROVIDER_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")   # "claude", "openai", "gemini", ... (display only)
CONVERSATION = "thread:"   # session of a chat whose app sends no id: it is known by the conversation it started
SESSION_RE = re.compile(r"^[\x21-\x7e]{1,200}$")     # visible ASCII, as MCP session ids are
RESERVED = {"relay", "admin", "me"}
SYSTEM = "relay"            # sender name for messages the hub writes itself
DIRECT = "direct"           # the channel conversations live in: private to the people in them
MEDIA_PREFIXES = ("video/", "audio/")
CLIP_ASPECTS = ("9:16",)          # the shapes a clip node will cut


def private_host(host: str) -> bool:
    """True for hosts a fetch must never be pointed at: loopback, private ranges,
    link-local (including cloud metadata at 169.254.169.254), and bare local names.
    Literal checks only - the node re-checks after DNS resolution."""
    h = (host or "").strip("[]").lower()
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return "." not in h            # single-label names resolve on the local network
    return not ip.is_global


DISK_LOW = 0.05             # /health says "not ok" under this share of the disk free


def client_ip(request) -> str:
    """Who is calling, for rate limits. Behind a proxy (Railway) the last X-Forwarded-For entry is the
    one the proxy added; earlier entries are whatever the caller wrote, so they are never trusted."""
    fwd = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    return fwd[-1] if fwd else (request.client.host if request.client else "")


def _now() -> float:
    return time.time()


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _loads(v, default):
    return json.loads(v) if v else default


# --------------------------------------------------------------------------- store
class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        self.blobs = os.path.join(data_dir, "blobs")
        self.tmp = os.path.join(data_dir, "tmp")
        os.makedirs(self.blobs, exist_ok=True)
        os.makedirs(self.tmp, exist_ok=True)
        self.db_path = os.path.join(data_dir, "relay.db")
        self.changed = threading.Condition()   # notified after every write: a waiting inbox wakes at once
        self.writes = 0                         # counts them, so a write between a look and a wait isn't missed
        self.on_write: list = []                # also told after every write (the app's live updates)
        c = self._connect()
        try:
            c.executescript(SCHEMA)
            cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
            if "not_before" not in cols:
                c.execute("ALTER TABLE jobs ADD COLUMN not_before REAL")
            agent_cols = {r["name"] for r in c.execute("PRAGMA table_info(agents)")}
            if "provider" not in agent_cols:
                c.execute("ALTER TABLE agents ADD COLUMN provider TEXT")
            if "owner" not in agent_cols:
                c.execute("ALTER TABLE agents ADD COLUMN owner INTEGER NOT NULL DEFAULT 0")
            sc_cols = {r["name"] for r in c.execute("PRAGMA table_info(session_cursors)")}
            for col in ("seen REAL", "ended REAL"):
                if col.split()[0] not in sc_cols:
                    c.execute(f"ALTER TABLE session_cursors ADD COLUMN {col}")
            c.execute("INSERT OR IGNORE INTO meta(key,value) SELECT 'session_floor', COALESCE(MAX(id), 0) FROM messages")
            msg_cols = {r["name"] for r in c.execute("PRAGMA table_info(messages)")}
            if "origin_session" not in msg_cols:     # the exact chat that sent it (routing only)
                c.execute("ALTER TABLE messages ADD COLUMN origin_session TEXT")
            if "client" not in agent_cols:          # the app it connects from, as that app names itself
                c.execute("ALTER TABLE agents ADD COLUMN client TEXT")
            for col in ("listener_app TEXT", "listener_seen REAL", "listener_woke REAL"):
                if col.split()[0] not in agent_cols:   # the Relay helper on the agent's computer
                    c.execute(f"ALTER TABLE agents ADD COLUMN {col}")
            if "provider_source" not in agent_cols:  # 'invite' | 'detected' | 'owner'
                c.execute("ALTER TABLE agents ADD COLUMN provider_source TEXT")
            if "last_read" not in agent_cols:        # when the agent last read its inbox (wake-up checks)
                c.execute("ALTER TABLE agents ADD COLUMN last_read REAL")
                # reads were never recorded before: a fire that got through says nothing yet, so start at
                # "ready" (press Test) rather than claim the agent never read anything
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='wakes'").fetchone():
                    c.execute("UPDATE wakes SET last_fired=NULL, last_status=NULL WHERE last_status LIKE 'ok%'")
            # hubs from before convs/members: build both once from the messages already there
            if not c.execute("SELECT 1 FROM meta WHERE key='convs_built'").fetchone():
                c.execute("INSERT OR REPLACE INTO convs(thread,last,n) SELECT thread, MAX(id), COUNT(*) FROM messages"
                          " WHERE thread IS NOT NULL GROUP BY thread")
                c.execute("INSERT OR IGNORE INTO members(thread,agent) SELECT thread, sender FROM messages"
                          " WHERE thread IS NOT NULL AND sender!='relay'")
                c.execute("INSERT OR IGNORE INTO members(thread,agent) SELECT x.thread, j.value FROM messages x, json_each(x.mentions) j"
                          " WHERE x.thread IS NOT NULL")
                c.execute("INSERT INTO meta(key,value) VALUES ('convs_built', '1')")
            c.execute("INSERT OR IGNORE INTO meta(key,value) SELECT 'seen_floor', COALESCE(MAX(id), 0) FROM messages")
            # search: a full-text index of every message, kept up to date by the database itself
            self.fts = False
            try:
                c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(text, content='messages', content_rowid='id',"
                          " tokenize='unicode61 remove_diacritics 2')")
                c.execute("CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages BEGIN"
                          " INSERT INTO msg_fts(rowid, text) VALUES (new.id, new.text); END")
                c.execute("CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF text ON messages BEGIN"
                          " INSERT INTO msg_fts(msg_fts, rowid, text) VALUES ('delete', old.id, old.text);"
                          " INSERT INTO msg_fts(rowid, text) VALUES (new.id, new.text); END")
                c.execute("CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN"
                          " INSERT INTO msg_fts(msg_fts, rowid, text) VALUES ('delete', old.id, old.text); END")
                if not c.execute("SELECT 1 FROM meta WHERE key='fts_built'").fetchone():
                    c.execute("INSERT INTO msg_fts(msg_fts) VALUES ('rebuild')")      # what was there before
                    c.execute("INSERT INTO meta(key,value) VALUES ('fts_built', '1')")
                self.fts = True
            except sqlite3.OperationalError:          # a SQLite without FTS5: search falls back to a plain scan
                pass
            invite_cols = {r["name"] for r in c.execute("PRAGMA table_info(invites)")}
            if "provider" not in invite_cols:
                c.execute("ALTER TABLE invites ADD COLUMN provider TEXT")
            if "rejoin" not in invite_cols:          # a code that adds a key to an agent that already exists
                c.execute("ALTER TABLE invites ADD COLUMN rejoin INTEGER NOT NULL DEFAULT 0")
        finally:
            c.close()

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=30000")
        return c

    @contextmanager
    def tx(self):
        """Write transaction. BEGIN IMMEDIATE serialises writers, so a job can only
        ever be claimed once even with many nodes polling at the same moment."""
        c = self._connect()
        try:
            c.execute("BEGIN IMMEDIATE")
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
        finally:
            c.close()
        with self.changed:                 # anyone long-polling looks again now, not on its next tick
            self.writes += 1
            self.changed.notify_all()
        for f in self.on_write:
            try:
                f()
            except Exception:
                pass

    @contextmanager
    def ro(self):
        c = self._connect()
        try:
            yield c
        finally:
            c.close()

    def blob_path(self, aid: str) -> str:
        return os.path.join(self.blobs, aid[:2], aid)


# --------------------------------------------------------------------------- shapes
CONTEXT_MESSAGES, CONTEXT_CHARS, PARENT_CHARS = 6, 6000, 12000


def _fit(texts: list, budget: int) -> list:
    """Trim the longest texts first (with "…") until all of them fit in `budget` characters."""
    if sum(len(t) for t in texts) <= budget:
        return texts
    lo, hi = 0, max(len(t) for t in texts)          # the largest per-text cap that still fits
    while lo < hi:
        mid = (lo + hi + 1) // 2
        lo, hi = (mid, hi) if sum(min(len(t), mid) for t in texts) <= budget else (lo, mid - 1)
    cap = max(lo, 2)
    return [t if len(t) <= cap else t[:cap - 1].rstrip() + "…" for t in texts]


def with_context(c, msgs: list) -> list:
    """A woken agent starts with no memory: a reply such as "do this" means nothing without what came
    before it. The first message of each thread in a batch gets `context`, the thread's earlier
    messages (oldest first, the last few, trimmed to a budget). Context is never marked read."""
    seen = set()
    for m in msgs:
        t = m.get("thread")
        if t is None or t in seen:
            continue
        seen.add(t)
        rows = c.execute("SELECT id, sender, text, time FROM messages WHERE thread=? AND id<? ORDER BY id DESC LIMIT ?",
                         (t, m["id"], CONTEXT_MESSAGES)).fetchall()[::-1]
        if rows:
            # the message being answered stays whole (it's the one a short reply refers to); the rest share a budget
            parent = m.get("reply_to")
            rest = [r for r in rows if r["id"] != parent]
            fitted = dict(zip([r["id"] for r in rest], _fit([r["text"] or "" for r in rest], CONTEXT_CHARS)))
            for r in rows:
                if r["id"] == parent:
                    fitted[r["id"]] = _fit([r["text"] or ""], PARENT_CHARS)[0]
            m["context"] = [{"id": r["id"], "from": r["sender"], "time": r["time"], "text": fitted[r["id"]]} for r in rows]
            if any(fitted[r["id"]] != (r["text"] or "") for r in rows):     # never a silent cut: say where the rest is
                m["context_note"] = f"Some earlier messages are shortened (…). check_replies with thread {t} gives them whole."
    return msgs


def msg_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "from": r["sender"], "channel": r["channel"],
            "thread": r["thread"], "reply_to": r["reply_to"], "time": r["time"],
            "text": r["text"], "mentions": _loads(r["mentions"], []),
            "artifacts": _loads(r["artifacts"], []), "job": r["job"]}


def job_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "kind": r["kind"], "params": _loads(r["params"], {}),
            "requested_by": r["requested_by"], "status": r["status"],
            "claimed_by": r["claimed_by"], "lease_until": r["lease_until"],
            "attempts": r["attempts"], "result": _loads(r["result"], None),
            "artifacts": _loads(r["artifacts"], []), "error": r["error"],
            "thread": r["thread"], "requires_approval": bool(r["requires_approval"]),
            "approved_by": r["approved_by"], "parent_job": r["parent_job"],
            "not_before": r["not_before"],
            "created": r["created"], "updated": r["updated"]}


def art_out(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "mime": r["mime"], "size": r["size"],
            "created_by": r["created_by"], "created": r["created"],
            "meta": _loads(r["meta"], {}), "parent": r["parent"], "kind": r["kind"],
            "url": f"/artifacts/{r['id']}"}


# --------------------------------------------------------------------------- bodies
class AgentIn(BaseModel):
    handle: str
    caps: list[str] = Field(default_factory=list)
    role: str = "agent"            # "agent" | "human"
    provider: Optional[str] = None


class InviteIn(BaseModel):
    handle: str
    caps: list[str] = Field(default_factory=list)
    role: str = "agent"
    provider: Optional[str] = None
    hours: float = 24
    note: Optional[str] = None
    rejoin: bool = False             # for an agent that already exists: one more key, on another computer


class OwnerIn(BaseModel):
    handle: str


class WakeIn(BaseModel):
    kind: str                       # claude_routine | webhook
    url: str
    token: Optional[str] = None     # routine API token, or a webhook signing secret (else generated)


class ClaimInviteIn(BaseModel):
    code: str


class CapsIn(BaseModel):
    caps: list[str]


class ListenerIn(BaseModel):
    app: Optional[str] = None        # which app the Relay helper wakes: codex | claude-code
    woke: Optional[float] = None     # when it last woke that app (the helper's own clock)


class PushKeysIn(BaseModel):
    p256dh: str = Field(max_length=200)
    auth: str = Field(max_length=100)


class PushDeviceIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: Optional[PushKeysIn] = None
    kinds: Optional[List[str]] = None
    label: Optional[str] = Field(default=None, max_length=60)


class ShortcutIn(BaseModel):
    title: str = Field(default="", max_length=80)
    text: str = Field(min_length=1, max_length=4000)
    mentions: List[str] = Field(default_factory=list, max_length=20)


class MarkIn(BaseModel):
    pinned: Optional[bool] = None
    muted: Optional[bool] = None


class PrefsIn(BaseModel):
    digest: Optional[bool] = None
    tz: Optional[int] = Field(default=None, ge=-720, le=840)     # minutes east of UTC: the daily email comes in your morning


class DeleteIn(BaseModel):
    confirm: str = Field(max_length=64)          # the person's own name, typed: no accidents


class MessageIn(BaseModel):
    text: str = Field(max_length=20000)          # the same limit agents have through the connector
    channel: Optional[str] = Field(default=None, max_length=64)
    reply_to: Optional[int] = None
    mentions: list[str] = Field(default_factory=list, max_length=20)
    artifacts: list[str] = Field(default_factory=list, max_length=10)
    media: list[str] = Field(default_factory=list, max_length=10)
    idem: Optional[str] = None


class AckIn(BaseModel):
    cursor: int


class ChannelIn(BaseModel):
    channel: str


class FollowIn(BaseModel):
    thread: int


class JobIn(BaseModel):
    kind: str
    params: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    reply_to: Optional[int] = None
    note: Optional[str] = None


class ClaimIn(BaseModel):
    kinds: list[str] = Field(default_factory=list)


class CompleteIn(BaseModel):
    result: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)


class FailIn(BaseModel):
    error: str
    retry: bool = True


# --------------------------------------------------------------------------- app
def create_app(data_dir: Optional[str] = None, admin_token: Optional[str] = None,
               lease_seconds: Optional[float] = None,
               max_attempts: Optional[int] = None,
               max_upload_mb: Optional[int] = None,
               retry_base_seconds: Optional[float] = None,
               allow_private_fetch: Optional[bool] = None,
               wake_opener=None, wake_check_url: bool = True, wake_cooldown: Optional[float] = None,
               supabase_url: Optional[str] = None, supabase_key: Optional[str] = None,
               supabase_send=None, resend_key: Optional[str] = None, email_from: Optional[str] = None,
               resend_send=None, vault_send=None, vault_resolve=None, caption_transcriber=None, caption_threads: bool = True,
               caption_public_url: Optional[str] = None, push_opener=None,
               session_idle_seconds: Optional[float] = None,
               session_sweep_seconds: Optional[float] = None,
               woken_idle_seconds: Optional[float] = None,
               media_threads: Optional[bool] = None) -> FastAPI:
    data_dir = data_dir or os.environ.get("RELAY_DATA", "./relay-data")
    os.makedirs(data_dir, exist_ok=True)
    admin_token = admin_token or os.environ.get("RELAY_ADMIN_TOKEN")
    if not admin_token:
        # never print the token: write it to a 0600 file next to the data
        path = os.path.join(data_dir, "admin_token")
        if os.path.exists(path):
            admin_token = open(path).read().strip()
        else:
            admin_token = secrets.token_urlsafe(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(admin_token)
        print(f"[relay] admin token stored in {path}")
    # The hub's own secret, for what must outlive any change of admin token: saved keys, wake-up
    # secrets and media links. Made once, in a 0600 file beside the data; never printed or served.
    secret_path = os.path.join(data_dir, "hub_secret")
    if os.path.exists(secret_path):
        hub_secret = open(secret_path).read().strip()
    else:
        hub_secret = secrets.token_urlsafe(48)
        fd = os.open(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(hub_secret)
    lease = float(lease_seconds or os.environ.get("RELAY_LEASE_SECONDS", 300))
    max_att = int(max_attempts or os.environ.get("RELAY_MAX_ATTEMPTS", 3))
    max_upload = int(max_upload_mb or os.environ.get("RELAY_MAX_UPLOAD_MB", 500)) * 2**20
    retry_base = float(retry_base_seconds if retry_base_seconds is not None
                       else os.environ.get("RELAY_RETRY_BASE_SECONDS", 30))
    allow_private = (allow_private_fetch if allow_private_fetch is not None
                     else os.environ.get("RELAY_ALLOW_PRIVATE_FETCH") == "1")

    store = Store(data_dir)
    public_url = (os.environ.get("RELAY_PUBLIC_URL") or "").rstrip("/") or None

    def origin(request: Request) -> str:
        """This hub's own address, as apps should use it (behind Railway: https, from the proxy headers)."""
        return public_url or own_origin(request)

    def own_origin(request: Request) -> str:
        """The address this request actually came in on. Passkeys and an app's sign-in are tied to it, so while
        a hub moves to a new domain (RELAY_PUBLIC_URL), the old address keeps working for both."""
        return f"{request.url.scheme}://{request.url.netloc}"

    # Moving to a new domain: pages a person opens on the old address go to the new one. Apps keep using the
    # old one (their sign-in, /mcp and the API answer there as before); /app hands a signed-in person over.
    MOVED_PAGES = re.compile(r"^/(?:join|welcome|privacy|terms|m/[^/]+(?:/.*)?|up/[^/]+)?$")

    seen_https: dict = {"origin": None}          # the hub's own https address, as the last visitor reached it

    async def move_home(request: Request, call_next):
        if request.url.scheme == "https":
            seen_https["origin"] = own_origin(request)
        if public_url and request.method == "GET" and own_origin(request) != public_url and MOVED_PAGES.match(request.url.path):
            target = public_url + request.url.path + (("?" + request.url.query) if request.url.query else "")
            return Response(status_code=301, headers={"Location": target, "Cache-Control": "no-store"})
        return await call_next(request)
    # a chat that answered and then went quiet this long is treated as gone: new messages for it in
    # a conversation someone else started go back to the identity (see release())
    session_idle = float(session_idle_seconds if session_idle_seconds is not None
                         else os.environ.get("RELAY_SESSION_IDLE_SECONDS", 600))
    session_sweep = float(session_sweep_seconds if session_sweep_seconds is not None
                          else os.environ.get("RELAY_SESSION_SWEEP_SECONDS", 30))
    # An agent with a wake-up (a Claude routine, a webhook) answers in one-shot runs: a run that has gone
    # quiet this long has finished, so a follow-up wakes a new run instead of waiting ten minutes for it.
    woken_idle = min(session_idle, float(woken_idle_seconds if woken_idle_seconds is not None
                                         else os.environ.get("RELAY_WOKEN_IDLE_SECONDS", 60)))
    with store.ro() as c:
        session_floor = int(c.execute("SELECT value FROM meta WHERE key='session_floor'").fetchone()["value"])

    @asynccontextmanager
    async def lifespan(_app):
        # `ask` and `/inbox?wait` hold a worker thread while they wait; give them room
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = int(
            os.environ.get("RELAY_THREADS", 200))
        yield

    from . import __version__
    app = FastAPI(title="Relay", version=__version__, lifespan=lifespan)
    app.middleware("http")(move_home)
    app.state.store = store
    from .wake import Waker, WakeError
    from .wake import _cipher
    waker = Waker(store, admin_token, os.environ.get("RELAY_SECRET_KEY") or hub_secret, opener=wake_opener,
                  check_url=wake_check_url,
                  cooldown=wake_cooldown if wake_cooldown is not None else float(
                      os.environ.get("RELAY_WAKE_COOLDOWN", 20)))
    app.state.waker = waker
    # notifications on people's phones and computers (Web Push, signed with the hub's own key)
    from . import push as push_mod

    def push_contact() -> str:
        """Who runs this hub, as the push services ask: the published contact, else the hub's address."""
        mail = os.environ.get("RELAY_LEGAL_EMAIL")
        return f"mailto:{mail}" if mail else (public_url or seen_https["origin"] or "https://localhost")
    pusher = push_mod.Pusher(store, data_dir, push_contact, opener=push_opener)
    app.state.pusher = pusher
    if not os.environ.get("RELAY_SECRET_KEY"):     # wake-up secrets locked with the admin token move to the hub's own
        waker.rekey(_cipher(admin_token, None))

    # ---------------------------------------------------------------- auth
    def auth(authorization: str = Header(default="")) -> dict:
        tok = authorization.removeprefix("Bearer ").strip()
        if not tok:
            raise HTTPException(401, "missing bearer token")
        if secrets.compare_digest(tok, admin_token):
            return {"handle": "admin", "role": "admin", "caps": []}
        with store.tx() as c:
            if tok.startswith("rly_oat_"):           # an app connected through "Allow" (OAuth)
                r = c.execute("SELECT a.* FROM oauth_tokens t JOIN agents a ON a.handle=t.handle WHERE t.token_hash=?"
                              " AND t.kind='access' AND t.revoked IS NULL AND t.expires>?", (_hash(tok), _now())).fetchone()
            elif tok.startswith("rly_sess_"):          # a person signed in by email
                sess = c.execute("SELECT handle FROM sessions WHERE token_hash=? AND expires>?",
                                 (_hash(tok), _now())).fetchone()
                r = c.execute("SELECT * FROM agents WHERE handle=?", (sess["handle"],)).fetchone() if sess else None
                if r:
                    c.execute("UPDATE sessions SET last_used=? WHERE token_hash=?", (_now(), _hash(tok)))
            else:
                r = c.execute("SELECT * FROM agents WHERE token_hash=?", (_hash(tok),)).fetchone() or \
                    c.execute("SELECT a.* FROM agent_keys k JOIN agents a ON a.handle=k.handle"
                              " WHERE k.token_hash=?", (_hash(tok),)).fetchone()   # a key from a re-join code
            if not r:
                raise HTTPException(401, "unknown token")
            c.execute("UPDATE agents SET last_seen=? WHERE handle=?", (_now(), r["handle"]))
        return {"handle": r["handle"], "role": r["role"], "caps": _loads(r["caps"], []),
                "owner": bool(r["owner"])}

    def need_agent(me: dict) -> None:
        if me["role"] == "admin":
            raise HTTPException(403, "admin token is for registration only; use an agent token")

    # ---------------------------------------------------------------- privacy between people
    # A person and the agents that work for them are one circle. A conversation (the "direct" channel)
    # is seen only by circles with someone in it: someone who wrote in it, was @mentioned in it, or
    # follows it. Nobody can follow, reply into or read a conversation they aren't in. Other channels
    # ("general", a node's "jobs") are open rooms. The admin token, the operator's key, sees everything.
    # A hub nobody has made theirs yet (no owner: agents and people the admin token registered) is one
    # team, as before: there are no people's things to keep apart.
    def first_owner(c) -> Optional[str]:
        o = c.execute("SELECT handle FROM agents WHERE owner=1 ORDER BY created, handle LIMIT 1").fetchone()
        return o["handle"] if o else None

    def person_of(c, h: str) -> Optional[str]:
        """Who `h` works for: itself for a person; the person who connected an agent, else the owner."""
        r = c.execute("SELECT role, person FROM agents WHERE handle=?", (h,)).fetchone()
        if not r:
            return None
        if r["role"] == "human":
            return h
        return r["person"] or first_owner(c)

    def circle(c, h: str) -> list:
        if not first_owner(c):
            return [r["handle"] for r in c.execute("SELECT handle FROM agents")]
        p = person_of(c, h)
        if not p:
            return [h]
        theirs = " OR person IS NULL" if p == first_owner(c) else ""     # the hub's own agents are the owner's
        return sorted({h, p} | {r["handle"] for r in c.execute(
            f"SELECT handle FROM agents WHERE role='agent' AND (person=?{theirs})", (p,))})

    def visible(c, h: str, alias: str = "m") -> tuple:
        """SQL (and its values) that keeps only messages `h`'s circle may see."""
        if h == "admin" or not first_owner(c):
            return "1=1", []
        who = circle(c, h)
        qs = ",".join("?" * len(who))
        return (f"({alias}.channel!=? OR {alias}.thread IN (SELECT thread FROM follows WHERE agent IN ({qs})"
                f" UNION SELECT thread FROM members WHERE agent IN ({qs})))",
                [DIRECT] + who * 2)

    def can_see(c, h: str, thread: int) -> bool:
        sql, a = visible(c, h)
        return bool(c.execute(f"SELECT 1 FROM messages m WHERE m.id=? AND {sql}", [thread] + a).fetchone())

    # ---------------------------------------------------------------- internals
    def check_session(session: Optional[str]) -> Optional[str]:
        session = (session or "").strip()
        if not session:
            return None
        if not SESSION_RE.match(session):
            raise HTTPException(400, "a session id is 1-200 visible ASCII characters")
        if session.startswith(CONVERSATION):
            raise HTTPException(400, f"session ids starting with '{CONVERSATION}' are reserved")
        return session

    warned: set = set()

    def no_session(handle: str, what: str, header_names=None) -> None:
        """An old client: delivery falls back to the whole identity, as before sessions existed.
        The header *names* it sent (never values) show what that app could identify a chat by."""
        if handle not in warned:
            warned.add(handle)
            log.warning("@%s %s without a session id; replies to it go to the identity as a whole, "
                        "not to the chat that asked (send X-Relay-Session or Mcp-Session-Id)%s", handle, what,
                        f"; headers sent: {', '.join(sorted(header_names))}" if header_names else "")

    def start_session(c, h: str, session: str) -> None:
        """The chat called: note that it is alive. A session first seen starts where its identity's
        cursor is, so it isn't handed old history. A chat that called after it 'ended' is back."""
        c.execute("INSERT OR IGNORE INTO session_cursors(handle,session,last)"
                  " SELECT ?, ?, COALESCE((SELECT last FROM cursors WHERE agent=?), 0)", (h, session, h))
        c.execute("UPDATE session_cursors SET seen=?, ended=NULL WHERE handle=? AND session=?", (_now(), h, session))

    def can_wake(c, h: str) -> bool:
        """Relay can start `h` the moment a message lands: a wake-up (routine, webhook) or a helper listening now."""
        if c.execute("SELECT 1 FROM wakes WHERE handle=?", (h,)).fetchone():
            return True
        r = c.execute("SELECT listener_seen FROM agents WHERE handle=?", (h,)).fetchone()
        return bool(r and r["listener_seen"] and _now() - r["listener_seen"] < 150)

    def answering(c, h: str, thread: int) -> bool:
        """`h` is answering in this thread (someone else started it), rather than waiting for an answer."""
        r = c.execute("SELECT sender FROM messages WHERE id=?", (thread,)).fetchone()
        return bool(r) and r["sender"] != h

    def session_state(c, h: str, session: str) -> str:
        """ended: its app said it exited · idle: no call for `session_idle` seconds · alive."""
        r = c.execute("SELECT seen, ended FROM session_cursors WHERE handle=? AND session=?", (h, session)).fetchone()
        if not r:
            return "idle"
        if r["ended"]:
            return "ended"
        return "idle" if (r["seen"] or 0) < _now() - idle_after(c, h) else "alive"

    def idle_after(c, h: str) -> float:
        woken = c.execute("SELECT 1 FROM wakes WHERE handle=?", (h,)).fetchone()
        return woken_idle if woken else session_idle

    def release(c, h: str, session: str, everything: bool = False) -> int:
        """Give back to the identity what this chat will never read: unread messages held for it in
        conversations it was answering (all of them when its app said it ended). They become new
        requests, so one chat of `h` is woken and takes them, with the thread as context. Answers to
        what this chat itself asked stay held for it unless it ended."""
        rows = c.execute(
            "SELECT m.id, m.thread, m.sender FROM routes r JOIN messages m ON m.id=r.msg"
            " JOIN messages root ON root.id=m.thread"
            " JOIN session_cursors s ON s.handle=r.handle AND s.session=r.session"
            " WHERE r.handle=? AND r.session=? AND m.sender!=? AND m.id>s.last"
            " AND NOT EXISTS (SELECT 1 FROM delivered d WHERE d.msg=m.id AND d.handle=r.handle AND d.session=r.session)"
            + ("" if everything else " AND root.sender!=?"),
            (h, session, h) + (() if everything else (h,))).fetchall()
        for m in rows:
            c.execute("DELETE FROM routes WHERE msg=? AND handle=?", (m["id"], h))
            c.execute("DELETE FROM thread_sessions WHERE thread=? AND handle=? AND session=?", (m["thread"], h, session))
        for sender in {m["sender"] for m in rows}:
            waker.notify({h}, sender)
        return len(rows)

    def route_for(c, h: str, thread: int, reply_to: Optional[int]) -> Optional[str]:
        """The session of `h` that must get a message in `thread`: the chat that sent the message being
        answered, else the chat of `h` already in the thread. None: a new request, any chat may take it."""
        if reply_to is not None:
            p = c.execute("SELECT sender, origin_session FROM messages WHERE id=?", (reply_to,)).fetchone()
            if p and p["sender"] == h and p["origin_session"]:
                return p["origin_session"]
        r = c.execute("SELECT session FROM thread_sessions WHERE thread=? AND handle=?", (thread, h)).fetchone()
        return r["session"] if r else None

    def seen_by(c, h: str, ids) -> None:
        """Agent `h` has these messages in front of it now (a read, not a listener's long-poll)."""
        now = _now()
        c.executemany("INSERT OR IGNORE INTO reads(msg,handle,time) VALUES (?,?,?)", [(i, h, now) for i in ids])

    def take(c, h: str, session: Optional[str], msgs: list) -> None:
        """A chat read these: the ones no chat of `h` owned yet are now its own, and so is their thread.
        An old client (no session) claims them as '' so that no chat handles them a second time."""
        seen_by(c, h, [m["id"] for m in msgs])
        for m in msgs:
            c.execute("INSERT OR IGNORE INTO routes(msg,handle,session) VALUES (?,?,?)", (m["id"], h, session or ""))
            if session and m.get("thread") is not None:     # a released thread moves to the chat taking it
                c.execute("INSERT INTO thread_sessions(thread,handle,session) VALUES (?,?,?)"
                          " ON CONFLICT(thread,handle) DO UPDATE SET session=excluded.session", (m["thread"], h, session))

    def post(c, sender, text, channel=None, reply_to=None, mentions=(), artifacts=(),
             idem=None, job=None, session=None, media=()) -> dict:
        mentions = sorted({m.lstrip("@") for m in mentions})
        if idem:
            prev = c.execute("SELECT * FROM messages WHERE sender=? AND idem=?",
                             (sender, idem)).fetchone()
            if prev:
                same = (prev["text"] == text and _loads(prev["mentions"], []) == mentions
                        and (reply_to is None or prev["reply_to"] == reply_to)
                        and (channel is None or prev["channel"] == channel))
                if not same:
                    raise HTTPException(409, "idempotency key reused with different content")
                return msg_out(prev)
        thread = None
        if reply_to is not None:
            parent = c.execute("SELECT id, channel, thread FROM messages WHERE id=?",
                               (reply_to,)).fetchone()
            if not parent:
                raise HTTPException(404, f"reply_to {reply_to} not found")
            channel, thread = parent["channel"], parent["thread"] or parent["id"]
            if sender != SYSTEM and not can_see(c, sender, thread):     # not your conversation: it doesn't exist
                raise HTTPException(404, f"reply_to {reply_to} not found")
        elif not channel:
            raise HTTPException(400, "channel is required to start a thread")
        for m in mentions:     # resolved at post time; unknown handles fail before commit
            if not c.execute("SELECT 1 FROM agents WHERE handle=?", (m,)).fetchone():
                raise HTTPException(400, f"unknown mention @{m}")
        for a in artifacts:            # only files the sender may see can travel with its messages
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (a,)).fetchone() or (
                    sender != SYSTEM and not artifact_seen(c, {"handle": sender, "role": "agent"}, a)):
                raise HTTPException(400, f"unknown artifact {a}")
        for m in media:
            if not c.execute("SELECT 1 FROM media WHERE id=?", (m,)).fetchone():
                raise HTTPException(400, f"unknown media {m}")
        cur = c.execute(
            "INSERT INTO messages(sender,idem,channel,thread,reply_to,time,text,mentions,artifacts,job,origin_session)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sender, idem, channel, thread, reply_to, _now(), text,
             json.dumps(mentions), json.dumps(list(artifacts)), job, session))
        mid = cur.lastrowid
        for m in dict.fromkeys(media):     # videos, images, audio, files that travel with this message
            c.execute("INSERT OR IGNORE INTO message_media(msg,media) VALUES (?,?)", (mid, m))
        if thread is None:
            thread = mid
            c.execute("UPDATE messages SET thread=? WHERE id=?", (mid, mid))
        c.execute("INSERT INTO convs(thread,last,n) VALUES (?,?,1) ON CONFLICT(thread) DO UPDATE SET last=excluded.last, n=n+1",
                  (thread, mid))
        if sender != SYSTEM and not text.startswith("\U0001F511"):     # an agent answered (a key note has its own)
            row = c.execute("SELECT role FROM agents WHERE handle=?", (sender,)).fetchone()
            if row and row["role"] == "agent":
                people_in = {r["agent"] for r in c.execute(
                    "SELECT m.agent FROM members m JOIN agents a ON a.handle=m.agent WHERE m.thread=? AND a.role='human'", (thread,))}
                people_in |= {m for m in mentions if (c.execute("SELECT role FROM agents WHERE handle=?", (m,)).fetchone() or {"role": ""})["role"] == "human"}
                line = next((l.strip() for l in text.splitlines() if l.strip()), "")
                quiet = {r["handle"] for r in c.execute("SELECT handle FROM marks WHERE thread=? AND muted=1", (thread,))}
                for person in people_in - {sender} - quiet:
                    pusher.notify(person, "answers", f"{sender} answered", line[:200] or "A new message",
                                  f"/app#activity/{thread}", tag=f"t{thread}")
        for m in {sender, *mentions} - {SYSTEM}:
            c.execute("INSERT OR IGNORE INTO members(thread,agent) VALUES (?,?)", (thread, m))
        if sender != SYSTEM:
            c.execute("INSERT OR IGNORE INTO follows(agent,thread) VALUES (?,?)", (sender, thread))
        if session:                    # this chat is in the thread now: later messages in it come back here
            start_session(c, sender, session)
            c.execute("INSERT INTO thread_sessions(thread,handle,session) VALUES (?,?,?)"
                      " ON CONFLICT(thread,handle) DO UPDATE SET session=excluded.session", (thread, sender, session))
            c.execute("INSERT OR IGNORE INTO routes(msg,handle,session) VALUES (?,?,?)", (mid, sender, session))
        # who should hear about this right away: mentioned, following the thread, or subscribed
        who = (set(mentions) | {r["agent"] for r in c.execute(
            "SELECT agent FROM follows WHERE thread=? UNION SELECT agent FROM subs WHERE channel=?",
            (thread, channel))}) - {sender}
        wake = set()
        for h in who:
            target = route_for(c, h, thread, reply_to)
            if target and answering(c, h, thread) and not can_wake(c, h):
                # a chat app (ChatGPT, Grok, Muse…) answering here can't be woken: its person opens whichever chat
                # they like next, so the reply goes to that chat, not one held for a chat they may have closed
                target = None
            if target:
                state = session_state(c, h, target)
                if state == "ended" or (state == "idle" and answering(c, h, thread)):
                    target = None      # that chat is gone: a new one takes the conversation over
            if target:                 # for a chat that exists: held for it, never a new chat
                c.execute("INSERT OR IGNORE INTO routes(msg,handle,session) VALUES (?,?,?)", (mid, h, target))
            else:                      # a new request: whichever chat of `h` reads it first takes it
                wake.add(h)
        waker.notify(wake, sender)
        return msg_out(c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

    def inbox_for(c, h: str, limit: int = 20, session: Optional[str] = None, unclaimed: bool = False) -> dict:
        """What is waiting for `h`. With a session: that chat's own messages plus new requests no chat
        of `h` has taken, read from that chat's own cursor. Without one (an old client): everything for
        `h`, as before. `unclaimed` keeps only messages no chat owns: what may start a new chat."""
        if session:
            start_session(c, h, session)
            cur = c.execute("SELECT last FROM session_cursors WHERE handle=? AND session=?", (h, session)).fetchone()
        else:
            cur = c.execute("SELECT last FROM cursors WHERE agent=?", (h,)).fetchone()
        last = cur["last"] if cur else 0
        q = ("SELECT m.* FROM messages m WHERE m.id>? AND m.sender!=? AND ("
             " EXISTS (SELECT 1 FROM json_each(m.mentions) WHERE value=?)"
             " OR (m.channel!=? AND m.channel IN (SELECT channel FROM subs WHERE agent=?))"
             " OR m.thread IN (SELECT thread FROM follows WHERE agent=?))"
             # the same messages, found through indexes: only conversations h is in (a mention makes h a member)
             # and channels h follows, so a new chat's first look doesn't read the hub's whole history
             " AND (m.thread IN (SELECT thread FROM members WHERE agent=? UNION SELECT thread FROM follows WHERE agent=?)"
             " OR (m.channel!=? AND m.channel IN (SELECT channel FROM subs WHERE agent=?)))")
        a: list = [last, h, h, DIRECT, h, h, h, h, DIRECT, h]
        if session:
            q += (" AND NOT EXISTS (SELECT 1 FROM routes r WHERE r.msg=m.id AND r.handle=? AND r.session!=?)"
                  " AND NOT EXISTS (SELECT 1 FROM delivered d WHERE d.msg=m.id AND d.handle=? AND d.session=?)"
                  # from before session routing, only what was routed to this chat (no old backlog)
                  " AND (m.id>? OR EXISTS (SELECT 1 FROM routes r WHERE r.msg=m.id AND r.handle=? AND r.session=?))")
            a += [h, session, h, session, session_floor, h, session]
        elif unclaimed:
            q += " AND NOT EXISTS (SELECT 1 FROM routes r WHERE r.msg=m.id AND r.handle=?)"
            a += [h]
        else:                  # answers held for a conversation an app without ids started: only that chat opens them
            q += " AND NOT EXISTS (SELECT 1 FROM routes r WHERE r.msg=m.id AND r.handle=? AND r.session LIKE ?)"
            a += [h, CONVERSATION + "%"]
        rows = c.execute(q + " ORDER BY m.id LIMIT ?", a + [limit]).fetchall()
        msgs = [msg_out(r) for r in rows]
        return {"messages": msgs, "cursor": msgs[-1]["id"] if msgs else last}

    def mark_read(c, h: str) -> None:
        """The agent itself fetched its messages. Unlike last_seen (any call at all), this is what tells
        a wake-up that the agent it started really reached Relay."""
        c.execute("UPDATE agents SET last_read=? WHERE handle=?", (_now(), h))

    def ack_to(c, h: str, cursor: int, session: Optional[str] = None) -> None:
        if session:
            start_session(c, h, session)
            c.execute("UPDATE session_cursors SET last=MAX(last, ?) WHERE handle=? AND session=?", (cursor, h, session))
            return
        c.execute("INSERT INTO cursors(agent,last) VALUES (?,?) ON CONFLICT(agent)"
                  " DO UPDATE SET last=MAX(last, excluded.last)", (h, cursor))

    def is_agent(c, handle) -> bool:
        return bool(c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone())

    def notify(c, job: sqlite3.Row, text: str, artifacts=()):
        who = [job["requested_by"]] if is_agent(c, job["requested_by"]) else []
        post(c, SYSTEM, text, reply_to=job["thread"], mentions=who,
             artifacts=artifacts, job=job["id"])

    def requeue_expired(c):
        for j in c.execute("SELECT * FROM jobs WHERE status='claimed' AND lease_until<?",
                           (_now(),)).fetchall():
            if j["attempts"] >= max_att:
                c.execute("UPDATE jobs SET status='failed', claimed_by=NULL, lease_until=NULL,"
                          " error=?, updated=? WHERE id=?",
                          (f"lease expired {j['attempts']} times", _now(), j["id"]))
                notify(c, j, f"job {j['id']} failed: its node went quiet {j['attempts']} times")
            else:
                c.execute("UPDATE jobs SET status='queued', claimed_by=NULL, lease_until=NULL,"
                          " error='lease expired, requeued', updated=? WHERE id=?",
                          (_now(), j["id"]))

    def create_job(c, me_handle, kind, params, requires_approval=False, reply_to=None,
                   note=None, parent_job=None, thread=None, session=None) -> dict:
        if kind == "fetch":
            url = str(params.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "fetch needs params.url (http or https)")
            if not allow_private and private_host(urllib.parse.urlparse(url).hostname or ""):
                raise HTTPException(400, "fetch may not target local or private network addresses")
        if kind in ("digest", "clip"):
            aid = str(params.get("artifact", ""))
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (aid,)).fetchone() or (
                    is_agent(c, me_handle) and not artifact_seen(c, {"handle": me_handle, "role": "agent"}, aid)):
                raise HTTPException(400, f"{kind} needs params.artifact (an existing artifact id)")
        if kind == "clip":
            try:
                count, seconds = int(params.get("count", 3)), float(params.get("seconds", 10))
            except (TypeError, ValueError):
                raise HTTPException(400, "clip: count and seconds must be numbers")
            if not 1 <= count <= 5 or not 6 <= seconds <= 20:
                raise HTTPException(400, "clip: count must be 1 to 5 and seconds 6 to 20")
            if str(params.get("aspect", "9:16")) not in CLIP_ASPECTS:
                raise HTTPException(400, f"clip: aspect must be one of {', '.join(CLIP_ASPECTS)}")
        jid = "j_" + uuid.uuid4().hex[:12]
        summary = ", ".join(f"{k}={v}" for k, v in params.items())[:300]
        text = f"job {jid} [{kind}] {summary}" + (f" — {note}" if note else "")
        sender = me_handle if is_agent(c, me_handle) else SYSTEM
        session = session if sender == me_handle else None
        if thread is not None:
            m = post(c, sender, text, reply_to=thread, job=jid, session=session)
        elif reply_to is not None:
            m = post(c, sender, text, reply_to=reply_to, job=jid, session=session)
        else:
            m = post(c, sender, text, channel="jobs", job=jid, session=session)
        status = "needs_approval" if requires_approval else "queued"
        c.execute("INSERT INTO jobs(id,kind,params,requested_by,status,thread,requires_approval,"
                  "parent_job,created,updated) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (jid, kind, json.dumps(params), me_handle, status, m["thread"],
                   int(requires_approval), parent_job, _now(), _now()))
        return job_out(c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def get_job(c, jid) -> sqlite3.Row:
        j = c.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        if not j:
            raise HTTPException(404, f"job {jid} not found")
        return j

    def my_job(c, me: dict, jid) -> sqlite3.Row:
        """A job my circle asked for, or one I'm working on. Anyone else's doesn't exist."""
        j = get_job(c, jid)
        if me["role"] != "admin" and j["claimed_by"] != me["handle"] and j["requested_by"] not in circle(c, me["handle"]):
            raise HTTPException(404, f"job {jid} not found")
        return j

    # ---------------------------------------------------------------- health
    def need_admin(me: dict) -> None:
        # the hub's owner (a person who signed in as themselves) can do everything the admin token can
        if me["role"] != "admin" and not me.get("owner"):
            raise HTTPException(403, "only the hub's owner can do this")

    def check_provider(provider: Optional[str]) -> Optional[str]:
        if provider in (None, ""):
            return None
        if not PROVIDER_RE.match(provider):
            raise HTTPException(400, "provider must be a short lowercase name like claude or openai")
        return provider

    def check_new_agent(handle: str, role: str) -> None:
        if not HANDLE_RE.match(handle) or handle in RESERVED:
            raise HTTPException(400, "name must be lowercase letters, digits, . _ - (max 32), not reserved")
        if role not in ("agent", "human"):
            raise HTTPException(400, "role must be agent or human")

    def invite_out(r: sqlite3.Row) -> dict:
        now = _now()
        state = ("claimed" if r["claimed"] else "revoked" if r["revoked"]
                 else "expired" if r["expires"] <= now else "pending")
        return {"id": r["id"], "handle": r["handle"], "role": r["role"],
                "caps": _loads(r["caps"], []), "provider": r["provider"], "note": r["note"], "created": r["created"],
                "expires": r["expires"], "claimed": r["claimed"], "state": state, "rejoin": bool(r["rejoin"])}

    # ---------------------------------------------------------------- web page
    # The dashboard is static: one HTML file and one script, no third-party code.
    # Tokens live in the browser tab's sessionStorage and go only in the
    # Authorization header, never in a URL or cookie.
    here = os.path.dirname(os.path.abspath(__file__))
    page_headers = {
        "Content-Security-Policy": ("default-src 'none'; script-src 'self'; connect-src 'self'; worker-src 'self'; "
                                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
                                    "manifest-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cache-Control": "no-store",
    }

    def static(name: str, mime: str) -> Response:
        try:
            with open(os.path.join(here, name), "rb") as f:
                return Response(f.read(), media_type=mime, headers=page_headers)
        except FileNotFoundError:
            raise HTTPException(500, f"{name} is missing from this install")

    @app.get("/providers", include_in_schema=False)
    def providers_table():      # the apps Relay knows: one table for the hub, the Allow page and the web app
        from . import providers
        return providers.table()

    @app.get("/join", include_in_schema=False)
    def join_page():           # the invite code travels in the #fragment, never the path
        return web("app.html")

    logos = {f[:-4] for f in os.listdir(os.path.join(here, "logos"))
             if f.endswith(".svg")} if os.path.isdir(os.path.join(here, "logos")) else set()

    @app.get("/logos/{name}.svg", include_in_schema=False)
    def logo(name: str):       # only files that shipped with the hub; no path from the request
        if name not in logos:
            raise HTTPException(404, "no such logo")
        with open(os.path.join(here, "logos", name + ".svg"), "rb") as f:
            return Response(f.read(), media_type="image/svg+xml",
                            headers={**page_headers, "Cache-Control": "public, max-age=86400"})

    web_dir = os.path.join(here, "web")
    web_files = {f: f for f in (os.listdir(web_dir) if os.path.isdir(web_dir) else [])}
    web_types = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".jpg": "image/jpeg", ".webp": "image/webp", ".mp4": "video/mp4",
                 ".json": "application/json",
                 # the one-line installers: plain text, so `irm` hands PowerShell a string
                 ".sh": "text/plain; charset=utf-8", ".ps1": "text/plain; charset=utf-8",
                 ".py": "text/x-python; charset=utf-8", ".ico": "image/x-icon"}
    media_types = (".mp4", ".jpg", ".webp", ".png")

    def web(name: str) -> Response:
        if name not in web_files:
            raise HTTPException(404, "not found")
        ext = os.path.splitext(name)[1]
        mime = web_types.get(ext, "application/octet-stream")
        if ext in media_types:
            # streamed with byte ranges (Safari will not play a video without them), and cacheable:
            # these files only change with a deploy
            return FileResponse(os.path.join(web_dir, name), media_type=mime,
                                headers={**page_headers, "Cache-Control": "public, max-age=3600"})
        with open(os.path.join(web_dir, name), "rb") as f:
            return Response(f.read(), media_type=mime, headers=page_headers)

    # Relay's icon where apps and browsers look for one: connector lists, tabs, home screens
    for path, name in (("/favicon.ico", "favicon.ico"), ("/favicon.svg", "icon.svg"), ("/icon.svg", "icon.svg"),
                       ("/apple-touch-icon.png", "apple-touch-icon.png"), ("/icon-192.png", "icon-192.png"),
                       ("/icon-512.png", "icon-512.png"), ("/og.png", "og.png")):
        app.add_api_route(path, (lambda n=name: icon_file(n)), methods=["GET"], include_in_schema=False)

    def icon_file(name: str) -> Response:
        if name not in web_files:
            raise HTTPException(404, "not found")
        mime = web_types.get(os.path.splitext(name)[1], "application/octet-stream")
        return FileResponse(os.path.join(web_dir, name), media_type=mime,
                            headers={"Cache-Control": "public, max-age=86400", "X-Content-Type-Options": "nosniff"})

    @app.get("/app", include_in_schema=False)
    def app_page():
        return web("app.html")

    @app.get("/", include_in_schema=False)
    def landing_page(request: Request):
        # link previews need the page's own address spelled out (a shared link shows Relay's card)
        with open(os.path.join(web_dir, "landing.html"), encoding="utf-8") as f:
            page = f.read().replace("{{origin}}", html.escape(origin(request), quote=True)).replace(
                "{{legal}}", ' <a href="/privacy">Privacy</a> · <a href="/terms">Terms</a>' if legal_on else "")
        return Response(page, media_type="text/html; charset=utf-8", headers=page_headers)

    # Privacy and terms: written from what Relay really does, with the operator's name, contact and place filled in
    # from RELAY_LEGAL_NAME, RELAY_LEGAL_EMAIL and RELAY_LEGAL_PLACE. Until all three are set, the pages don't exist.
    LEGAL_UPDATED = "November 25, 2026"           # change with the text
    legal = {k: (os.environ.get(f"RELAY_LEGAL_{k.upper()}") or "").strip() for k in ("name", "email", "place")}
    legal_on = all(legal.values())

    def legal_page(name: str) -> Response:
        if not legal_on:
            raise HTTPException(404, "not found")
        with open(os.path.join(web_dir, name), encoding="utf-8") as f:
            page = f.read()
        for k, v in {**legal, "updated": LEGAL_UPDATED}.items():
            page = page.replace("{{" + k + "}}", html.escape(v, quote=True))
        return Response(page, media_type="text/html; charset=utf-8", headers=page_headers)

    app.add_api_route("/privacy", lambda: legal_page("privacy.html"), methods=["GET"], include_in_schema=False)
    app.add_api_route("/terms", lambda: legal_page("terms.html"), methods=["GET"], include_in_schema=False)

    # ---------------------------------------------------------------- notifications (Web Push)
    def tell_owner(kind: str, title: str, body: str, url: str, tag: str) -> None:
        with store.ro() as c:
            owner = first_owner(c)
        if owner:
            pusher.notify(owner, kind, title, body, url, tag=tag)

    def need_person(me: dict) -> str:
        if me["role"] != "human":
            raise HTTPException(403, "Notifications are for people. Sign in as yourself.")
        return me["handle"]

    @app.get("/push/key")
    def push_key():
        """The hub's public key, which a browser needs to subscribe (applicationServerKey)."""
        return {"key": pusher.public}

    @app.post("/push/devices")
    def push_subscribe(body: PushDeviceIn, me: dict = Depends(auth)):
        """This browser wants notifications: its push subscription and which kinds."""
        h = need_person(me)
        if not body.keys:
            raise HTTPException(400, "send the subscription's keys (p256dh and auth)")
        kinds = body.kinds if body.kinds is not None else list(push_mod.KINDS)
        try:
            return pusher.subscribe(h, body.endpoint, body.keys.p256dh, body.keys.auth, kinds, body.label)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

    @app.post("/push/devices/find")
    def push_device(body: PushDeviceIn, me: dict = Depends(auth)):
        """Is this browser's subscription on the hub, and for which kinds? (The endpoint goes in the body.)"""
        return pusher.device(need_person(me), body.endpoint) or {"endpoint": None, "kinds": []}

    @app.put("/push/devices")
    def push_kinds(body: PushDeviceIn, me: dict = Depends(auth)):
        got = pusher.set_kinds(need_person(me), body.endpoint, body.kinds or [])
        if not got:
            raise HTTPException(404, "This device isn't set up for notifications.")
        return got

    @app.post("/push/devices/remove")
    def push_unsubscribe(body: PushDeviceIn, me: dict = Depends(auth)):
        return {"removed": pusher.unsubscribe(need_person(me), body.endpoint)}

    @app.post("/push/test")
    def push_test(me: dict = Depends(auth)):
        """A notification to every device of yours, to see that it works."""
        n = pusher.notify(need_person(me), "approvals", "Relay notifications are on",
                          "This is what it looks like when something needs you.", "/app#activity", tag="test")
        return {"sent": n}

    @app.get("/sw.js", include_in_schema=False)
    def service_worker():
        """Shows notifications and opens the right screen when one is tapped. Served from the root so it
        covers the whole app."""
        with open(os.path.join(web_dir, "sw.js"), "rb") as f:
            # the app's own files, as a version: a deploy that changes them makes a new service worker
            body = f.read().replace(b"__SHELL_VERSION__", shell_version().encode())
        return Response(body, media_type="text/javascript", headers=page_headers)

    @functools.lru_cache(maxsize=1)
    def shell_version() -> str:
        d = hashlib.sha256()
        for n in ("app.html", "app.js", "relay.css", "fuse.js", "icon.svg", "icon-192.png", "sw.js"):
            try:
                with open(os.path.join(web_dir, n), "rb") as f:
                    d.update(f.read())
            except OSError:
                pass
        return d.hexdigest()[:16]

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        """What a phone needs to put Relay on its home screen like an app."""
        return Response(json.dumps({
            "name": "Relay", "short_name": "Relay", "description": "Your AIs, passing work to each other.",
            "start_url": "/app", "scope": "/", "display": "standalone",
            "background_color": "#f6f4ef", "theme_color": "#f6f4ef",
            "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"},
                      {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"}]}),
            media_type="application/manifest+json", headers={"Cache-Control": "public, max-age=3600"})

    @app.get("/welcome", include_in_schema=False)
    def welcome_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=308)

    # The one-line installers carry this hub's own address, so the line people paste needs
    # only the invite code. The address comes from the request (the deploy runs uvicorn with
    # --proxy-headers, so it is https behind Railway) and must look like a plain origin before
    # it goes anywhere near a shell.
    origin_re = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
    installers = ("install.sh", "install.ps1", "update-helpers.py")

    def installer(name: str, request: Request) -> Response:
        origin = f"{request.url.scheme}://{request.url.netloc}"
        if not origin_re.match(origin):
            raise HTTPException(400, "unexpected host")
        if name not in web_files:
            raise HTTPException(404, "not found")
        with open(os.path.join(web_dir, name), encoding="utf-8") as f:
            body = f.read().replace("__RELAY_HUB__", origin)
        return Response(body, media_type=web_types[os.path.splitext(name)[1]], headers=page_headers)

    @app.get("/install.sh", include_in_schema=False)
    def install_sh(request: Request):
        return installer("install.sh", request)

    @app.get("/install.ps1", include_in_schema=False)
    def install_ps1(request: Request):
        return installer("install.ps1", request)

    @app.get("/update-helpers.py", include_in_schema=False)
    def update_helpers(request: Request):      # one script that updates every helper on a computer
        return installer("update-helpers.py", request)

    @app.get("/static/{name}", include_in_schema=False)
    def static_file(name: str, request: Request):   # only files shipped in relay/web; the name is a dict key
        return installer(name, request) if name in installers else web(name)

    @app.get("/cli.py", include_in_schema=False)
    def cli_py():              # public: it is the open client, and holds no secrets
        return static("cli.py", "text/x-python; charset=utf-8")

    @app.get("/client", include_in_schema=False)
    def client_version():
        """Which helper this hub serves, so an installed helper can tell it is out of date and update
        itself (cli.py checks this, then downloads /cli.py and verifies it against this sha256)."""
        try:
            with open(os.path.join(here, "cli.py"), "rb") as f:
                data = f.read()
        except FileNotFoundError:
            raise HTTPException(404, "this hub serves no client")
        m = re.search(rb"^HELPER_RELEASE = (\d+)$", data, re.M)
        try:            # the owner's signature for this release, made on their own computer (cli.py release sign)
            with open(os.path.join(here, "cli.sig.json")) as f:
                signed = json.load(f) or None
        except (OSError, ValueError):
            signed = None
        return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": "/cli.py",
                "release": int(m.group(1)) if m else None, "signed": signed}

    @app.get("/health")
    def health(response: Response):
        """Honest: not ok when the disk is almost full (the database lives on it). For monitors."""
        free, total = media_mod.disk(store.dir)
        ok = free >= DISK_LOW * total
        if not ok:
            response.status_code = 503
        return {"ok": ok, "version": app.version, "disk": "ok" if ok else "nearly full"}

    @app.get("/health/live", include_in_schema=False)
    def live():
        """The process answers. Deploys check this one, so a fix can still go out when the disk is full."""
        return {"ok": True, "version": app.version}

    # ---------------------------------------------------------------- agents
    @app.post("/agents")
    def register(body: AgentIn, me: dict = Depends(auth)):
        need_admin(me)
        check_new_agent(body.handle, body.role)
        provider = check_provider(body.provider)
        token = "rly_" + secrets.token_urlsafe(32)
        with store.tx() as c:
            if c.execute("SELECT 1 FROM agents WHERE handle=?", (body.handle,)).fetchone():
                raise HTTPException(409, f"@{body.handle} already registered")
            c.execute("INSERT INTO agents(handle,token_hash,caps,role,created,provider) VALUES (?,?,?,?,?,?)",
                      (body.handle, _hash(token), json.dumps(sorted(set(body.caps))),
                       body.role, _now(), provider))
            c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (body.handle,))
        # the only time this token is ever returned
        return {"handle": body.handle, "token": token, "caps": sorted(set(body.caps)),
                "role": body.role}

    @app.post("/owner")
    def become_owner(body: OwnerIn, me: dict = Depends(auth)):
        """Admin token only, once per person: make `handle` (a person) the hub's owner and return
        their own key. From then on they sign in as themselves and the admin token can go back
        in the drawer. Re-running it for the same person issues them a fresh key."""
        if me["role"] != "admin":
            raise HTTPException(403, "use the admin token for this")
        check_new_agent(body.handle, "human")
        token = "rly_" + secrets.token_urlsafe(32)
        with store.tx() as c:
            r = c.execute("SELECT role FROM agents WHERE handle=?", (body.handle,)).fetchone()
            if r and r["role"] != "human":
                raise HTTPException(409, f"@{body.handle} is an agent; pick your own name")
            if r:
                c.execute("UPDATE agents SET token_hash=?, owner=1 WHERE handle=?", (_hash(token), body.handle))
            else:
                c.execute("INSERT INTO agents(handle,token_hash,caps,role,created,owner) VALUES (?,?,?,?,?,1)",
                          (body.handle, _hash(token), "[]", "human", _now()))
                c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (body.handle,))
        return {"handle": body.handle, "token": token, "owner": True}

    @app.delete("/agents/{handle}")
    def remove_agent(handle: str, me: dict = Depends(auth)):
        """Admin: remove an agent. Its key stops working at once; its past messages stay."""
        need_admin(me)
        with store.tx() as c:
            if not c.execute("DELETE FROM agents WHERE handle=?", (handle,)).rowcount:
                raise HTTPException(404, f"no agent @{handle}")
            for table, col in (("subs", "agent"), ("follows", "agent"), ("cursors", "agent"),
                               ("contacts", "owner"), ("contacts", "address"), ("wakes", "handle"),
                               ("people_emails", "handle"), ("sessions", "handle"), ("agent_keys", "handle")):
                c.execute(f"DELETE FROM {table} WHERE {col}=?", (handle,))
            c.execute("UPDATE jobs SET status='queued', claimed_by=NULL, lease_until=NULL"
                      " WHERE claimed_by=? AND status='claimed'", (handle,))
        return {"removed": handle}

    @app.put("/agents/{handle}/provider")
    def set_provider(handle: str, body: dict, me: dict = Depends(auth)):
        """Admin: say which provider an agent runs on (display only)."""
        need_admin(me)
        provider = check_provider(body.get("provider"))
        with store.tx() as c:
            if not c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone():
                raise HTTPException(404, f"no agent @{handle}")
            # the owner's own choice is final: a detected app never overrides it
            c.execute("UPDATE agents SET provider=?, provider_source='owner' WHERE handle=?", (provider, handle))
        return {"handle": handle, "provider": provider}

    # ---------------------------------------------------------------- wake-ups
    def wake_target(handle: str, me: dict) -> str:
        if handle == "me":
            need_agent(me)
            return me["handle"]
        need_admin(me)
        with store.ro() as c:
            if not c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone():
                raise HTTPException(404, f"no agent @{handle}")
        return handle

    @app.put("/agents/{handle}/wake")
    def set_wake(handle: str, body: WakeIn, me: dict = Depends(auth)):
        h = wake_target(handle, me)
        try:
            return waker.configure(h, body.kind, body.url, body.token)
        except WakeError as e:
            raise HTTPException(400, str(e))

    @app.get("/agents/{handle}/wake")
    def get_wake(handle: str, me: dict = Depends(auth)):
        return waker.get(wake_target(handle, me)) or {"kind": None}

    @app.delete("/agents/{handle}/wake")
    def delete_wake(handle: str, me: dict = Depends(auth)):
        return {"removed": waker.remove(wake_target(handle, me))}

    @app.post("/agents/{handle}/wake/test")
    def test_wake(handle: str, me: dict = Depends(auth)):
        h = wake_target(handle, me)
        return {"status": waker.fire_now(h), **(waker.get(h) or {})}

    @app.post("/agents/{handle}/token")
    def reset_token(handle: str, me: dict = Depends(auth)):
        """Admin: issue a new token for an agent. The old one stops working at once."""
        need_admin(me)
        token = "rly_" + secrets.token_urlsafe(32)
        with store.tx() as c:
            if not c.execute("SELECT 1 FROM agents WHERE handle=?", (handle,)).fetchone():
                raise HTTPException(404, f"no agent @{handle}")
            c.execute("UPDATE agents SET token_hash=? WHERE handle=?", (_hash(token), handle))
            c.execute("DELETE FROM agent_keys WHERE handle=?", (handle,))   # every old key, not just the first
        return {"handle": handle, "token": token}

    # ---------------------------------------------------------------- invites
    # An invite lets a new agent join without anyone handling its token: the admin
    # makes a one-time code, the agent's own machine trades it for a token
    # (`cli.py join`), and the token never passes through a browser or a chat.
    @app.post("/invites")
    def new_invite(body: InviteIn, me: dict = Depends(auth)):
        need_admin(me)
        if not 0.1 <= body.hours <= 168:
            raise HTTPException(400, "hours must be between 0.1 and 168")
        code = "inv_" + secrets.token_urlsafe(24)
        iid = "i_" + uuid.uuid4().hex[:12]
        now = _now()
        if body.rejoin:
            # The agent already exists and keeps its key (an app may be using it); claiming this adds a
            # second key for it, e.g. for the Relay helper on another computer. Only the newest code works.
            with store.tx() as c:
                a = c.execute("SELECT * FROM agents WHERE handle=?", (body.handle,)).fetchone()
                if not a:
                    raise HTTPException(404, f"no agent @{body.handle}")
                if a["role"] != "agent":
                    raise HTTPException(400, "people sign in for themselves")
                c.execute("UPDATE invites SET revoked=? WHERE handle=? AND rejoin=1 AND claimed IS NULL"
                          " AND revoked IS NULL", (now, body.handle))
                c.execute("INSERT INTO invites(id,code_hash,handle,role,caps,provider,note,created,expires,rejoin)"
                          " VALUES (?,?,?,?,?,?,?,?,?,1)",
                          (iid, _hash(code), a["handle"], a["role"], a["caps"], a["provider"], body.note, now,
                           now + body.hours * 3600))
                out = invite_out(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())
            out["code"] = code
            return out
        check_new_agent(body.handle, body.role)
        provider = check_provider(body.provider)
        with store.tx() as c:
            if c.execute("SELECT 1 FROM agents WHERE handle=?", (body.handle,)).fetchone():
                raise HTTPException(409, f"@{body.handle} already exists")
            if c.execute("SELECT 1 FROM invites WHERE handle=? AND claimed IS NULL AND revoked IS NULL"
                         " AND expires>?", (body.handle, now)).fetchone():
                raise HTTPException(409, f"@{body.handle} already has an open invite; revoke it first")
            c.execute("INSERT INTO invites(id,code_hash,handle,role,caps,provider,note,created,expires)"
                      " VALUES (?,?,?,?,?,?,?,?,?)",
                      (iid, _hash(code), body.handle, body.role,
                       json.dumps(sorted(set(body.caps))), provider, body.note, now,
                       now + body.hours * 3600))
            out = invite_out(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())
        out["code"] = code          # the only time the code is ever returned
        return out

    @app.get("/invites")
    def list_invites(me: dict = Depends(auth)):
        need_admin(me)
        with store.ro() as c:
            return [invite_out(r) for r in
                    c.execute("SELECT * FROM invites ORDER BY created DESC LIMIT 100")]

    @app.delete("/invites/{iid}")
    def revoke_invite(iid: str, me: dict = Depends(auth)):
        need_admin(me)
        with store.tx() as c:
            r = c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone()
            if not r:
                raise HTTPException(404, "no such invite")
            if r["claimed"]:
                raise HTTPException(409, "already used; reset the agent's token instead")
            c.execute("UPDATE invites SET revoked=COALESCE(revoked, ?) WHERE id=?", (_now(), iid))
            return invite_out(c.execute("SELECT * FROM invites WHERE id=?", (iid,)).fetchone())

    @app.post("/invites/peek")
    def peek_invite(body: ClaimInviteIn):
        """No auth: what an invite is for, so the join page can show it. Does not use it up."""
        with store.ro() as c:
            r = c.execute("SELECT * FROM invites WHERE code_hash=?", (_hash(body.code.strip()),)).fetchone()
        if not r:
            raise HTTPException(404, "invite is not valid: it was used, revoked, expired or mistyped")
        out = invite_out(r)
        with store.ro() as c:          # who runs this hub, so the page can say who invited you
            host = c.execute("SELECT handle FROM agents WHERE owner=1 ORDER BY created LIMIT 1").fetchone()
        return {"handle": out["handle"], "role": out["role"], "caps": out["caps"],
                "provider": out["provider"], "expires": out["expires"], "state": out["state"],
                "host": host["handle"] if host else None}

    @app.post("/invites/claim")
    def claim_invite(body: ClaimInviteIn):
        """No auth: the code is the credential. Single use, checked in one transaction."""
        now = _now()
        with store.tx() as c:
            r = c.execute("SELECT * FROM invites WHERE code_hash=?", (_hash(body.code.strip()),)).fetchone()
            if not r or r["claimed"] or r["revoked"] or r["expires"] <= now:
                raise HTTPException(404, "invite is not valid: it was used, revoked, expired or mistyped")
            token = "rly_" + secrets.token_urlsafe(32)
            if r["rejoin"]:                    # one more key for an existing agent; its other keys keep working
                a = c.execute("SELECT * FROM agents WHERE handle=?", (r["handle"],)).fetchone()
                if not a:
                    raise HTTPException(404, "invite is not valid: that agent was removed")
                c.execute("INSERT INTO agent_keys(token_hash,handle,created) VALUES (?,?,?)",
                          (_hash(token), a["handle"], now))
                c.execute("UPDATE invites SET claimed=? WHERE id=?", (now, r["id"]))
                return {"handle": a["handle"], "token": token, "role": a["role"], "caps": _loads(a["caps"], [])}
            if c.execute("SELECT 1 FROM agents WHERE handle=?", (r["handle"],)).fetchone():
                raise HTTPException(409, f"@{r['handle']} already exists")
            c.execute("INSERT INTO agents(handle,token_hash,caps,role,created,provider) VALUES (?,?,?,?,?,?)",
                      (r["handle"], _hash(token), r["caps"], r["role"], now, r["provider"]))
            c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (r["handle"],))
            c.execute("UPDATE invites SET claimed=? WHERE id=?", (now, r["id"]))
            post(c, SYSTEM, f"@{r['handle']} joined the hub", channel="general")
        return {"handle": r["handle"], "token": token, "role": r["role"],
                "caps": _loads(r["caps"], [])}

    @app.get("/agents")
    def agents(me: dict = Depends(auth)):
        sees_emails = me["role"] == "admin" or me.get("owner")
        with store.ro() as c:
            emails = {r["handle"]: r["email"] for r in c.execute("SELECT handle, email FROM people_emails")
                      if sees_emails or r["handle"] == me["handle"]}      # everyone sees their own
            try:
                woken = {r["handle"] for r in c.execute("SELECT handle FROM wakes")}
            except sqlite3.Error:
                woken = set()

            def answers(r) -> str:
                """person, instantly (a wake-up or a listening helper) or when_asked (a chat app)."""
                if r["role"] == "human":
                    return "person"
                listening = r["listener_seen"] and _now() - r["listener_seen"] < 150
                return "instantly" if r["handle"] in woken or listening else "when_asked"
            rows = c.execute("SELECT * FROM agents ORDER BY handle").fetchall()
            boss = first_owner(c)
            joined = {r["handle"]: r["created"] for r in rows if r["role"] == "human"}

            def works_for(r):            # whose agent it is: the person who connected it, else the hub's owner
                if r["role"] != "agent":
                    return None
                return r["person"] if r["person"] in joined else boss
            return [{"handle": r["handle"], "caps": _loads(r["caps"], []), "role": r["role"],
                     "provider": r["provider"], "client": r["client"], "last_seen": r["last_seen"],
                     "owner": bool(r["owner"]), "listener": listener_out(r),
                     "email": emails.get(r["handle"]), "answers": answers(r), "person": works_for(r),
                     "joined": joined.get(r["handle"])}
                    for r in rows]

    @app.get("/agents/me")
    def whoami(me: dict = Depends(auth)):
        return me

    def listener_out(r: sqlite3.Row) -> Optional[dict]:
        if not r["listener_seen"]:
            return None
        return {"app": r["listener_app"], "seen": r["listener_seen"], "woke": r["listener_woke"]}

    @app.put("/agents/me/listener")
    def set_listener(body: ListenerIn, me: dict = Depends(auth)):
        """The Relay helper on the agent's own computer says it is running (and when it last
        woke its app), so the owner can see it. Long-polls of the inbox keep `seen` fresh."""
        need_agent(me)
        if body.app is not None and body.app not in ("codex", "claude-code"):
            raise HTTPException(400, "app must be codex or claude-code")
        now = _now()
        woke = min(body.woke, now) if body.woke else None
        with store.tx() as c:
            c.execute("UPDATE agents SET listener_app=?, listener_seen=?,"
                      " listener_woke=COALESCE(?, listener_woke) WHERE handle=?",
                      (body.app, now, woke, me["handle"]))
            return listener_out(c.execute("SELECT * FROM agents WHERE handle=?", (me["handle"],)).fetchone())

    @app.put("/agents/me/caps")
    def set_caps(body: CapsIn, me: dict = Depends(auth)):
        need_agent(me)
        caps = sorted(set(body.caps))
        with store.tx() as c:
            c.execute("UPDATE agents SET caps=? WHERE handle=?", (json.dumps(caps), me["handle"]))
        return {"handle": me["handle"], "caps": caps}

    # ---------------------------------------------------------------- messages
    @app.post("/messages")
    def send(body: MessageIn, me: dict = Depends(auth), x_relay_session: str = Header(default="")):
        need_agent(me)
        session = check_session(x_relay_session)
        if not session:
            no_session(me["handle"], "sent a message")
        for m in body.media:            # only media this agent may see can travel with its messages
            try:
                media_svc.check_access(me["handle"], m)
            except media_mod.MediaError as e:
                raise HTTPException(400, str(e)) from None
        with store.tx() as c:
            return post(c, me["handle"], body.text, channel=body.channel,
                        reply_to=body.reply_to, mentions=body.mentions,
                        artifacts=body.artifacts, idem=body.idem, session=session, media=body.media)

    @app.get("/messages")
    def read(channel: Optional[str] = None, thread: Optional[int] = None,
             after: int = 0, before: int = 0, limit: int = Query(50, ge=1, le=200),
             latest: bool = False, me: dict = Depends(auth)):
        """Oldest first from `after`; with latest=true, the newest `limit` (still oldest first).
        `before`: only messages older than that id, so a long conversation can be read back a page at a time."""
        q, a = "SELECT * FROM messages m WHERE id>?", [after]
        if before:
            q += " AND id<?"; a.append(before)
        if channel:
            q += " AND channel=?"; a.append(channel)
        if thread:
            q += " AND thread=?"; a.append(thread)
        with store.ro() as c:
            only, more = visible(c, me["handle"])          # only conversations my circle is in
            q += f" AND {only}"; a += more
            q += " ORDER BY id DESC LIMIT ?" if latest else " ORDER BY id LIMIT ?"; a.append(limit)
            rows = [msg_out(r) for r in c.execute(q, a)]
            files: dict = {}                            # the files each message carries, for the cards people see
            if rows:
                ids = [m["id"] for m in rows]
                for r in c.execute(f"SELECT msg, media FROM message_media WHERE msg IN ({','.join('?' * len(ids))})"
                                   " ORDER BY rowid", ids):
                    files.setdefault(r["msg"], []).append(r["media"])
            read: dict = {}
            if rows:
                for r in c.execute(f"SELECT msg, handle, time FROM reads WHERE msg IN ({','.join('?' * len(ids))})", ids):
                    read.setdefault(r["msg"], {})[r["handle"]] = r["time"]
        for m in rows:
            m["media"] = [media_svc.brief(x) for x in files.get(m["id"], [])]
            m["read"] = read.get(m["id"], {})
        if thread and rows and me["role"] == "human":       # a person looking at a conversation has seen it
            top = max(m["id"] for m in rows)
            with store.ro() as c:
                was = c.execute("SELECT last FROM seen WHERE handle=? AND thread=?", (me["handle"], thread)).fetchone()
            if not was or was["last"] < top:                # only when it moved: polls don't take the write lock
                with store.tx() as c:
                    c.execute("INSERT INTO seen(handle,thread,last) VALUES (?,?,?) ON CONFLICT(handle,thread)"
                              " DO UPDATE SET last=MAX(last, excluded.last)", (me["handle"], thread, top))
        return rows[::-1] if latest else rows

    @app.get("/threads")
    def threads(limit: int = Query(40, ge=1, le=200), before: int = 0, with_: Optional[str] = Query(None, alias="with", max_length=64),
                since: float = 0, until: float = 0, me: dict = Depends(auth)):
        """The conversations I may see, the most recently active first: each one's newest message, how many
        messages it has and who has been in it. `before` (a newest-message id) gives the next page."""
        with store.ro() as c:
            only, more = visible(c, me["handle"])
            q = f"SELECT t.thread, t.n, m.* FROM convs t JOIN messages m ON m.id=t.last WHERE {only}"
            a = list(more)
            marks = {r["thread"]: r for r in c.execute("SELECT * FROM marks WHERE handle=?", (me["handle"],))}
            pins = sorted((t for t, r in marks.items() if r["pinned"]), key=lambda t: marks[t]["pinned"])
            if pins:                           # pinned ones come first, on the first page only, and never twice
                q += f" AND t.thread NOT IN ({','.join('?' * len(pins))})"; a += pins
            if before:
                q += " AND t.last<?"; a.append(before)
            if with_:                          # only conversations this agent or person has been in
                q += " AND t.thread IN (SELECT thread FROM members WHERE agent=?)"; a.append(with_.lstrip("@"))
            if since:                          # last active from this time (a date filter)
                q += " AND m.time>=?"; a.append(since)
            if until:                          # ...and before this one
                q += " AND m.time<?"; a.append(until)
            rows = c.execute(q + " ORDER BY t.last DESC LIMIT ?", a + [limit]).fetchall()
            if pins and not before:
                pq = q.replace(f" AND t.thread NOT IN ({','.join('?' * len(pins))})", f" AND t.thread IN ({','.join('?' * len(pins))})", 1)
                top = {r["thread"]: r for r in c.execute(pq, a)}
                rows = [top[t] for t in pins if t in top] + rows
            ids = [r["thread"] for r in rows]
            people: dict = {}
            first: dict = {}
            seen: dict = {}
            if ids:
                qs = ",".join("?" * len(ids))
                for r in c.execute(f"SELECT thread, agent FROM members WHERE thread IN ({qs}) ORDER BY rowid", ids):
                    people.setdefault(r["thread"], []).append(r["agent"])
                for r in c.execute(f"SELECT id, sender, text FROM messages WHERE id IN ({qs})", ids):
                    first[r["id"]] = {"from": r["sender"], "text": r["text"]}
                for r in c.execute(f"SELECT thread, last FROM seen WHERE handle=? AND thread IN ({qs})", [me["handle"]] + ids):
                    seen[r["thread"]] = r["last"]
            floor = int((c.execute("SELECT value FROM meta WHERE key='seen_floor'").fetchone() or {"value": 0})["value"])

        def unread(r) -> bool:
            if r["sender"] in (me["handle"], SYSTEM):
                return False                     # the newest is yours, or the hub's own note ("ama joined")
            return r["id"] > seen.get(r["thread"], floor)
        return [{"thread": r["thread"], "count": r["n"], "people": people.get(r["thread"], []), "last": msg_out(r),
                 "first": first.get(r["thread"]), "unread": unread(r),
                 "pinned": bool(r["thread"] in marks and marks[r["thread"]]["pinned"]),
                 "muted": bool(r["thread"] in marks and marks[r["thread"]]["muted"])} for r in rows]

    # ---------------------------------------------------------------- live: the app hears about changes at once
    # One request at a time per open app, answered the moment something that person would see changes, instead
    # of the app asking for everything every few seconds. It waits on the event loop, not a worker thread, so a
    # hundred open apps hold no threads.
    live_waiters: set = set()                    # (its event loop, its event): each woken on its own loop

    def live_wake() -> None:
        for loop, ev in list(live_waiters):
            try:
                loop.call_soon_threadsafe(ev.set)
            except RuntimeError:                 # that loop has closed
                live_waiters.discard((loop, ev))
    store.on_write.append(live_wake)

    def live_state(me: dict) -> str:
        """A short fingerprint of what this person sees: the newest message in a conversation they can see,
        who has read what, what waits for their OK, people asking to join (the owner), their pins and mutes."""
        h = me["handle"]
        with store.ro() as c:
            sql, a = visible(c, h)
            last = c.execute(f"SELECT MAX(t.last) FROM convs t JOIN messages m ON m.id=t.last WHERE {sql}", a).fetchone()[0] or 0
            reads = c.execute("SELECT MAX(rowid) FROM reads").fetchone()[0] or 0
            marks = c.execute("SELECT COUNT(*), COALESCE(SUM(pinned), 0), COALESCE(SUM(muted), 0) FROM marks WHERE handle=?",
                              (h,)).fetchone()
            owner = first_owner(c) == h
            asks = c.execute("SELECT COUNT(*), COALESCE(MAX(created), 0) FROM vault_calls WHERE owner=? AND status='waiting'",
                             (h,)).fetchone() if me["role"] == "human" else (0, 0)
            inv = c.execute("SELECT COUNT(*), COALESCE(MAX(created), 0) FROM invite_requests WHERE status='waiting'").fetchone() \
                if owner else (0, 0)
        raw = f"{last}|{reads}|{tuple(marks)}|{tuple(asks)}|{tuple(inv)}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @app.get("/changes")
    async def changes(since: str = Query("", max_length=32), wait: float = Query(25, ge=0, le=30), me: dict = Depends(auth)):
        """Answers as soon as something you'd see has changed since `since` (or after `wait` seconds) with
        the new `state`; pass that back next time. The first call (no `since`) answers at once."""
        loop = asyncio.get_running_loop()
        deadline = time.monotonic() + wait
        while True:
            woke = asyncio.Event()
            live_waiters.add((loop, woke))       # listening before looking: a write in between isn't missed
            try:
                state = await asyncio.to_thread(live_state, me)
                left = deadline - time.monotonic()
                if state != since or left <= 0:
                    return {"state": state}
                try:
                    await asyncio.wait_for(woke.wait(), timeout=left)
                except asyncio.TimeoutError:
                    pass
                await asyncio.sleep(0.25)         # a burst of writes (an agent streaming) is looked at once
            finally:
                live_waiters.discard((loop, woke))

    PINS_EACH = 10

    @app.put("/threads/{thread}/mark")
    def mark_thread(thread: int, body: MarkIn, me: dict = Depends(auth)):
        """Pin a conversation to the top of your list, or mute it: no notifications, not counted as new.
        Only for you; nobody else sees it."""
        if me["role"] != "human":
            raise HTTPException(403, "Pinning and muting are for people.")
        h = me["handle"]
        with store.tx() as c:
            if not c.execute("SELECT 1 FROM convs WHERE thread=?", (thread,)).fetchone() or not can_see(c, h, thread):
                raise HTTPException(404, "no such conversation")
            r = c.execute("SELECT * FROM marks WHERE handle=? AND thread=?", (h, thread)).fetchone()
            pinned, muted = (r["pinned"], r["muted"]) if r else (None, 0)
            if body.pinned is not None:
                if body.pinned and not pinned:
                    if c.execute("SELECT COUNT(*) FROM marks WHERE handle=? AND pinned IS NOT NULL", (h,)).fetchone()[0] >= PINS_EACH:
                        raise HTTPException(429, f"You can pin {PINS_EACH} conversations. Unpin one first.")
                    pinned = _now()
                elif not body.pinned:
                    pinned = None
            if body.muted is not None:
                muted = 1 if body.muted else 0
            if pinned is None and not muted:
                c.execute("DELETE FROM marks WHERE handle=? AND thread=?", (h, thread))
            else:
                c.execute("INSERT INTO marks(handle,thread,pinned,muted) VALUES (?,?,?,?) ON CONFLICT(handle,thread)"
                          " DO UPDATE SET pinned=excluded.pinned, muted=excluded.muted", (h, thread, pinned, muted))
        return {"thread": thread, "pinned": pinned is not None, "muted": bool(muted)}

    # ---------------------------------------------------------------- shortcuts: a hand-off again, in one tap
    SHORTCUTS_EACH = 30

    def shortcut_out(r) -> dict:
        return {"id": r["id"], "title": r["title"], "text": r["text"], "mentions": _loads(r["mentions"], []),
                "created": r["created"], "used": r["used"], "uses": r["uses"]}

    @app.get("/shortcuts")
    def list_shortcuts(me: dict = Depends(auth)):
        with store.ro() as c:
            return [shortcut_out(r) for r in c.execute(
                "SELECT * FROM shortcuts WHERE handle=? ORDER BY COALESCE(used, created) DESC", (me["handle"],))]

    @app.post("/shortcuts")
    def save_shortcut(body: ShortcutIn, me: dict = Depends(auth)):
        if me["role"] != "human":
            raise HTTPException(403, "Shortcuts are for people.")
        who = sorted({m.lstrip("@") for m in body.mentions})
        with store.tx() as c:
            for m in who:
                if not c.execute("SELECT 1 FROM agents WHERE handle=?", (m,)).fetchone():
                    raise HTTPException(400, f"unknown @{m}")
            if c.execute("SELECT COUNT(*) n FROM shortcuts WHERE handle=?", (me["handle"],)).fetchone()["n"] >= SHORTCUTS_EACH:
                raise HTTPException(429, f"That's {SHORTCUTS_EACH} shortcuts. Remove one first.")
            sid = "sc_" + secrets.token_hex(6)
            title = body.title.strip() or body.text.strip().splitlines()[0][:80]
            c.execute("INSERT INTO shortcuts(id,handle,title,text,mentions,created) VALUES (?,?,?,?,?,?)",
                      (sid, me["handle"], title, body.text.strip(), json.dumps(who), _now()))
            return shortcut_out(c.execute("SELECT * FROM shortcuts WHERE id=?", (sid,)).fetchone())

    @app.delete("/shortcuts/{sid}")
    def remove_shortcut(sid: str, me: dict = Depends(auth)):
        with store.tx() as c:
            if not c.execute("DELETE FROM shortcuts WHERE id=? AND handle=?", (sid, me["handle"])).rowcount:
                raise HTTPException(404, "no such shortcut")
        return {"removed": sid}

    @app.post("/shortcuts/{sid}/run")
    def run_shortcut(sid: str, me: dict = Depends(auth)):
        """Send it again: a new conversation with the same words to the same agents."""
        with store.tx() as c:
            r = c.execute("SELECT * FROM shortcuts WHERE id=? AND handle=?", (sid, me["handle"])).fetchone()
            if not r:
                raise HTTPException(404, "no such shortcut")
            who = [m for m in _loads(r["mentions"], []) if c.execute("SELECT 1 FROM agents WHERE handle=?", (m,)).fetchone()]
            m = post(c, me["handle"], r["text"], channel=DIRECT, mentions=who)
            c.execute("UPDATE shortcuts SET used=?, uses=uses+1 WHERE id=?", (_now(), sid))
        return m

    @app.get("/search")
    def search(q: str = Query("", max_length=200), limit: int = Query(20, ge=1, le=50), since: float = 0, until: float = 0,
               me: dict = Depends(auth)):
        """Conversations you may see that match: by what was said in them, who is in them, or a file's name.
        Each comes with up to three matching lines, the words found marked with \\x02 and \\x03."""
        q = q.strip()
        words = re.findall(r"\w+", q.lower())[:6]
        if len(q) < 2 or not words:
            return []
        found: dict = {}                        # thread -> {"hits": [...], "why": set()}

        def add(thread, why, hit=None):
            f = found.setdefault(thread, {"hits": [], "why": set()})
            f["why"].add(why)
            if hit and len(f["hits"]) < 3:
                f["hits"].append(hit)

        def esc(w: str) -> str:
            return w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        def mark(text: str) -> str:             # the plain-scan fallback marks words the way FTS5's snippet does
            line = next((l for l in text.splitlines() if any(w in l.lower() for w in words)), text)[:200]
            return re.sub("(" + "|".join(re.escape(w) for w in words) + ")", "\x02\\1\x03", line, flags=re.I)
        with store.ro() as c:
            only, more = visible(c, me["handle"])
            if store.fts:
                match = " AND ".join(f'"{w}"*' for w in words)
                # newest first straight from the index (cheap), then who may see them, then the marked words
                # only for the lines that will be shown (snippets cost far more than finding)
                ids = [r[0] for r in c.execute("SELECT rowid FROM msg_fts WHERE msg_fts MATCH ? ORDER BY rowid DESC LIMIT 2000", (match,))]
                rows = []
                if ids:
                    when = (" AND m.time>=?" if since else "") + (" AND m.time<?" if until else "")
                    cand = c.execute(f"SELECT m.id, m.thread, m.sender, m.time FROM messages m WHERE m.id IN ({','.join('?' * len(ids))})"
                                     f" AND {only}{when} ORDER BY m.id DESC", ids + more + [x for x in (since, until) if x]).fetchall()
                    per, keep = {}, []
                    for r in cand:
                        n = per.get(r["thread"], 0)
                        if n < 3 and (n or len(per) < limit):
                            per[r["thread"]] = n + 1
                            keep.append(r)
                    snips = {}
                    if keep:
                        snips = {r[0]: r[1] for r in c.execute(
                            f"SELECT rowid, snippet(msg_fts, 0, char(2), char(3), '…', 14) FROM msg_fts"
                            f" WHERE msg_fts MATCH ? AND rowid IN ({','.join('?' * len(keep))})", [match] + [r["id"] for r in keep])}
                    rows = [{"id": r["id"], "thread": r["thread"], "sender": r["sender"], "time": r["time"], "snip": snips.get(r["id"], "")}
                            for r in keep]
            else:
                rows = c.execute(f"SELECT m.id, m.thread, m.sender, m.time, m.text AS snip FROM messages m WHERE {only}"
                                 + "".join(" AND m.text LIKE ? ESCAPE '\\'" for _ in words) + " ORDER BY m.id DESC LIMIT 300",
                                 more + ["%" + esc(w) + "%" for w in words]).fetchall()
            for r in rows:
                add(r["thread"], "said", {"id": r["id"], "from": r["sender"], "time": r["time"],
                                          "text": r["snip"] if store.fts else mark(r["snip"])})
            like = "%" + esc(q.lower()) + "%"
            for r in c.execute(f"SELECT DISTINCT mb.thread FROM members mb JOIN messages m ON m.id=mb.thread"
                               f" WHERE lower(mb.agent) LIKE ? ESCAPE '\\' AND {only} LIMIT 200", [like] + more):
                add(r["thread"], "with")
            for r in c.execute(f"SELECT m.id, m.thread, m.sender, m.time, x.name FROM messages m JOIN message_media mm ON mm.msg=m.id"
                               f" JOIN media x ON x.id=mm.media WHERE lower(x.name) LIKE ? ESCAPE '\\' AND {only} LIMIT 200",
                               [like] + more):
                add(r["thread"], "file", {"id": r["id"], "from": r["sender"], "time": r["time"], "file": r["name"]})
            if not found:
                return []
            ids = list(found)
            qs = ",".join("?" * len(ids))
            convs = {r["thread"]: r for r in c.execute(f"SELECT t.thread, t.n, m.* FROM convs t JOIN messages m ON m.id=t.last"
                                                         f" WHERE t.thread IN ({qs})", ids)}
            people: dict = {}
            for r in c.execute(f"SELECT thread, agent FROM members WHERE thread IN ({qs}) ORDER BY rowid", ids):
                people.setdefault(r["thread"], []).append(r["agent"])
            first = {r["id"]: {"from": r["sender"], "text": r["text"]}
                     for r in c.execute(f"SELECT id, sender, text FROM messages WHERE id IN ({qs})", ids)}
        out = [{"thread": t, "count": convs[t]["n"], "people": people.get(t, []), "first": first.get(t), "last": msg_out(convs[t]),
                "why": sorted(f["why"]), "hits": f["hits"]} for t, f in found.items() if t in convs]
        if since or until:                     # people and file matches keep to the dates too
            inside = lambda t: (not since or t >= since) and (not until or t < until)
            out = [x for x in out if any(inside(h["time"]) for h in x["hits"]) or (not x["hits"] and inside(x["last"]["time"]))]
        newest = lambda x: max([h["id"] for h in found[x["thread"]]["hits"]] or [x["last"]["id"]])
        out.sort(key=newest, reverse=True)
        return out[:limit]

    @app.get("/inbox")
    def inbox(limit: int = Query(20, ge=1, le=200), wait: float = Query(0, ge=0, le=50),
              unclaimed: bool = False, me: dict = Depends(auth), x_relay_session: str = Header(default="")):
        """What is waiting for me: mentions, my channels, threads I follow.
        At-least-once: nothing is consumed until I ack the cursor. With wait=N the request
        holds for up to N seconds until something arrives (long-poll), so a listener hears a
        message within about a second instead of at its next check.
        X-Relay-Session: only this chat's messages and new requests no other chat has taken; reading
        them makes them this chat's. unclaimed=true (a listener that starts new chats): only messages
        no chat owns, so an answer meant for a chat that already exists never starts a new one."""
        need_agent(me)
        session = check_session(x_relay_session)
        if not session and not unclaimed and not wait:
            no_session(me["handle"], "read its inbox")
        with store.tx() as c:
            if wait:                   # a listener: say it is still there (only one that reported in)
                c.execute("UPDATE agents SET listener_seen=? WHERE handle=? AND listener_seen IS NOT NULL",
                          (_now(), me["handle"]))
            else:                      # the agent reading (a long-poll is a listener keeping watch, not a read)
                mark_read(c, me["handle"])
        deadline = time.monotonic() + wait
        writes = bool(session) or not wait    # a plain long-poll only reads; keep it off the write lock
        while True:
            with (store.tx() if writes else store.ro()) as c:
                box = inbox_for(c, me["handle"], limit, session=session, unclaimed=unclaimed and not session)
                if box["messages"] and not wait:          # a reader gets each thread's earlier messages too
                    take(c, me["handle"], session, box["messages"])
                    with_context(c, box["messages"])
            seen_writes = store.writes                    # after our own write, or we'd wake ourselves
            if box["messages"] or time.monotonic() >= deadline:
                return box
            with store.changed:               # sleep until something is written (or 2 s pass), not a fixed tick
                store.changed.wait_for(lambda: store.writes != seen_writes,
                                       timeout=max(0.05, min(2.0, deadline - time.monotonic())))

    @app.post("/inbox/ack")
    def ack(body: AckIn, me: dict = Depends(auth), x_relay_session: str = Header(default="")):
        need_agent(me)
        session = check_session(x_relay_session)
        with store.tx() as c:
            ack_to(c, me["handle"], body.cursor, session)
        return {"ok": True, "cursor": body.cursor}

    @app.post("/sessions/end")
    def end_session(me: dict = Depends(auth), x_relay_session: str = Header(default="")):
        """The app behind this chat has exited (the Relay helper says so after each run). Whatever is
        still held for it goes back to the identity at once instead of waiting to go stale."""
        need_agent(me)
        session = check_session(x_relay_session)
        if not session:
            raise HTTPException(400, "send X-Relay-Session: the session that ended")
        with store.tx() as c:
            c.execute("UPDATE session_cursors SET ended=? WHERE handle=? AND session=?", (_now(), me["handle"], session))
            return {"ended": session, "released": release(c, me["handle"], session, everything=True)}

    def sweep() -> int:
        """Chats that were answering and went quiet: give their unread messages back to the identity."""
        with store.tx() as c:
            pairs = c.execute(
                "SELECT DISTINCT r.handle, r.session FROM routes r JOIN messages m ON m.id=r.msg"
                " JOIN messages root ON root.id=m.thread"
                " JOIN session_cursors s ON s.handle=r.handle AND s.session=r.session"
                " WHERE r.session!='' AND m.sender!=r.handle AND root.sender!=r.handle AND m.id>s.last"
                " AND (s.ended IS NOT NULL OR COALESCE(s.seen, 0) < ? - CASE WHEN EXISTS"
                " (SELECT 1 FROM wakes w WHERE w.handle=r.handle) THEN ? ELSE ? END)",
                (_now(), woken_idle, session_idle)).fetchall()
            n = sum(release(c, p["handle"], p["session"]) for p in pairs)
            prune(c)
            return n

    PRUNE_AFTER = 30 * 86400

    def prune(c) -> None:
        """Forget chats unheard of for 30 days that have nothing left waiting for them."""
        old = c.execute(
            "SELECT s.handle, s.session, s.last FROM session_cursors s WHERE COALESCE(s.seen, 0)<?"
            " AND NOT EXISTS (SELECT 1 FROM routes r JOIN messages m ON m.id=r.msg WHERE r.handle=s.handle"
            " AND r.session=s.session AND r.msg>s.last AND m.sender!=s.handle)", (_now() - PRUNE_AFTER,)).fetchall()
        for o in old:
            k = (o["handle"], o["session"])
            c.execute("DELETE FROM thread_sessions WHERE handle=? AND session=?", k)
            c.execute("DELETE FROM delivered WHERE handle=? AND session=?", k)
            c.execute("DELETE FROM session_cursors WHERE handle=? AND session=?", k)

    def sweeper():
        while True:
            time.sleep(session_sweep)
            try:
                sweep()
            except Exception as e:                 # never let routing upkeep take the hub down
                log.warning("session sweep failed: %s", e)

    import threading
    threading.Thread(target=sweeper, name="relay-session-sweep", daemon=True).start()

    # ---------------------------------------------------------------- backups (no paid plan needed; see backup.py)
    from . import backup as backup_mod
    from . import offsite as offsite_mod
    try:
        offsite = offsite_mod.Offsite.from_env()     # R2, B2 or any S3: a copy that survives losing this disk
    except ValueError as e:
        offsite = None
        log.warning("off-site backups are off: %s", e)
    if offsite and not os.environ.get("RELAY_ADMIN_TOKEN"):
        log.warning("off-site backups are locked with an admin token kept on this disk; set RELAY_ADMIN_TOKEN "
                    "as a variable so a copy can still be opened if the disk is lost")

    def nightly_backups():
        time.sleep(300)                            # let a fresh deploy settle first
        while True:
            try:
                made = backup_mod.nightly(store.dir, room=lambda n: media_mod.room(store.dir, n))
                if made:
                    log.info("backup written: %s", os.path.basename(made))
                sent = offsite_mod.sync(store.dir, offsite, admin_token)
                if sent:
                    log.info("backup copied off-site: %s", sent)
            except Exception as e:                 # a backup that fails must never take the hub down
                log.warning("backup failed: %s", e)
            try:
                app.state.alert_health()           # a failed backup, a key turned away, a disk filling up
            except Exception as e:
                log.warning("health check failed: %s", e)
            try:
                media_svc.tidy()                   # half-finished uploads and leftovers from restarts
            except Exception as e:
                log.warning("tidying up failed: %s", e)
            time.sleep(3600)

    if os.environ.get("RELAY_BACKUPS", "1") != "0":
        threading.Thread(target=nightly_backups, name="relay-backups", daemon=True).start()

    # ---------------------------------------------------------------- delete my account
    # ---------------------------------------------------------------- download my data
    EXPORT_NOTE = ("Your data from Relay.\n\n"
                   "Open index.html to read everything: every conversation, in order, with dates. Each conversation is also "
                   "a plain text file in the conversations folder.\n\n"
                   "The .json files are the same data for other apps: me.json (your account), agents.json (your agents), "
                   "conversations/*.json, shortcuts.json, keys.json (names and websites only: the keys themselves never "
                   "leave the hub), approvals.json (what your agents asked to do with them, and what you said), "
                   "devices.json (passkeys and notification devices, by name).\n\n"
                   "Videos and files you shared are not in this download, only their names. Save the ones you want from "
                   "Relay before you delete your account.\n")

    def when_iso(t) -> Optional[str]:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)) if t else None

    def when_read(t) -> str:
        return time.strftime("%a %d %b %Y, %H:%M UTC", time.gmtime(t)) if t else ""

    def file_safe(text: str, n: int = 48) -> str:
        words = re.sub(r"[^\w\s.,'-]", "", re.sub(r"@\S+", "", text or "")).split()
        return " ".join(words)[:n].strip(" .,-") or "Conversation"

    @app.get("/me/export")
    def export_me(me: dict = Depends(auth)):
        """A zip of everything this person has on the hub, readable as plain JSON."""
        if me["role"] != "human":
            raise HTTPException(403, "Sign in as yourself to download your data.")
        h = me["handle"]
        buf = io.BytesIO()
        with store.ro() as c:
            boss = first_owner(c)
            mine = [r["handle"] for r in c.execute(
                "SELECT handle FROM agents WHERE role='agent' AND (person=? OR (? AND (person IS NULL OR person NOT IN"
                " (SELECT handle FROM agents WHERE role='human'))))", (h, 1 if boss == h else 0))]
            who = [h] + mine
            qs = ",".join("?" * len(who))
            row = c.execute("SELECT handle, created, last_seen, owner FROM agents WHERE handle=?", (h,)).fetchone()
            email = c.execute("SELECT email FROM people_emails WHERE handle=?", (h,)).fetchone()
            agents_out = [{"name": r["handle"], "app": r["provider"], "connected_from": r["client"], "created": r["created"],
                           "last_seen": r["last_seen"]} for r in c.execute(f"SELECT * FROM agents WHERE handle IN ({','.join('?' * len(mine)) or "''"}) AND role='agent'", mine)]
            threads_ = [r["thread"] for r in c.execute(f"SELECT DISTINCT thread FROM members WHERE agent IN ({qs})", who)]
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr("README.txt", EXPORT_NOTE)
                z.writestr("me.json", json.dumps({"name": h, "owner": bool(row["owner"]), "joined": row["created"],
                                                  "last_seen": row["last_seen"], "email": email["email"] if email else None,
                                                  "pinned_conversations": [r["thread"] for r in c.execute(
                                                      "SELECT thread FROM marks WHERE handle=? AND pinned IS NOT NULL ORDER BY pinned", (h,))],
                                                  "muted_conversations": [r["thread"] for r in c.execute(
                                                      "SELECT thread FROM marks WHERE handle=? AND muted=1", (h,))]}, indent=2))
                z.writestr("agents.json", json.dumps(agents_out, indent=2))
                index = []
                for t in threads_:
                    msgs = [msg_out(r) for r in c.execute("SELECT * FROM messages WHERE thread=? ORDER BY id LIMIT 20000", (t,))]
                    for m in msgs:
                        m["when"] = when_iso(m.get("time"))
                    z.writestr(f"conversations/{t}.json", json.dumps(msgs, indent=1))
                    if not msgs:
                        continue
                    who = sorted(({m["from"] for m in msgs} | {x for m in msgs for x in (m.get("mentions") or [])}) - {SYSTEM})
                    start = msgs[0]
                    title = file_safe(start.get("text", ""))
                    name = f"{time.strftime('%Y-%m-%d', time.gmtime(start['time']))} {title} ({', '.join(who)[:60]}) {t}.txt"
                    z.writestr(f"conversations/{name}", "\n\n".join(
                        f"{when_read(m['time'])} · {m['from']}:\n{m.get('text') or ''}"
                        + "".join(f"\n[file: {f.get('name') or f.get('id')}]" for f in (m.get("media") or []) if isinstance(f, dict))
                        for m in msgs) + "\n")
                    index.append((start["time"], title, who, name, msgs))
                index.sort(key=lambda x: -x[0])
                esc = html.escape
                page = ["<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>",
                        f"<title>Your Relay data</title><body style=\"font:16px/1.5 -apple-system,system-ui,sans-serif;max-width:720px;"
                        f"margin:0 auto;padding:24px 16px;color:#18171d;background:#f6f4ef\"><h1>Your Relay data</h1>",
                        f"<p>{esc(h)} · downloaded {esc(when_read(time.time()))} · {len(index)} conversation{'s' if len(index) != 1 else ''}</p>"]
                for t0, title, who, name, msgs in index:
                    page.append(f"<details style=\"background:#fff;border-radius:14px;padding:12px 16px;margin:10px 0\"><summary><b>{esc(title)}</b>"
                                f"<br><small>{esc(when_read(t0))} · {esc(', '.join(who))} · {len(msgs)} message{'s' if len(msgs) != 1 else ''}</small></summary>")
                    for m in msgs:
                        page.append(f"<p><small>{esc(when_read(m['time']))}</small><br><b>{esc(m['from'])}</b>: "
                                    f"{esc(m.get('text') or '').replace(chr(10), '<br>')}</p>")
                    page.append("</details>")
                z.writestr("index.html", "\n".join(page))
                z.writestr("shortcuts.json", json.dumps([{"title": r["title"], "text": r["text"], "to": _loads(r["mentions"], []),
                                                          "uses": r["uses"]} for r in c.execute("SELECT * FROM shortcuts WHERE handle=?", (h,))], indent=2))
                z.writestr("keys.json", json.dumps([{"name": r["name"], "websites": _loads(r["hosts"], []), "ask": r["ask"],
                                                     "created": r["created"], "last_used": r["last_used"]}
                                                    for r in c.execute("SELECT * FROM vault_keys WHERE owner=?", (h,))], indent=2))
                z.writestr("approvals.json", json.dumps([{"agent": r["agent"], "method": r["method"], "url": r["url"], "why": r["why"],
                                                          "status": r["status"], "asked": r["created"], "decided": r["decided"]}
                                                         for r in c.execute("SELECT * FROM vault_calls WHERE owner=? ORDER BY created", (h,))], indent=2))
                z.writestr("devices.json", json.dumps({
                    "passkeys": [{"name": r["name"], "added": r["created"], "last_used": r["last_used"]}
                                 for r in c.execute("SELECT name, created, last_used FROM passkeys WHERE handle=?", (h,))],
                    "notifications": [{"device": r["label"], "kinds": _loads(r["kinds"], []), "added": r["created"]}
                                      for r in c.execute("SELECT label, kinds, created FROM push_subs WHERE handle=?", (h,))]}, indent=2))
        name = time.strftime(f"relay-{h}-%Y-%m-%d.zip", time.gmtime())
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})

    @app.post("/me/delete")
    def delete_me(body: DeleteIn, me: dict = Depends(auth)):
        """A person leaves: they, their agents, sign-ins, connections, saved keys and everything they shared
        are deleted. Their messages stay in other people's conversations as "(deleted)", so replies make
        sense. The hub's owner can't: the hub would be left with nobody to run it."""
        if me["role"] != "human":
            raise HTTPException(403, "Sign in as yourself to delete your account.")
        if body.confirm.strip().lower() != me["handle"]:
            raise HTTPException(400, f"Type {me['handle']} to confirm.")
        if me.get("owner"):
            raise HTTPException(409, "You run this hub, so your account stays with it.")
        return erase_person(me["handle"])

    @app.delete("/people/{handle}")
    def remove_person(handle: str, me: dict = Depends(auth)):
        """The hub's owner removes a person: everything Delete my account removes, for them."""
        if not me.get("owner"):
            raise HTTPException(403, "Only the hub's owner removes people.")
        with store.ro() as c:
            r = c.execute("SELECT role, owner FROM agents WHERE handle=?", (handle,)).fetchone()
        if not r or r["role"] != "human":
            raise HTTPException(404, f"no person @{handle}")
        if r["owner"]:
            raise HTTPException(409, "That's you: the hub keeps its owner.")
        return erase_person(handle)

    def erase_person(p: str) -> dict:
        """A person, their agents, sign-ins, connections, devices, saved keys and files: gone. Their words in
        other people's conversations become "(deleted)"."""
        with store.tx() as c:
            handles = [p] + [r["handle"] for r in c.execute("SELECT handle FROM agents WHERE person=? AND role='agent'", (p,))]
            q, hs = ",".join("?" * len(handles)), handles
            mids = [r["id"] for r in c.execute(f"SELECT id FROM messages WHERE sender IN ({q})", hs)]
            c.execute(f"UPDATE messages SET text='(deleted)', artifacts='[]', origin_session=NULL WHERE sender IN ({q})", hs)
            if mids:
                c.execute(f"DELETE FROM message_media WHERE msg IN ({','.join('?' * len(mids))})", mids)
            for table, col in (("subs", "agent"), ("follows", "agent"), ("cursors", "agent"), ("contacts", "owner"),
                               ("wakes", "handle"), ("people_emails", "handle"), ("sessions", "handle"),
                               ("agent_keys", "handle"), ("routes", "handle"), ("thread_sessions", "handle"),
                               ("session_cursors", "handle"), ("delivered", "handle"), ("passkeys", "handle"),
                               ("oauth_tokens", "handle"), ("oauth_codes", "handle"), ("webauthn_challenges", "handle"),
                               ("push_subs", "handle"), ("reads", "handle"), ("seen", "handle"), ("shortcuts", "handle"),
                               ("prefs", "handle"), ("marks", "handle"),
                               ("agents", "handle")):
                c.execute(f"DELETE FROM {table} WHERE {col} IN ({q})", hs)
            for table in ("oauth_tokens", "oauth_codes"):
                c.execute(f"DELETE FROM {table} WHERE person=?", (p,))
            for table in ("vault_calls", "vault_keys"):
                c.execute(f"DELETE FROM {table} WHERE owner=?", (p,))
            c.execute(f"DELETE FROM contacts WHERE address IN ({q})", hs)
        files = media_svc.erase(handles)
        log.info("account deleted: @%s with %d agent(s), %d file(s)", p, len(handles) - 1, files)
        return {"deleted": p, "agents": handles[1:]}

    @app.get("/backup/status")
    def backup_status(me: dict = Depends(auth)):
        """For the owner: the newest nightly copy here, and whether one is safely off this server."""
        if not (me.get("owner") or me["role"] == "admin"):
            raise HTTPException(403, "Only the hub's owner sees backups.")
        folder = os.path.join(store.dir, "backups")
        local = sorted(f for f in os.listdir(folder) if f.endswith(".zip")) if os.path.isdir(folder) else []
        st = offsite_mod.status(store.dir)
        return {"local": {"name": local[-1], "time": os.path.getmtime(os.path.join(folder, local[-1]))} if local else None,
                "offsite": {"on": bool(offsite), "name": st.get("name"), "ok": st.get("ok"), "error": st.get("error"),
                            "failed": st.get("failed")}}

    # ---------------------------------------------------------------- health: told when something breaks
    HEALTH_GAP = 20 * 3600                     # the same problem taps you at most once a day
    STARTED = time.time()

    def site_of(url: str) -> str:
        host = (urllib.parse.urlsplit(url).hostname or url).lower()
        for name, hosts in (("Instagram", ("instagram.com", "facebook.com")), ("YouTube", ("youtube.com", "googleapis.com")),
                            ("TikTok", ("tiktok.com", "tiktokapis.com")), ("X", ("x.com", "twitter.com"))):
            if any(host == x or host.endswith("." + x) for x in hosts):
                return name
        return host

    def health_for(h: str) -> list:
        """What's fine and what isn't, for this person: their saved keys; for the owner also backups and storage.
        Problems first. Each: id, ok, title, detail and, where there's something to do, where to do it."""
        out = []
        with store.ro() as c:
            owner = first_owner(c) == h
            keys = c.execute("SELECT id, name FROM vault_keys WHERE owner=? ORDER BY name", (h,)).fetchall()
            refused = []
            for k in keys:                     # the newest answer a website gave with this key
                r = c.execute("SELECT result_status, url FROM vault_calls WHERE key_id=? AND status='done'"
                              " ORDER BY finished DESC LIMIT 1", (k["id"],)).fetchone()
                if r and r["result_status"] in (401, 403):
                    refused.append((k, r))
        for k, r in refused:
            out.append({"id": f"key:{k['id']}", "ok": False, "title": f"{site_of(r['url'])} turned away your {k['name']} key",
                        "detail": "It may have expired or been changed there. Paste a new one in Keys.", "url": f"me/keys/{k['id']}"})
        if keys and not refused:
            out.append({"id": "keys", "ok": True, "title": "Your keys work",
                        "detail": f"{len(keys)} saved; no website has turned one away", "url": None})
        if owner and os.environ.get("RELAY_BACKUPS", "1") != "0":
            folder = os.path.join(store.dir, "backups")
            local = sorted(f for f in os.listdir(folder) if f.endswith(".zip")) if os.path.isdir(folder) else []
            age = time.time() - os.path.getmtime(os.path.join(folder, local[-1])) if local else None
            if age is not None and age > 36 * 3600:
                out.append({"id": "backup", "ok": False, "title": f"No backup for {int(age // 86400) or 1} day{'s' if age >= 2 * 86400 else ''}",
                            "detail": "The nightly copy didn't happen. The hub may be short of space; see Storage.", "url": None})
            elif age is None and time.time() - STARTED > 3 * 3600:
                out.append({"id": "backup", "ok": False, "title": "No backup yet",
                            "detail": "The nightly copy hasn't run. The hub may be short of space; see Storage.", "url": None})
            else:
                out.append({"id": "backup", "ok": True, "title": "Backed up every night",
                            "detail": "The first copy is on its way" if age is None else "Last copy today" if age < 86400 else "Last copy yesterday",
                            "url": None})
            if offsite:
                st = offsite_mod.status(store.dir)
                if st.get("error") and not (st.get("ok") and st.get("ok", 0) > st.get("failed", 0)):
                    out.append({"id": "offsite", "ok": False, "title": "The off-site copy failed",
                                "detail": str(st["error"])[:200], "url": None})
                else:
                    out.append({"id": "offsite", "ok": True, "title": "A copy is kept off this server",
                                "detail": "Encrypted, the last 7 nights", "url": None})
        if owner:
            free, total = media_mod.disk(store.dir)
            used = round(100 * (1 - free / total)) if total else 0
            stop = round(100 * (1 - media_mod.KEEP_FREE))
            if total and used >= stop - 5:
                out.append({"id": "storage", "ok": False, "title": f"Storage is {used}% full",
                            "detail": f"New files stop at {stop}%. Remove old videos, or give the volume more room in Railway.", "url": None})
            else:
                out.append({"id": "storage", "ok": True, "title": "Room to spare", "detail": f"Storage {used}% used", "url": None})
        return sorted(out, key=lambda x: x["ok"])

    def alert_health(h: str) -> int:
        """Tap this person on the shoulder about each problem, once a day while it lasts. Returns how many."""
        sent = 0
        now = time.time()
        problems = [x for x in health_for(h) if not x["ok"]]
        with store.tx() as c:
            known = {r["key"][6:]: float(r["value"] or 0) for r in c.execute(
                "SELECT key, value FROM prefs WHERE handle=? AND key LIKE 'alert:%'", (h,))}
            for x in problems:
                if now - known.get(x["id"], 0) >= HEALTH_GAP:
                    pusher.notify(h, "approvals", x["title"], x["detail"], f"/app#{x['url'] or 'me/health'}", tag=x["id"], urgent=True)
                    set_pref(c, h, f"alert:{x['id']}", str(now))
                    sent += 1
            for gone in set(known) - {x["id"] for x in problems}:     # fixed: a new break later taps again
                c.execute("DELETE FROM prefs WHERE handle=? AND key=?", (h, f"alert:{gone}"))
        return sent

    def alert_everyone() -> int:
        with store.ro() as c:
            who = {first_owner(c)} | {r["owner"] for r in c.execute("SELECT DISTINCT owner FROM vault_keys")}
        return sum(alert_health(h) for h in who if h)
    app.state.alert_health = alert_everyone

    @app.get("/me/health")
    def my_health(me: dict = Depends(auth)):
        if me["role"] != "human":
            raise HTTPException(403, "Sign in as yourself.")
        return {"checks": health_for(me["handle"])}

    @app.get("/backup", include_in_schema=False)
    def download_backup(me: dict = Depends(auth)):
        """The owner's copy, to keep off the server: everything except the media files themselves."""
        need_admin(me)
        if not media_mod.room(store.dir, os.path.getsize(store.db_path), keep=0.02):
            raise HTTPException(507, "the hub is nearly full")
        fd, path = tempfile.mkstemp(dir=store.tmp, suffix=".zip")
        os.close(fd)
        try:
            backup_mod.make_zip(store.dir, path)
        except BaseException:
            os.remove(path)
            raise
        from starlette.background import BackgroundTask
        return FileResponse(path, media_type="application/zip",
                            filename=time.strftime("relay-backup-%Y-%m-%d.zip", time.gmtime()),
                            headers={"Cache-Control": "no-store"}, background=BackgroundTask(os.remove, path))
    app.state.sweep = sweep

    @app.post("/subscriptions")
    def subscribe(body: ChannelIn, me: dict = Depends(auth)):
        need_agent(me)
        if body.channel == DIRECT:
            raise HTTPException(400, "conversations aren't a channel: you get the ones you're in")
        with store.tx() as c:
            c.execute("INSERT OR IGNORE INTO subs(agent,channel) VALUES (?,?)",
                      (me["handle"], body.channel))
        return {"ok": True}

    @app.post("/follows")
    def follow(body: FollowIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            if not can_see(c, me["handle"], body.thread):
                raise HTTPException(404, f"no conversation {body.thread}")
            c.execute("INSERT OR IGNORE INTO follows(agent,thread) VALUES (?,?)",
                      (me["handle"], body.thread))
        return {"ok": True}

    # ---------------------------------------------------------------- jobs
    @app.post("/jobs")
    def new_job(body: JobIn, me: dict = Depends(auth), x_relay_session: str = Header(default="")):
        need_agent(me)
        session = check_session(x_relay_session)
        with store.tx() as c:
            return create_job(c, me["handle"], body.kind, body.params,
                              body.requires_approval, body.reply_to, body.note, session=session)

    @app.get("/jobs")
    def list_jobs(status: Optional[str] = None, kind: Optional[str] = None,
                  limit: int = Query(50, ge=1, le=200), me: dict = Depends(auth)):
        q, a = "SELECT * FROM jobs WHERE 1=1", []
        if me["role"] != "admin":          # what my circle asked for, and what I work on as a node
            with store.ro() as c:
                who = circle(c, me["handle"])
            qs = ",".join("?" * len(who))
            q += f" AND (requested_by IN ({qs}) OR claimed_by=?)"; a += who + [me["handle"]]
        if status:
            q += " AND status=?"; a.append(status)
        if kind:
            q += " AND kind=?"; a.append(kind)
        q += " ORDER BY created DESC LIMIT ?"; a.append(limit)
        with store.tx() as c:
            requeue_expired(c)
            return [job_out(r) for r in c.execute(q, a)]

    @app.get("/jobs/{jid}")
    def show_job(jid: str, me: dict = Depends(auth)):
        with store.tx() as c:
            requeue_expired(c)
            return job_out(my_job(c, me, jid))

    @app.post("/jobs/claim")
    def claim(body: ClaimIn, me: dict = Depends(auth)):
        """Hand the oldest queued job this node is able to do to this node.
        Addressed by capability: whoever is awake and able picks up the call."""
        need_agent(me)
        caps = set(me["caps"])
        kinds = (set(body.kinds) & caps) if body.kinds else caps
        if not kinds:
            return {"job": None}
        with store.tx() as c:
            requeue_expired(c)
            marks = ",".join("?" * len(kinds))
            j = c.execute(f"SELECT * FROM jobs WHERE status='queued' AND kind IN ({marks})"
                          " AND (not_before IS NULL OR not_before<=?)"
                          " ORDER BY created LIMIT 1", [*sorted(kinds), _now()]).fetchone()
            if not j:
                return {"job": None}
            c.execute("UPDATE jobs SET status='claimed', claimed_by=?, lease_until=?,"
                      " attempts=attempts+1, error=NULL, updated=? WHERE id=?",
                      (me["handle"], _now() + lease, _now(), j["id"]))
            post(c, SYSTEM, f"@{me['handle']} picked up job {j['id']} [{j['kind']}]",
                 reply_to=j["thread"], job=j["id"])
            return {"job": job_out(get_job(c, j["id"])), "lease_seconds": lease}

    def mine(c, jid, me) -> sqlite3.Row:
        j = get_job(c, jid)
        if j["status"] != "claimed" or j["claimed_by"] != me["handle"]:
            raise HTTPException(409, f"job {jid} is {j['status']} and not held by @{me['handle']}")
        return j

    @app.post("/jobs/{jid}/heartbeat")
    def heartbeat(jid: str, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            mine(c, jid, me)
            c.execute("UPDATE jobs SET lease_until=?, updated=? WHERE id=?",
                      (_now() + lease, _now(), jid))
        return {"ok": True, "lease_seconds": lease}

    @app.post("/jobs/{jid}/complete")
    def complete(jid: str, body: CompleteIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            j = mine(c, jid, me)
            for a in body.artifacts:
                if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (a,)).fetchone():
                    raise HTTPException(400, f"unknown artifact {a}")
            c.execute("UPDATE jobs SET status='done', result=?, artifacts=?, lease_until=NULL,"
                      " updated=? WHERE id=?",
                      (json.dumps(body.result), json.dumps(body.artifacts), _now(), jid))
            j = get_job(c, jid)
            short = body.result.get("summary") or ", ".join(
                f"{k}={v}" for k, v in body.result.items() if not isinstance(v, (dict, list)))
            notify(c, j, f"job {jid} [{j['kind']}] done by @{me['handle']}: {short}"[:1500],
                   artifacts=body.artifacts)
            follow_ups = []
            if j["kind"] == "fetch":
                # every video or audio that lands gets eyes automatically
                for a in body.artifacts:
                    art = c.execute("SELECT * FROM artifacts WHERE id=?", (a,)).fetchone()
                    if art["mime"] and art["mime"].startswith(MEDIA_PREFIXES):
                        follow_ups.append(create_job(
                            c, j["requested_by"], "digest", {"artifact": a},
                            parent_job=jid, thread=j["thread"],
                            note="auto: digest of fetched media"))
            if j["kind"] in ("digest", "clip"):
                src = _loads(j["params"], {}).get("artifact")
                art = c.execute("SELECT meta FROM artifacts WHERE id=?", (src,)).fetchone()
                if art:
                    meta = _loads(art["meta"], {})
                    meta["digest" if j["kind"] == "digest" else "clips"] = {
                        "job": jid, "result": body.result, "artifacts": body.artifacts}
                    c.execute("UPDATE artifacts SET meta=? WHERE id=?", (json.dumps(meta), src))
            return {"job": job_out(j), "follow_ups": follow_ups}

    @app.post("/jobs/{jid}/fail")
    def fail(jid: str, body: FailIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            j = mine(c, jid, me)
            again = body.retry and j["attempts"] < max_att
            # back off before a retry, so one bad moment (a site's bot check, a dropped
            # connection) doesn't burn every attempt in the same second
            wait = retry_base * (2 ** (j["attempts"] - 1)) if again else None
            c.execute("UPDATE jobs SET status=?, claimed_by=NULL, lease_until=NULL, error=?,"
                      " not_before=?, updated=? WHERE id=?",
                      ("queued" if again else "failed", body.error[:4000],
                       (_now() + wait) if again else None, _now(), jid))
            j = get_job(c, jid)
            if not again:
                notify(c, j, f"job {jid} [{j['kind']}] failed: {body.error[:800]}")
            return job_out(j)

    @app.post("/jobs/{jid}/approve")
    def approve(jid: str, me: dict = Depends(auth)):
        if me["role"] not in ("human", "admin"):
            raise HTTPException(403, "only a human can approve")
        with store.tx() as c:
            j = my_job(c, me, jid)
            if j["status"] != "needs_approval":
                raise HTTPException(409, f"job {jid} is {j['status']}")
            c.execute("UPDATE jobs SET status='queued', approved_by=?, updated=? WHERE id=?",
                      (me["handle"], _now(), jid))
            notify(c, get_job(c, jid), f"job {jid} approved by @{me['handle']}")
            return job_out(get_job(c, jid))

    # ---------------------------------------------------------------- artifacts
    @app.post("/artifacts")
    async def upload(request: Request, name: str = Query(...),
                     mime: str = Query("application/octet-stream"),
                     kind: str = Query("file"), parent: Optional[str] = None,
                     me: dict = Depends(auth)):
        """Raw body upload, streamed to disk. Big files never sit in memory and never
        travel inside a message: the message carries the id this returns."""
        need_agent(me)
        try:
            expected = int(request.headers.get("content-length") or 0)
        except ValueError:
            expected = 0
        if expected > max_upload:
            raise HTTPException(413, "upload too large")
        if not media_mod.room(store.dir, expected):
            raise HTTPException(507, "the hub is nearly full")
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=store.tmp)
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_upload:
                        raise HTTPException(413, "upload too large")
                    if size > expected and size % (64 * 2**20) < len(chunk) and not media_mod.room(store.dir, 0):
                        raise HTTPException(507, "the hub is nearly full")
                    h.update(chunk)
                    f.write(chunk)
            aid = h.hexdigest()
            dest = store.blob_path(aid)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                os.remove(tmp)          # same bytes already stored: dedupe
            else:
                os.replace(tmp, dest)
        except BaseException:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise
        with store.tx() as c:
            if parent and not c.execute("SELECT 1 FROM artifacts WHERE id=?", (parent,)).fetchone():
                raise HTTPException(400, f"unknown parent artifact {parent}")
            c.execute("INSERT OR IGNORE INTO artifacts(id,name,mime,size,created_by,created,parent,kind)"
                      " VALUES (?,?,?,?,?,?,?,?)",
                      (aid, name, mime, size, me["handle"], _now(), parent, kind))
            return art_out(c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone())

    def artifact_seen(c, me: dict, aid: str) -> bool:
        """Its maker's circle; anyone in a conversation it was shared in; a node working on a job that
        uses it; and, for a file made from another (a digest, a clip), whoever may see the original."""
        if me["role"] == "admin":
            return True
        h, seen = me["handle"], set()
        while aid and aid not in seen:
            seen.add(aid)
            r = c.execute("SELECT created_by, parent FROM artifacts WHERE id=?", (aid,)).fetchone()
            if not r:
                return False
            if r["created_by"] in circle(c, h):
                return True
            only, more = visible(c, h)
            if c.execute(f"SELECT 1 FROM messages m, json_each(m.artifacts) j WHERE j.value=? AND {only} LIMIT 1",
                         [aid] + more).fetchone():
                return True
            if c.execute("SELECT 1 FROM jobs WHERE claimed_by=? AND (json_extract(params,'$.artifact')=?"
                         " OR EXISTS (SELECT 1 FROM json_each(jobs.artifacts) j WHERE j.value=?)) LIMIT 1",
                         (h, aid, aid)).fetchone():
                return True
            aid = r["parent"]
        return False

    @app.get("/artifacts/{aid}")
    def download(aid: str, me: dict = Depends(auth)):
        with store.ro() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if r and not artifact_seen(c, me, aid):
                r = None
        if not r or not os.path.exists(store.blob_path(aid)):
            raise HTTPException(404, "no such artifact")
        return FileResponse(store.blob_path(aid), media_type=r["mime"], filename=r["name"])

    @app.get("/artifacts/{aid}/meta")
    def meta(aid: str, me: dict = Depends(auth)):
        with store.ro() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if not r or not artifact_seen(c, me, aid):
                raise HTTPException(404, "no such artifact")
            out = art_out(r)
            out["derived"] = [art_out(x) for x in c.execute(
                "SELECT * FROM artifacts WHERE parent=? ORDER BY created", (aid,))]
            return out

    # ---------------------------------------------------------------- the speech model media listens with
    from types import SimpleNamespace
    from . import captions
    transcriber = caption_transcriber or captions.WhisperTranscriber(download_root=os.path.join(store.dir, "models"))

    # ---------------------------------------------------------------- media (videos, audio, images, files)
    media_svc = media_mod.Media(
        store, private_host, os.environ.get("RELAY_SECRET_KEY") or hub_secret, transcriber=transcriber,
        threads=caption_threads if media_threads is None else media_threads, allow_private=allow_private,
        public_url=caption_public_url, max_upload=max_upload)
    app.state.media = media_svc
    media_svc.may_use = lambda c, who, aid: artifact_seen(c, {"handle": who, "role": "agent"}, aid)
    if not os.environ.get("RELAY_SECRET_KEY"):
        media_svc.also_accept(admin_token)          # links made before the hub had its own secret keep working
    media_mod.mount(app, SimpleNamespace(auth=auth), media_svc)

    def delivered(link: dict, mid: str) -> None:
        """A person used an upload link: the file goes to the chat that asked for it, in its conversation."""
        m = media_svc.get(mid)
        text = (f"📎 {m['name'] or 'A file'} arrived through your upload link ({media_mod.size_text(m['size'] or 0)}). "
                f"It's being looked at now; view_media {mid} shows it.")
        with store.tx() as c:
            anchor = link.get("anchor")
            if anchor and c.execute("SELECT 1 FROM messages WHERE id=?", (anchor,)).fetchone():
                post(c, SYSTEM, text, reply_to=anchor, mentions=[link["handle"]], media=[mid])
            else:
                post(c, SYSTEM, text, channel="media", mentions=[link["handle"]], media=[mid])
    media_svc.on_upload = delivered
    media_svc.resume()

    # ---------------------------------------------------------------- saved keys (used, never seen)
    from . import vault as vault_mod
    vault = vault_mod.Vault(store, _now, os.environ.get("RELAY_VAULT_KEY") or hub_secret,
                            send=vault_send, resolve=vault_resolve)
    app.state.vault = vault
    VERBS = {"GET": "read", "POST": "post", "PUT": "change", "PATCH": "change", "DELETE": "delete"}

    def ok_needed(owner, cid, agent, method, url, key_name):
        host = urllib.parse.urlsplit(url).hostname or url
        pusher.notify(owner, "approvals", f"{agent} wants to {VERBS.get(method, method)}",
                      f"With your {key_name} key, on {host}. Tap to review.", f"/app#approve/{cid}", tag=cid, urgent=True)
    vault.on_waiting = ok_needed
    vault.on_refused = alert_health                 # a website turned a key away: say so now, not tomorrow

    def waiting_for(person: str) -> int:
        """What waits for this person: OKs to give, and for the owner, people asking to join."""
        n = len(vault.waiting(person))
        with store.ro() as c:
            if first_owner(c) == person:
                n += c.execute("SELECT COUNT(*) FROM invite_requests WHERE status='waiting'").fetchone()[0]
        return n
    pusher.badge_for = waiting_for
    if not os.environ.get("RELAY_VAULT_KEY"):
        vault.rekey(admin_token)                    # saved keys sealed with the admin token move to the hub's own

    # ---------------------------------------------------------------- contacts + connector
    from . import connector
    connector.mount(app, SimpleNamespace(
        store=store, auth=auth, post=post, inbox_for=inbox_for, ack_to=ack_to,
        take=take, seen_by=seen_by, check_session=check_session, CONVERSATION=CONVERSATION, media=media_svc, origin=origin, here=own_origin, no_session=no_session, start_session=start_session,
        mark_read=mark_read, msg_out=msg_out, with_context=with_context, now=_now, loads=_loads, handle_re=HANDLE_RE,
        session_state=session_state,
        vault=vault))
    from . import people
    sb_url = supabase_url or os.environ.get("SUPABASE_URL")
    sb_key = supabase_key or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    supabase = people.Supabase(sb_url, sb_key, send=supabase_send) if sb_url and sb_key else None
    rs_key = resend_key or os.environ.get("RESEND_API_KEY") or os.environ.get("RESEND_KEY")
    resend = people.Resend(rs_key, email_from or os.environ.get("RELAY_EMAIL_FROM") or "Relay <onboarding@resend.dev>",
                           send=resend_send) if rs_key else None
    people.mount(app, SimpleNamespace(store=store, auth=auth, now=_now, hash=_hash, client_ip=client_ip,
                                      check_new_agent=check_new_agent, public_url=public_url,
                                      new_session=lambda h: new_session(h), tell_owner=tell_owner), supabase, resend)

    # ---------------------------------------------------------------- the daily email: for people without notifications
    DIGEST_GAP = 24 * 3600 - 1800                # once a day at most (the loop runs every half hour)

    def pref(c, h: str, key: str) -> Optional[str]:
        r = c.execute("SELECT value FROM prefs WHERE handle=? AND key=?", (h, key)).fetchone()
        return r["value"] if r else None

    def set_pref(c, h: str, key: str, value: str) -> None:
        c.execute("INSERT INTO prefs(handle,key,value) VALUES (?,?,?) ON CONFLICT(handle,key) DO UPDATE SET value=excluded.value",
                  (h, key, value))

    def unread_for(c, h: str) -> int:
        """Conversations of this person's with something they haven't seen."""
        floor = int((c.execute("SELECT value FROM meta WHERE key='seen_floor'").fetchone() or {"value": 0})["value"])
        return c.execute(
            "SELECT COUNT(*) FROM convs t JOIN messages m ON m.id=t.last JOIN members mb ON mb.thread=t.thread AND mb.agent=?"
            " LEFT JOIN seen s ON s.handle=? AND s.thread=t.thread"
            " WHERE m.sender NOT IN (?, ?) AND m.id > COALESCE(s.last, ?)"
            " AND NOT (t.n = 1 AND m.text LIKE '🔑 Asking %')"      # an agent's "asking you to OK" note: the OK itself is listed
            " AND NOT EXISTS (SELECT 1 FROM marks k WHERE k.handle=? AND k.thread=t.thread AND k.muted=1)",
            (h, h, h, SYSTEM, floor, h)).fetchone()[0]

    def digest_lines(h: str) -> list:
        """What waits for this person, for the daily email: OKs to give first (each with its own link and when it
        expires), then the rest. Each: {text, url, note, ask}."""
        lines = []
        for call in vault.waiting(h)[:5]:
            verb = VERBS.get(call["method"], call["method"])
            said = None
            try:
                body = json.loads(call["body"]) if call["body"] else {}
                said = next((body[k] for k in ("caption", "text", "message", "title") if isinstance(body, dict)
                             and isinstance(body.get(k), str) and body[k].strip()), None)
            except ValueError:
                pass
            left = call["created"] + vault_mod.APPROVAL_SECONDS - _now()
            lines.append({"text": f"{call['agent']} wants to {verb} on {site_of(call['url'])}" + (f": “{said.strip()[:120]}”" if said else ""),
                          "note": f"Expires in {max(1, round(left / 3600))} h" if left >= 3600 else "Expires within the hour",
                          "url": f"/app#approve/{call['id']}", "ask": True,
                          "subject": f"{call['agent']} is waiting for your OK to {verb} on {site_of(call['url'])}"})
        with store.ro() as c:
            if first_owner(c) == h:
                for r in c.execute("SELECT name FROM invite_requests WHERE status='waiting' ORDER BY created DESC LIMIT 5"):
                    lines.append({"text": f"{r['name']} would like an invite", "url": "/app#activity", "ask": False})
            n = unread_for(c, h)
        if n:
            lines.append({"text": f"{n} conversation{'s have' if n != 1 else ' has'} something new", "url": "/app#activity", "ask": False})
        lines += [{"text": x["title"], "url": f"/app#{x['url'] or 'me/health'}", "ask": False} for x in health_for(h) if not x["ok"]][:3]
        return lines

    def stop_token(h: str) -> str:
        """Stops this person's daily email from the email itself, without signing in. Only does that."""
        return h + "." + hmac.new(hub_secret.encode(), b"digest-stop:" + h.encode(), hashlib.sha256).hexdigest()[:32]

    def local_day(c, h: str):
        """(this person's local date, local hour), or None when the app hasn't said which time zone they're in."""
        tz = pref(c, h, "tz")
        if tz is None:
            return None
        t = _now() + int(tz) * 60
        return int(t // 86400), int(t % 86400 // 3600)

    def send_digests() -> int:
        """Everyone with an email on file, no notifications turned on and the email not switched off, who has
        something waiting and hasn't had one in a day, gets one short email. Returns how many went."""
        if not resend:
            return 0
        sent = 0
        with store.ro() as c:
            people_ = [(r["handle"], r["email"]) for r in c.execute(
                "SELECT e.handle, e.email FROM people_emails e JOIN agents a ON a.handle=e.handle AND a.role='human'"
                " WHERE NOT EXISTS (SELECT 1 FROM push_subs p WHERE p.handle=e.handle AND p.kinds LIKE '%\"approvals\"%')")]
        home = public_url or seen_https["origin"] or ""
        for h, email in people_:
            with store.ro() as c:
                if pref(c, h, "digest") == "off":
                    continue
                day = local_day(c, h)
                if day is None:                    # time zone unknown: a day apart
                    if _now() - float(pref(c, h, "digest_sent") or 0) < DIGEST_GAP:
                        continue
                elif pref(c, h, "digest_day") == str(day[0]) or not (8 <= day[1] < 21):
                    continue                       # once a local day, in the morning or daytime, never at 3 am
            lines = digest_lines(h)
            if not lines:
                continue
            try:
                status, _ = resend.send_digest(email, lines, home, f"{home}/email/stop?t={stop_token(h)}" if home else "")
            except Exception as e:
                log.warning("digest to @%s failed: %s", h, e)
                continue
            if 200 <= status < 300:
                with store.tx() as c:
                    set_pref(c, h, "digest_sent", str(_now()))
                    if day is not None:
                        set_pref(c, h, "digest_day", str(day[0]))
                sent += 1
        return sent
    app.state.send_digests = send_digests

    def digest_loop():
        time.sleep(600)
        while True:
            try:
                n = send_digests()
                if n:
                    log.info("daily emails sent: %d", n)
            except Exception as e:
                log.warning("daily emails failed: %s", e)
            time.sleep(1800)
    if os.environ.get("RELAY_DIGEST", "1") != "0" and os.environ.get("RELAY_BACKUPS", "1") != "0":
        threading.Thread(target=digest_loop, name="relay-digest", daemon=True).start()

    def prefs_out(c, h: str) -> dict:
        """The daily email as the person should see it: on or off, where it goes (half hidden), and whether a
        device of theirs gets "needs you" notifications, which pauses it."""
        row = c.execute("SELECT email FROM people_emails WHERE handle=?", (h,)).fetchone()
        addr = row["email"] if row else None
        if addr and "@" in addr:
            name, dom = addr.split("@", 1)
            addr = f"{name[:1]}{'•' * min(max(len(name) - 1, 2), 6)}@{dom}"
        dev = c.execute("SELECT label FROM push_subs WHERE handle=? AND kinds LIKE '%\"approvals\"%' ORDER BY last_ok DESC LIMIT 1",
                        (h,)).fetchone()
        return {"digest": pref(c, h, "digest") != "off", "email": bool(resend), "address": addr,
                "paused_by": (dev["label"] or "a device") if dev else None}

    @app.get("/me/prefs")
    def my_prefs(me: dict = Depends(auth)):
        with store.ro() as c:
            return prefs_out(c, me["handle"])

    @app.put("/me/prefs")
    def set_my_prefs(body: PrefsIn, me: dict = Depends(auth)):
        if me["role"] != "human":
            raise HTTPException(403, "Settings are for people.")
        with store.tx() as c:
            if body.digest is not None:
                set_pref(c, me["handle"], "digest", "on" if body.digest else "off")
            if body.tz is not None:
                set_pref(c, me["handle"], "tz", str(body.tz))
            return prefs_out(c, me["handle"])

    # Stop the daily email from the email itself: the link carries a token that can do only that. GET for a
    # person's click, POST for a mail app's one-click unsubscribe (RFC 8058).
    @app.api_route("/email/stop", methods=["GET", "POST"], include_in_schema=False)
    def stop_email(t: str = Query("", max_length=200)):
        h = t.rsplit(".", 1)[0]
        ok = bool(h) and hmac.compare_digest(t, stop_token(h))
        if ok:
            with store.tx() as c:
                if c.execute("SELECT 1 FROM agents WHERE handle=? AND role='human'", (h,)).fetchone():
                    set_pref(c, h, "digest", "off")
                else:
                    ok = False
        msg = ("You won't get Relay's daily email any more. Turn it back on in Relay: Me, Notifications."
               if ok else "That link didn't work. Turn the daily email off in Relay: Me, Notifications.")
        page = ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>Relay</title><body style=\"font:17px/1.5 -apple-system,system-ui,sans-serif;background:#f6f4ef;color:#18171d;"
                f"display:grid;place-items:center;min-height:90vh;margin:0;padding:24px\"><p style=\"max-width:30em\">{html.escape(msg)}</p>")
        return Response(page, status_code=200 if ok else 400, media_type="text/html; charset=utf-8", headers=page_headers)

    # ---------------------------------------------------------------- connect apps with "Allow" (OAuth)
    def new_session(handle: str) -> dict:
        """A signed-in person (passkey): the same kind of session an email sign-in gives."""
        token = "rly_sess_" + secrets.token_urlsafe(32)
        now = _now()
        with store.tx() as c:
            c.execute("INSERT INTO sessions(token_hash,handle,created,expires,last_used) VALUES (?,?,?,?,?)",
                      (_hash(token), handle, now, now + 30 * 86400, now))
            owner = c.execute("SELECT owner FROM agents WHERE handle=?", (handle,)).fetchone()
        return {"token": token, "handle": handle, "owner": bool(owner and owner["owner"])}

    from . import oauth
    oauth.mount(app, SimpleNamespace(store=store, auth=auth, now=_now, check_new_agent=check_new_agent, client_ip=client_ip,
                                     private_host=(lambda h: False) if allow_private else private_host,
                                     origin=origin, here=own_origin, email_on=bool(supabase or resend), log=log, new_session=new_session))
    vault_mod.mount(app, SimpleNamespace(auth=auth, store=store, post=post, SYSTEM=SYSTEM), vault)
    return app


def __getattr__(name):            # `uvicorn relay.hub:app` builds the app on first use
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
