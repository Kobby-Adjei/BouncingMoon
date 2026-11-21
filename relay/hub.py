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

import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

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
CONTEXT_MESSAGES, CONTEXT_CHARS = 6, 2000


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
            texts = _fit([r["text"] or "" for r in rows], CONTEXT_CHARS)
            m["context"] = [{"id": r["id"], "from": r["sender"], "time": r["time"], "text": tx}
                            for r, tx in zip(rows, texts)]
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


class MessageIn(BaseModel):
    text: str
    channel: Optional[str] = None
    reply_to: Optional[int] = None
    mentions: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
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
               youtube_key: Optional[str] = None, youtube_opener=None, youtube_threads: bool = True,
               supabase_url: Optional[str] = None, supabase_key: Optional[str] = None,
               supabase_send=None, caption_transcriber=None, caption_threads: bool = True,
               caption_public_url: Optional[str] = None, caption_tool_wait: Optional[float] = None,
               caption_idle_seconds: Optional[float] = None,
               session_idle_seconds: Optional[float] = None,
               session_sweep_seconds: Optional[float] = None) -> FastAPI:
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
    lease = float(lease_seconds or os.environ.get("RELAY_LEASE_SECONDS", 300))
    max_att = int(max_attempts or os.environ.get("RELAY_MAX_ATTEMPTS", 3))
    max_upload = int(max_upload_mb or os.environ.get("RELAY_MAX_UPLOAD_MB", 4096)) * 2**20
    retry_base = float(retry_base_seconds if retry_base_seconds is not None
                       else os.environ.get("RELAY_RETRY_BASE_SECONDS", 30))
    allow_private = (allow_private_fetch if allow_private_fetch is not None
                     else os.environ.get("RELAY_ALLOW_PRIVATE_FETCH") == "1")

    store = Store(data_dir)
    # a chat that answered and then went quiet this long is treated as gone: new messages for it in
    # a conversation someone else started go back to the identity (see release())
    session_idle = float(session_idle_seconds if session_idle_seconds is not None
                         else os.environ.get("RELAY_SESSION_IDLE_SECONDS", 600))
    session_sweep = float(session_sweep_seconds if session_sweep_seconds is not None
                          else os.environ.get("RELAY_SESSION_SWEEP_SECONDS", 30))
    with store.ro() as c:
        session_floor = int(c.execute("SELECT value FROM meta WHERE key='session_floor'").fetchone()["value"])

    @asynccontextmanager
    async def lifespan(_app):
        # `ask` and `/inbox?wait` hold a worker thread while they wait; give them room
        import anyio.to_thread
        anyio.to_thread.current_default_thread_limiter().total_tokens = int(
            os.environ.get("RELAY_THREADS", 200))
        yield

    app = FastAPI(title="Relay", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    from .wake import Waker, WakeError
    waker = Waker(store, admin_token, os.environ.get("RELAY_SECRET_KEY"), opener=wake_opener,
                  check_url=wake_check_url,
                  cooldown=wake_cooldown if wake_cooldown is not None else float(
                      os.environ.get("RELAY_WAKE_COOLDOWN", 20)))
    app.state.waker = waker

    # ---------------------------------------------------------------- auth
    def auth(authorization: str = Header(default="")) -> dict:
        tok = authorization.removeprefix("Bearer ").strip()
        if not tok:
            raise HTTPException(401, "missing bearer token")
        if secrets.compare_digest(tok, admin_token):
            return {"handle": "admin", "role": "admin", "caps": []}
        with store.tx() as c:
            if tok.startswith("rly_sess_"):          # a person signed in by email
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
        return "idle" if (r["seen"] or 0) < _now() - session_idle else "alive"

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

    def take(c, h: str, session: Optional[str], msgs: list) -> None:
        """A chat read these: the ones no chat of `h` owned yet are now its own, and so is their thread.
        An old client (no session) claims them as '' so that no chat handles them a second time."""
        for m in msgs:
            c.execute("INSERT OR IGNORE INTO routes(msg,handle,session) VALUES (?,?,?)", (m["id"], h, session or ""))
            if session and m.get("thread") is not None:     # a released thread moves to the chat taking it
                c.execute("INSERT INTO thread_sessions(thread,handle,session) VALUES (?,?,?)"
                          " ON CONFLICT(thread,handle) DO UPDATE SET session=excluded.session", (m["thread"], h, session))

    def post(c, sender, text, channel=None, reply_to=None, mentions=(), artifacts=(),
             idem=None, job=None, session=None) -> dict:
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
        elif not channel:
            raise HTTPException(400, "channel is required to start a thread")
        for m in mentions:     # resolved at post time; unknown handles fail before commit
            if not c.execute("SELECT 1 FROM agents WHERE handle=?", (m,)).fetchone():
                raise HTTPException(400, f"unknown mention @{m}")
        for a in artifacts:
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?", (a,)).fetchone():
                raise HTTPException(400, f"unknown artifact {a}")
        cur = c.execute(
            "INSERT INTO messages(sender,idem,channel,thread,reply_to,time,text,mentions,artifacts,job,origin_session)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (sender, idem, channel, thread, reply_to, _now(), text,
             json.dumps(mentions), json.dumps(list(artifacts)), job, session))
        mid = cur.lastrowid
        if thread is None:
            thread = mid
            c.execute("UPDATE messages SET thread=? WHERE id=?", (mid, mid))
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
             " OR m.channel IN (SELECT channel FROM subs WHERE agent=?)"
             " OR m.thread IN (SELECT thread FROM follows WHERE agent=?))")
        a: list = [last, h, h, h, h]
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
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?",
                             (str(params.get("artifact", "")),)).fetchone():
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
        "Content-Security-Policy": ("default-src 'none'; script-src 'self'; connect-src 'self'; "
                                    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self'; "
                                    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
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

    @app.get("/classic", include_in_schema=False)
    def classic():             # the first dashboard, kept while the new app settles in
        return static("ui.html", "text/html; charset=utf-8")

    @app.get("/join", include_in_schema=False)
    def join_page():           # the invite code travels in the #fragment, never the path
        return static("ui.html", "text/html; charset=utf-8")

    @app.get("/ui.js", include_in_schema=False)
    def ui_js():
        return static("ui.js", "text/javascript; charset=utf-8")

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
                 ".sh": "text/plain; charset=utf-8", ".ps1": "text/plain; charset=utf-8"}
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

    @app.get("/app", include_in_schema=False)
    def app_page():
        return web("app.html")

    @app.get("/", include_in_schema=False)
    def landing_page():
        return web("landing.html")

    @app.get("/welcome", include_in_schema=False)
    def welcome_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/", status_code=308)

    # The one-line installers carry this hub's own address, so the line people paste needs
    # only the invite code. The address comes from the request (the deploy runs uvicorn with
    # --proxy-headers, so it is https behind Railway) and must look like a plain origin before
    # it goes anywhere near a shell.
    origin_re = re.compile(r"^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$")
    installers = ("install.sh", "install.ps1")

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
        return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "url": "/cli.py"}

    @app.get("/health")
    def health():
        return {"ok": True, "version": "0.1.0"}

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
        return {"handle": out["handle"], "role": out["role"], "caps": out["caps"],
                "provider": out["provider"], "expires": out["expires"], "state": out["state"]}

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
            emails = {r["handle"]: r["email"] for r in c.execute("SELECT handle, email FROM people_emails")} \
                if sees_emails else {}
            return [{"handle": r["handle"], "caps": _loads(r["caps"], []), "role": r["role"],
                     "provider": r["provider"], "client": r["client"], "last_seen": r["last_seen"],
                     "owner": bool(r["owner"]), "listener": listener_out(r),
                     "email": emails.get(r["handle"])}
                    for r in c.execute("SELECT * FROM agents ORDER BY handle")]

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
        with store.tx() as c:
            return post(c, me["handle"], body.text, channel=body.channel,
                        reply_to=body.reply_to, mentions=body.mentions,
                        artifacts=body.artifacts, idem=body.idem, session=session)

    @app.get("/messages")
    def read(channel: Optional[str] = None, thread: Optional[int] = None,
             after: int = 0, limit: int = Query(50, ge=1, le=200),
             latest: bool = False, me: dict = Depends(auth)):
        """Oldest first from `after`; with latest=true, the newest `limit` (still oldest first)."""
        q, a = "SELECT * FROM messages WHERE id>?", [after]
        if channel:
            q += " AND channel=?"; a.append(channel)
        if thread:
            q += " AND thread=?"; a.append(thread)
        q += " ORDER BY id DESC LIMIT ?" if latest else " ORDER BY id LIMIT ?"; a.append(limit)
        with store.ro() as c:
            rows = [msg_out(r) for r in c.execute(q, a)]
        return rows[::-1] if latest else rows

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
            if box["messages"] or time.monotonic() >= deadline:
                return box
            time.sleep(0.5)

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
                " AND (s.ended IS NOT NULL OR COALESCE(s.seen, 0)<?)", (_now() - session_idle,)).fetchall()
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
    app.state.sweep = sweep

    @app.post("/subscriptions")
    def subscribe(body: ChannelIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            c.execute("INSERT OR IGNORE INTO subs(agent,channel) VALUES (?,?)",
                      (me["handle"], body.channel))
        return {"ok": True}

    @app.post("/follows")
    def follow(body: FollowIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
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
            return job_out(get_job(c, jid))

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
            j = get_job(c, jid)
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
        h = hashlib.sha256()
        size = 0
        fd, tmp = tempfile.mkstemp(dir=store.tmp)
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > max_upload:
                        raise HTTPException(413, "upload too large")
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

    @app.get("/artifacts/{aid}")
    def download(aid: str, me: dict = Depends(auth)):
        with store.ro() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
        if not r or not os.path.exists(store.blob_path(aid)):
            raise HTTPException(404, "no such artifact")
        return FileResponse(store.blob_path(aid), media_type=r["mime"], filename=r["name"])

    @app.get("/artifacts/{aid}/meta")
    def meta(aid: str, me: dict = Depends(auth)):
        with store.ro() as c:
            r = c.execute("SELECT * FROM artifacts WHERE id=?", (aid,)).fetchone()
            if not r:
                raise HTTPException(404, "no such artifact")
            out = art_out(r)
            out["derived"] = [art_out(x) for x in c.execute(
                "SELECT * FROM artifacts WHERE parent=? ORDER BY created", (aid,))]
            return out

    # ---------------------------------------------------------------- live stream chat (YouTube)
    from types import SimpleNamespace
    from . import streams
    watcher = streams.Watcher(store, youtube_key if youtube_key is not None else os.environ.get("YOUTUBE_API_KEY"),
                              opener=youtube_opener or urllib.request.urlopen, threads=youtube_threads)
    app.state.watcher = watcher
    stream_tools = streams.mount(app, SimpleNamespace(auth=auth), watcher)
    watcher.resume()

    # ---------------------------------------------------------------- captioned shorts (made here, in the cloud)
    from . import captions
    captioner = captions.Captioner(store, private_host, transcriber=caption_transcriber, threads=caption_threads,
                                   allow_private=allow_private, public_url=caption_public_url,
                                   idle_seconds=600 if caption_idle_seconds is None else caption_idle_seconds)
    app.state.captioner = captioner
    caption_tools = captions.mount(app, SimpleNamespace(auth=auth), captioner,
                                   tool_wait=captions.TOOL_WAIT if caption_tool_wait is None else caption_tool_wait)
    captioner.resume()
    extra_tools = {"tools": stream_tools["tools"] + caption_tools["tools"],
                   "handlers": {**stream_tools["handlers"], **caption_tools["handlers"]}}

    # ---------------------------------------------------------------- contacts + connector
    from . import connector
    connector.mount(app, SimpleNamespace(
        extra_tools=extra_tools, store=store, auth=auth, post=post, inbox_for=inbox_for, ack_to=ack_to,
        take=take, check_session=check_session, CONVERSATION=CONVERSATION, no_session=no_session, start_session=start_session,
        mark_read=mark_read, msg_out=msg_out, with_context=with_context, now=_now, loads=_loads, handle_re=HANDLE_RE))
    from . import people
    sb_url = supabase_url or os.environ.get("SUPABASE_URL")
    sb_key = supabase_key or os.environ.get("SUPABASE_PUBLISHABLE_KEY")
    people.mount(app, SimpleNamespace(store=store, auth=auth, now=_now, hash=_hash,
                                      check_new_agent=check_new_agent),
                 people.Supabase(sb_url, sb_key, send=supabase_send) if sb_url and sb_key else None)
    return app


def __getattr__(name):            # `uvicorn relay.hub:app` builds the app on first use
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
