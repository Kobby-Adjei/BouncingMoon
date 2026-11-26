"""Live updates under load: many open apps waiting on /changes while agents write fast.

A throwaway hub (20 people, 50 agents, 20,000 messages), then for each run: agents post about ten
messages a second into people's conversations while a person's app loads the conversation list every
half second (what a person feels). Run twice: with no apps waiting, and with N apps holding /changes.
Reports the conversation list's speed, the hub's CPU, and how soon an app hears about a message in
one of its own conversations.

    python relay/tools/live_load.py [--apps 200] [--seconds 40]
"""
import argparse
import asyncio
import os
import random
import statistics
import sys
import tempfile
import threading
import time

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from load import bulk_messages, call, seed, start  # noqa: E402


def cpu_seconds(pid: int) -> float:
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return (int(parts[11]) + int(parts[12])) / os.sysconf("SC_CLK_TCK")


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p))] if xs else float("nan")


def run(base, pid, people, agents, threads_by_person, apps, seconds):
    stop = time.monotonic() + seconds
    probe, heard, posted = [], [], {}            # posted: person -> times their conversations got a message
    lock = threading.Lock()
    names = list(people)

    def writer(handle, tok):
        while time.monotonic() < stop:
            person = random.choice(names)           # someone answering in one of their own conversations
            t = random.choice(threads_by_person[person])
            try:
                call(base, "POST", "/messages", people[person], {"reply_to": t, "text": "working on it " + str(random.random())})
                with lock:
                    posted.setdefault(person, []).append(time.monotonic())
            except Exception:
                pass
            time.sleep(len(agents_w) / 10)       # ten messages a second, all writers together

    def prober():
        tok = people["person3"]
        while time.monotonic() < stop:
            t0 = time.monotonic()
            try:
                call(base, "GET", "/threads?limit=40", tok)
                probe.append(time.monotonic() - t0)
            except Exception:
                probe.append(float("inf"))
            time.sleep(0.5)

    async def app(person, tok, client):
        state = ""
        while time.monotonic() < stop:
            try:
                r = await client.get(f"{base}/changes", params={"since": state, "wait": 25},
                                     headers={"Authorization": "Bearer " + tok}, timeout=40)
                new = r.json()["state"]
            except Exception:
                await asyncio.sleep(1)
                continue
            if state and new != state:
                now = time.monotonic()
                with lock:
                    mine = [x for x in posted.get(person, []) if x <= now]
                if mine:
                    heard.append(now - mine[-1])
            state = new

    async def apps_main():
        limits = httpx.Limits(max_connections=apps + 10, max_keepalive_connections=apps + 10)
        async with httpx.AsyncClient(limits=limits) as client:
            await asyncio.gather(*(app(names[i % len(names)], people[names[i % len(names)]], client) for i in range(apps)))

    agents_w = list(agents.items())[:10]
    ths = [threading.Thread(target=writer, args=a, daemon=True) for a in agents_w] + [threading.Thread(target=prober, daemon=True)]
    c0 = cpu_seconds(pid)
    t0 = time.monotonic()
    for th in ths:
        th.start()
    if apps:
        asyncio.run(apps_main())
    for th in ths:
        th.join()
    cpu = (cpu_seconds(pid) - c0) / (time.monotonic() - t0)
    n = sum(len(v) for v in posted.values())
    return {"apps": apps, "messages": n, "list_p50": pct(probe, .5), "list_p95": pct(probe, .95), "list_max": max(probe),
            "cpu": cpu, "heard_p50": pct(heard, .5), "heard_p95": pct(heard, .95), "wakes": len(heard)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", type=int, default=200)
    ap.add_argument("--seconds", type=int, default=40)
    a = ap.parse_args()
    data = tempfile.mkdtemp(prefix="relay-live-")
    port = random.randint(9300, 9900)
    base = f"http://127.0.0.1:{port}"
    p, _ = start(data, port)
    people, agents = seed(base, data, 20, 50)
    p.terminate(); p.wait()
    bulk_messages(data, people, agents, 20000, 400)
    p, took = start(data, port)
    try:
        convs = {}
        for h, tok in people.items():
            convs[h] = [t["thread"] for t in call(base, "GET", "/threads?limit=200", tok) if t["last"]["from"] != "relay"] or [None]
        convs = {h: v for h, v in convs.items() if v[0]}
        people = {h: people[h] for h in convs}
        for apps in (0, a.apps):
            r = run(base, p.pid, people, agents, convs, apps, a.seconds)
            print(f"{r['apps']:>4} apps waiting · {r['messages']} messages · conversation list p50 {r['list_p50']*1000:.0f} ms, "
                  f"p95 {r['list_p95']*1000:.0f} ms, max {r['list_max']*1000:.0f} ms · hub CPU {r['cpu']*100:.0f}%"
                  + (f" · app heard its own news p50 {r['heard_p50']*1000:.0f} ms, p95 {r['heard_p95']*1000:.0f} ms ({r['wakes']} wakes)"
                     if apps else ""), flush=True)
    finally:
        p.terminate()


if __name__ == "__main__":
    main()
