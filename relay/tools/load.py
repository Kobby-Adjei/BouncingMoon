"""Hammer a copy of the hub the way a busy launch day would, and say where it slows down.

A throwaway hub with 20 people, 50 agents and 100,000 messages in 2,000 conversations, then, all at
once for a minute: every person's app polling like the real one (the conversation list, the open
conversation, agents, approvals), 25 agents long-polling their inbox like the helper, 25 agents reading
and answering through /mcp like Claude and ChatGPT do, and people searching. It reports how long each
kind of request took (median, 95th and 99th percentile, slowest) and anything that failed.

    python relay/tools/load.py [--messages 100000] [--seconds 60]
"""
import argparse
import json
import os
import random
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

PACE = 3.0
WORDS = ("launch video caption deck invoice draft cut post schedule review clip thumbnail budget client "
         "logo music edit render upload export proposal contract meeting notes summary release fix bug").split()


def call(base, method, path, tok=None, body=None, headers=None, timeout=40):
    h = {"Content-Type": "application/json", **(headers or {})}
    if tok:
        h["Authorization"] = "Bearer " + tok
    req = urllib.request.Request(base + path, method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def start(data, port):
    env = {**os.environ, "RELAY_DATA": data, "RELAY_BACKUPS": "0", "RELAY_ADMIN_TOKEN": "load-admin"}
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "relay.hub:app", "--port", str(port), "--log-level", "warning"],
                         cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    t0 = time.monotonic()
    while time.monotonic() - t0 < 300:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health/live", timeout=2)
            return p, time.monotonic() - t0
        except Exception:
            time.sleep(0.2)
    p.kill()
    raise SystemExit("the hub didn't start")


def seed(base, data, people_n, agents_n):
    owner = call(base, "POST", "/owner", "load-admin", {"handle": "kobby"})["token"]
    people = {"kobby": owner}
    for i in range(people_n - 1):
        inv = call(base, "POST", "/invites", owner, {"handle": f"person{i}", "role": "human"})
        people[f"person{i}"] = call(base, "POST", "/invites/claim", None, {"code": inv["code"]})["token"]
    agents = {}
    names = list(people)
    for i in range(agents_n):
        h = f"agent{i}"
        agents[h] = call(base, "POST", "/agents", owner, {"handle": h, "provider": random.choice(["claude", "openai", "codex", "grok"])})["token"]
    db = sqlite3.connect(os.path.join(data, "relay.db"))
    for i in range(agents_n):                       # every person gets two or three agents of their own
        db.execute("UPDATE agents SET person=? WHERE handle=?", (names[i % len(names)], f"agent{i}"))
    db.commit(); db.close()
    return people, agents


def bulk_messages(data, people, agents, total, threads_n):
    """100k messages straight into the database (posting them one by one would take an hour), then the
    hub's own indexes are dropped so it rebuilds them on start, the way an existing hub upgrades."""
    db = sqlite3.connect(os.path.join(data, "relay.db"))
    names, ags = list(people), list(agents)
    now, rows = time.time() - 30 * 86400, []
    mid = db.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
    per = total // threads_n
    for t in range(threads_n):
        person = names[t % len(names)]
        mine = [a for i, a in enumerate(ags) if names[i % len(names)] == person] or ags[:2]
        root = mid + 1
        for j in range(per):
            mid += 1
            sender = person if j % 5 == 0 else random.choice(mine)
            text = " ".join(random.choice(WORDS) for _ in range(random.randint(6, 40)))
            to = [random.choice(mine)] if sender == person else [person]
            rows.append((mid, sender, "direct", root, None if j == 0 else mid - 1, now + mid * 25, text, json.dumps(to)))
    db.executemany("INSERT INTO messages(id,sender,channel,thread,reply_to,time,text,mentions) VALUES (?,?,?,?,?,?,?,?)", rows)
    db.execute("INSERT OR IGNORE INTO follows(agent,thread) SELECT sender, thread FROM messages")
    db.execute("DELETE FROM meta WHERE key IN ('convs_built','fts_built','seen_floor')")
    for t in ("convs", "members"):
        db.execute(f"DELETE FROM {t}")
    db.execute("DROP TABLE IF EXISTS msg_fts")
    db.commit(); db.close()
    return threads_n


class Clock:
    def __init__(self):
        self.lock, self.times, self.errors = threading.Lock(), {}, {}

    def run(self, label, fn):
        t0 = time.monotonic()
        try:
            out = fn()
            with self.lock:
                self.times.setdefault(label, []).append(time.monotonic() - t0)
            return out
        except Exception as e:
            with self.lock:
                key = f"{label}: {getattr(e, 'code', '')} {type(e).__name__}"
                self.errors[key] = self.errors.get(key, 0) + 1
            return None


def load(base, people, agents, threads_n, seconds):
    clock, stop = Clock(), time.monotonic() + seconds
    sample = list(range(1, threads_n + 1))

    def person(tok):
        open_thread = None
        n = 0
        while time.monotonic() < stop:
            if n % 4 == 0:
                got = clock.run("GET /threads", lambda: call(base, "GET", "/threads?limit=40", tok))
                if got:
                    open_thread = random.choice(got)["thread"]
            if open_thread:
                clock.run("GET /messages (open conversation)", lambda: call(base, "GET", f"/messages?thread={open_thread}&latest=true&limit=30", tok))
            if n % 5 == 0:
                clock.run("GET /agents", lambda: call(base, "GET", "/agents", tok))
                clock.run("GET /approvals", lambda: call(base, "GET", "/approvals", tok))
            n += 1
            time.sleep(PACE)

    def listener(tok):            # like the helper: hold a long-poll; when something comes, wake the app (a few seconds)
        while time.monotonic() < stop:
            got = clock.run("GET /inbox?wait (helper)", lambda: call(base, "GET", "/inbox?wait=10&unclaimed=true", tok, timeout=30))
            if got and got.get("messages"):
                time.sleep(5)

    def chat_app(handle, tok):
        sid = f"chat-{handle}"
        while time.monotonic() < stop:
            got = clock.run("POST /mcp check_replies", lambda: call(base, "POST", "/mcp", tok, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                            "params": {"name": "check_replies", "arguments": {}}}, {"Accept": "application/json, text/event-stream", "X-Relay-Session": sid}))
            msgs = (((got or {}).get("result") or {}).get("structuredContent") or {}).get("messages") or []
            for m in msgs[:1]:
                clock.run("POST /mcp reply", lambda: call(base, "POST", "/mcp", tok, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                          "params": {"name": "reply", "arguments": {"message_id": m["id"], "text": "On it: " + " ".join(random.choice(WORDS) for _ in range(8))}}},
                          {"Accept": "application/json, text/event-stream", "X-Relay-Session": sid}))
            time.sleep(3)

    def writer(handle, tok):          # people starting new conversations
        while time.monotonic() < stop:
            to = random.choice(list(agents))
            clock.run("POST /mcp ask", lambda: call(base, "POST", "/mcp", tok, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                      "params": {"name": "ask", "arguments": {"to": to, "message": " ".join(random.choice(WORDS) for _ in range(10)),
                                                      "wait_seconds": 0, "new_topic": True}}},
                      {"Accept": "application/json, text/event-stream", "X-Relay-Session": f"w-{handle}"}))
            time.sleep(4)

    def searcher(tok):
        while time.monotonic() < stop:
            q = " ".join(random.sample(WORDS, random.choice([1, 2])))
            clock.run("GET /search", lambda: call(base, "GET", "/search?q=" + urllib.request.quote(q), tok))
            time.sleep(2)

    ags = list(agents.items())
    workers = [threading.Thread(target=person, args=(t,)) for t in people.values()]
    workers += [threading.Thread(target=listener, args=(t,)) for _, t in ags[:25]]
    workers += [threading.Thread(target=chat_app, args=(h, t)) for h, t in ags[25:]]
    workers += [threading.Thread(target=writer, args=(h, t)) for h, t in ags[25:35]]
    workers += [threading.Thread(target=searcher, args=(t,)) for t in list(people.values())[:3]]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    return clock


def report(clock, startup, n_msgs):
    lines = [f"Startup with {n_msgs} messages (building search and conversation indexes): {startup:.1f} s", ""]
    lines.append(f"{'request':38} {'count':>6} {'median':>8} {'p95':>8} {'p99':>8} {'slowest':>8}")
    for label, ts in sorted(clock.times.items()):
        ts = sorted(ts)
        q = lambda p: ts[min(len(ts) - 1, int(p * len(ts)))]
        lines.append(f"{label:38} {len(ts):6d} {statistics.median(ts)*1000:7.0f}ms {q(.95)*1000:7.0f}ms {q(.99)*1000:7.0f}ms {ts[-1]*1000:7.0f}ms")
    lines.append("")
    lines.append("Failures: " + (", ".join(f"{k} ×{v}" for k, v in clock.errors.items()) or "none"))
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=100_000)
    ap.add_argument("--threads", type=int, default=2000)
    ap.add_argument("--people", type=int, default=20)
    ap.add_argument("--agents", type=int, default=50)
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--out", default=None)
    ap.add_argument("--pace", type=float, default=3.0, help="seconds between a person's polls (the app: 3)")
    a = ap.parse_args()
    global PACE
    PACE = a.pace
    data = tempfile.mkdtemp(prefix="relay-load-")
    port = 9500 + random.randint(0, 400)
    base = f"http://127.0.0.1:{port}"
    p, _ = start(data, port)
    people, agents = seed(base, data, a.people, a.agents)
    p.terminate(); p.wait()
    bulk_messages(data, people, agents, a.messages, a.threads)
    p, startup = start(data, port)
    try:
        clock = load(base, people, agents, a.threads, a.seconds)
    finally:
        p.terminate(); p.wait()
    text = report(clock, startup, a.messages)
    print(text)
    if a.out:
        with open(a.out, "w") as f:
            f.write(text + "\n")


if __name__ == "__main__":
    main()
