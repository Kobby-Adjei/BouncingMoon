"""Backups that need no paid plan.

- Every night the hub writes a copy of its database to <data>/backups (the last 7 are kept). That
  undoes a bad deploy or a damaged database.
- The owner can download a fresh copy at any time (GET /backup, "Download a backup" in Me) and keep it
  off the server. That one survives losing the volume itself.

A backup is a zip: relay.db (people, agents, passkeys, conversations, sealed keys, wake-ups) and
hub_secret (what unseals them). Media files themselves are not in it; they are too big.
To restore: stop the hub, put both files in the data folder (/data on Railway), start it again.
The zip opens everything on the hub, so keep it as private as a password.
"""
from __future__ import annotations

import os
import sqlite3
import time
import zipfile

KEEP = 7
NOTE = ("Relay backup.\n\nrelay.db and hub_secret go in the hub's data folder (/data on Railway).\n"
        "Stop the hub, put both files there, start it again.\n"
        "Media files are not included.\n\nKeep this file as private as a password.\n")


def copy_db(db_path: str, dest: str) -> None:
    """A consistent copy while the hub keeps running (SQLite's online backup)."""
    src, dst = sqlite3.connect(db_path), sqlite3.connect(dest)
    try:
        with dst:
            src.backup(dst)
    finally:
        dst.close()
        src.close()


def make_zip(data_dir: str, out_path: str) -> str:
    tmp = os.path.join(data_dir, "tmp", f"backup-{os.getpid()}-{time.monotonic_ns()}.db")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    try:
        copy_db(os.path.join(data_dir, "relay.db"), tmp)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            z.write(tmp, "relay.db")
            secret = os.path.join(data_dir, "hub_secret")
            if os.path.exists(secret):
                z.write(secret, "hub_secret")
            z.writestr("README.txt", NOTE)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    os.chmod(out_path, 0o600)
    return out_path


def nightly(data_dir: str, keep: int = KEEP, room=None, now=time.time) -> str | None:
    """Today's copy, once a day; older ones beyond `keep` are removed. None when there was nothing to do."""
    folder = os.path.join(data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    name = time.strftime("relay-%Y-%m-%d.zip", time.gmtime(now()))
    path = os.path.join(folder, name)
    made = None
    if not os.path.exists(path):
        size = os.path.getsize(os.path.join(data_dir, "relay.db"))
        if room is None or room(size):
            made = make_zip(data_dir, path)
    old = sorted(f for f in os.listdir(folder) if f.startswith("relay-") and f.endswith(".zip"))
    for f in old[:-keep] if keep else old:
        os.remove(os.path.join(folder, f))
    return made
