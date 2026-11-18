#!/usr/bin/env python3
"""Relay hub.

Agents on different providers (Claude, Codex, Muse, anything that can make an HTTP
request) talk through one hub. Three things live here:

  * messages  - channels, flat threads, mentions, a cursor-acknowledged inbox.
                Model borrowed from opencrawl-core's board (Apache-2.0): flat threads
                keyed by root id, idempotent posts, mentions resolved at post time,
                at-least-once delivery, client polling.
  * jobs      - work addressed to a CAPABILITY ("fetch", "digest"), not to a named
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
import os
import re
import secrets
import sqlite3
import tempfile
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
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
"""

HANDLE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,31}$")
RESERVED = {"relay", "admin"}
SYSTEM = "relay"            # sender name for messages the hub writes itself
MEDIA_PREFIXES = ("video/", "audio/")


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


class CapsIn(BaseModel):
    caps: list[str]


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
               allow_private_fetch: Optional[bool] = None) -> FastAPI:
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
    app = FastAPI(title="Relay", version="0.1.0")
    app.state.store = store

    # ---------------------------------------------------------------- auth
    def auth(authorization: str = Header(default="")) -> dict:
        tok = authorization.removeprefix("Bearer ").strip()
        if not tok:
            raise HTTPException(401, "missing bearer token")
        if secrets.compare_digest(tok, admin_token):
            return {"handle": "admin", "role": "admin", "caps": []}
        with store.tx() as c:
            r = c.execute("SELECT * FROM agents WHERE token_hash=?", (_hash(tok),)).fetchone()
            if not r:
                raise HTTPException(401, "unknown token")
            c.execute("UPDATE agents SET last_seen=? WHERE handle=?", (_now(), r["handle"]))
        return {"handle": r["handle"], "role": r["role"], "caps": _loads(r["caps"], [])}

    def need_agent(me: dict) -> None:
        if me["role"] == "admin":
            raise HTTPException(403, "admin token is for registration only; use an agent token")

    # ---------------------------------------------------------------- internals
    def post(c, sender, text, channel=None, reply_to=None, mentions=(), artifacts=(),
             idem=None, job=None) -> dict:
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
            "INSERT INTO messages(sender,idem,channel,thread,reply_to,time,text,mentions,artifacts,job)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (sender, idem, channel, thread, reply_to, _now(), text,
             json.dumps(mentions), json.dumps(list(artifacts)), job))
        mid = cur.lastrowid
        if thread is None:
            thread = mid
            c.execute("UPDATE messages SET thread=? WHERE id=?", (mid, mid))
        if sender != SYSTEM:
            c.execute("INSERT OR IGNORE INTO follows(agent,thread) VALUES (?,?)", (sender, thread))
        return msg_out(c.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone())

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
                   note=None, parent_job=None, thread=None) -> dict:
        if kind == "fetch":
            url = str(params.get("url", ""))
            if not url.startswith(("http://", "https://")):
                raise HTTPException(400, "fetch needs params.url (http or https)")
            if not allow_private and private_host(urllib.parse.urlparse(url).hostname or ""):
                raise HTTPException(400, "fetch may not target local or private network addresses")
        if kind == "digest":
            if not c.execute("SELECT 1 FROM artifacts WHERE id=?",
                             (params.get("artifact", ""),)).fetchone():
                raise HTTPException(400, "digest needs params.artifact (an existing artifact id)")
        jid = "j_" + uuid.uuid4().hex[:12]
        summary = ", ".join(f"{k}={v}" for k, v in params.items())[:300]
        text = f"job {jid} [{kind}] {summary}" + (f" — {note}" if note else "")
        sender = me_handle if is_agent(c, me_handle) else SYSTEM
        if thread is not None:
            m = post(c, sender, text, reply_to=thread, job=jid)
        elif reply_to is not None:
            m = post(c, sender, text, reply_to=reply_to, job=jid)
        else:
            m = post(c, sender, text, channel="jobs", job=jid)
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
    @app.get("/health")
    def health():
        return {"ok": True, "version": "0.1.0"}

    # ---------------------------------------------------------------- agents
    @app.post("/agents")
    def register(body: AgentIn, me: dict = Depends(auth)):
        if me["role"] != "admin":
            raise HTTPException(403, "only the admin token can register agents")
        if not HANDLE_RE.match(body.handle) or body.handle in RESERVED:
            raise HTTPException(400, "handle must be lowercase, 1-32 chars, not reserved")
        if body.role not in ("agent", "human"):
            raise HTTPException(400, "role must be agent or human")
        token = "rly_" + secrets.token_urlsafe(32)
        with store.tx() as c:
            if c.execute("SELECT 1 FROM agents WHERE handle=?", (body.handle,)).fetchone():
                raise HTTPException(409, f"@{body.handle} already registered")
            c.execute("INSERT INTO agents(handle,token_hash,caps,role,created) VALUES (?,?,?,?,?)",
                      (body.handle, _hash(token), json.dumps(sorted(set(body.caps))),
                       body.role, _now()))
            c.execute("INSERT OR IGNORE INTO cursors(agent,last) VALUES (?,0)", (body.handle,))
        # the only time this token is ever returned
        return {"handle": body.handle, "token": token, "caps": sorted(set(body.caps)),
                "role": body.role}

    @app.get("/agents")
    def agents(me: dict = Depends(auth)):
        with store.ro() as c:
            return [{"handle": r["handle"], "caps": _loads(r["caps"], []), "role": r["role"],
                     "last_seen": r["last_seen"]}
                    for r in c.execute("SELECT * FROM agents ORDER BY handle")]

    @app.get("/agents/me")
    def whoami(me: dict = Depends(auth)):
        return me

    @app.put("/agents/me/caps")
    def set_caps(body: CapsIn, me: dict = Depends(auth)):
        need_agent(me)
        caps = sorted(set(body.caps))
        with store.tx() as c:
            c.execute("UPDATE agents SET caps=? WHERE handle=?", (json.dumps(caps), me["handle"]))
        return {"handle": me["handle"], "caps": caps}

    # ---------------------------------------------------------------- messages
    @app.post("/messages")
    def send(body: MessageIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            return post(c, me["handle"], body.text, channel=body.channel,
                        reply_to=body.reply_to, mentions=body.mentions,
                        artifacts=body.artifacts, idem=body.idem)

    @app.get("/messages")
    def read(channel: Optional[str] = None, thread: Optional[int] = None,
             after: int = 0, limit: int = Query(50, ge=1, le=200),
             me: dict = Depends(auth)):
        q, a = "SELECT * FROM messages WHERE id>?", [after]
        if channel:
            q += " AND channel=?"; a.append(channel)
        if thread:
            q += " AND thread=?"; a.append(thread)
        q += " ORDER BY id LIMIT ?"; a.append(limit)
        with store.ro() as c:
            return [msg_out(r) for r in c.execute(q, a)]

    @app.get("/inbox")
    def inbox(limit: int = Query(20, ge=1, le=200), me: dict = Depends(auth)):
        """What is waiting for me: mentions, my channels, threads I follow.
        At-least-once: nothing is consumed until I ack the cursor."""
        need_agent(me)
        h = me["handle"]
        with store.ro() as c:
            cur = c.execute("SELECT last FROM cursors WHERE agent=?", (h,)).fetchone()
            last = cur["last"] if cur else 0
            rows = c.execute(
                "SELECT m.* FROM messages m WHERE m.id>? AND m.sender!=? AND ("
                " EXISTS (SELECT 1 FROM json_each(m.mentions) WHERE value=?)"
                " OR m.channel IN (SELECT channel FROM subs WHERE agent=?)"
                " OR m.thread IN (SELECT thread FROM follows WHERE agent=?))"
                " ORDER BY m.id LIMIT ?", (last, h, h, h, h, limit)).fetchall()
        msgs = [msg_out(r) for r in rows]
        return {"messages": msgs, "cursor": msgs[-1]["id"] if msgs else last}

    @app.post("/inbox/ack")
    def ack(body: AckIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            c.execute("INSERT INTO cursors(agent,last) VALUES (?,?) ON CONFLICT(agent)"
                      " DO UPDATE SET last=MAX(last, excluded.last)", (me["handle"], body.cursor))
        return {"ok": True, "cursor": body.cursor}

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
    def new_job(body: JobIn, me: dict = Depends(auth)):
        need_agent(me)
        with store.tx() as c:
            return create_job(c, me["handle"], body.kind, body.params,
                              body.requires_approval, body.reply_to, body.note)

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
            if j["kind"] == "digest":
                src = _loads(j["params"], {}).get("artifact")
                art = c.execute("SELECT meta FROM artifacts WHERE id=?", (src,)).fetchone()
                if art:
                    meta = _loads(art["meta"], {})
                    meta["digest"] = {"job": jid, "result": body.result,
                                      "artifacts": body.artifacts}
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

    return app


def __getattr__(name):            # `uvicorn relay.hub:app` builds the app on first use
    if name == "app":
        global app
        app = create_app()
        return app
    raise AttributeError(name)
