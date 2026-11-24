#!/usr/bin/env python3
"""Update every Relay helper on this computer to the latest signed release, in one go.

    curl -fsSL __RELAY_HUB__/update-helpers.py -o update-helpers.py
    python3 update-helpers.py            # shows what it found, asks, then updates
    python3 update-helpers.py --check    # only show what it found; change nothing
    python3 update-helpers.py --yes      # don't ask

What it does, for every agent that joined from this computer and has a helper:
  1. finds the agent and the app its helper wakes (~/.relay/agents/<name>.json, <name>.helper.json)
  2. downloads the Relay client from the hub once, checks it is exactly the file the hub announces
     (sha256) and that the owner's release key signed it; anything else is refused
  3. installs it to ~/.relay/bin/cli.py (the old one is kept as cli.py.prev)
  4. re-runs the helper's own setup with the agent's name: the old helper is stopped and the new
     one started. No invite code is needed and the agent's key is not changed.
  5. checks the helper is running and now keeps itself updated

Needs only Python 3.8+. Works on macOS, Linux and Windows. Your keys are read only to find the
hub's address; they are never printed or sent anywhere except to that hub by the helper itself.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

DEFAULT_HUB = "__RELAY_HUB__"
# The owner's release key: only a client signed with it is installed.
RELEASE_KEYS = ("bfe767af03a2b83e89160bccebbea800ae836d55c62dac42e1c571eedc5c9708",)
AGENTS = os.path.join(os.path.expanduser("~"), ".relay", "agents")
CLI = os.path.join(os.path.expanduser("~"), ".relay", "bin", "cli.py")
NOT_AGENT = (".helper.json", ".helper-state.json", ".claude-mcp.json")


# ------------------------------------------------------------------ Ed25519 verification (RFC 8032)
P = 2 ** 255 - 19
Q = 2 ** 252 + 27742317777372353535851937790883648493
D = -121665 * pow(121666, P - 2, P) % P
SQ = pow(2, (P - 1) // 4, P)


def _add(a, b):
    A, B = (a[1] - a[0]) * (b[1] - b[0]) % P, (a[1] + a[0]) * (b[1] + b[0]) % P
    C, E = 2 * a[3] * b[3] * D % P, 2 * a[2] * b[2] % P
    e, f, g, h = B - A, E - C, E + C, B + A
    return (e * f, g * h, f * g, e * h)


def _mul(n, pt):
    r = (0, 1, 1, 0)
    while n > 0:
        if n & 1:
            r = _add(r, pt)
        pt, n = _add(pt, pt), n >> 1
    return r


def _x(y, sign):
    if y >= P:
        return None
    x2 = (y * y - 1) * pow(D * y * y + 1, P - 2, P)
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (P + 3) // 8, P)
    if (x * x - x2) % P:
        x = x * SQ % P
    if (x * x - x2) % P:
        return None
    return P - x if (x & 1) != sign else x


GY = 4 * pow(5, P - 2, P) % P
G = (_x(GY, 0), GY, 1, _x(GY, 0) * GY % P)


def _dec(b):
    y = int.from_bytes(b, "little")
    s, y = y >> 255, y & ((1 << 255) - 1)
    x = _x(y, s)
    return None if x is None else (x, y, 1, x * y % P)


def verify(pub: bytes, msg: bytes, sig: bytes) -> bool:
    if len(pub) != 32 or len(sig) != 64:
        return False
    A, R = _dec(pub), _dec(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if A is None or R is None or s >= Q:
        return False
    h = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % Q
    left, right = _mul(s, G), _add(R, _mul(h, A))
    return (left[0] * right[2] - right[0] * left[2]) % P == 0 and (left[1] * right[2] - right[1] * left[2]) % P == 0


# ------------------------------------------------------------------ what is on this computer
def read_json(path):
    try:
        with open(path) as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def release_of(text):
    m = re.search(r"^HELPER_RELEASE = (\d+)$", text, re.M)
    return int(m.group(1)) if m else None


def installed_client():
    """(release, keeps itself updated), or None when no Relay client is installed here."""
    try:
        text = open(CLI, encoding="utf-8").read()
    except OSError:
        return None
    return release_of(text), "def self_update(" in text


def find_helpers():
    """[(name, app, hub)] for each agent that joined here and has a helper; plus agents without one."""
    found, bare = [], []
    if not os.path.isdir(AGENTS):
        return found, bare
    for f in sorted(os.listdir(AGENTS)):
        if not f.endswith(".json") or f.endswith(NOT_AGENT):
            continue
        name = f[:-5]
        agent = read_json(os.path.join(AGENTS, f))
        helper = read_json(os.path.join(AGENTS, f"{name}.helper.json"))
        if not helper.get("app"):
            bare.append(name)
            continue
        found.append((name, helper["app"], (agent.get("url") or DEFAULT_HUB).rstrip("/")))
    return found, bare


# ------------------------------------------------------------------ the signed client
def fetch(url, limit=5 * 2 ** 20):
    req = urllib.request.Request(url, headers={"User-Agent": "relay-update-helpers"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise SystemExit(f"{url} is unexpectedly large; stopping")
    return data


def signed_client(hub, allow_unsigned=False):
    """The hub's client, only if it is exactly what the hub announces and the release key signed it."""
    info = json.loads(fetch(hub + "/client"))
    data = fetch(hub + "/cli.py")
    sha = hashlib.sha256(data).hexdigest()
    if sha != info.get("sha256"):
        raise SystemExit("the download doesn't match what the hub announced; nothing was changed")
    release, sig = release_of(data.decode("utf-8", "replace")), info.get("signed") or {}
    ok = (sig.get("sha256") == sha and sig.get("release") == release and isinstance(release, int)
          and any(verify(bytes.fromhex(k), f"relay-helper-release\n{release}\n{sha}\n".encode(),
                         bytes.fromhex(str(sig.get("signature", "")) or "00")) for k in RELEASE_KEYS))
    if not ok and not allow_unsigned:
        raise SystemExit(f"release {release} on {hub} is not signed with the owner's release key; "
                         "nothing was changed (sign it, or run with --allow-unsigned)")
    if "def self_update(" not in data.decode("utf-8", "replace"):
        raise SystemExit("the hub's client can't keep itself updated; nothing was changed")
    return data, release, sha, ok


def install_client(data):
    os.makedirs(os.path.dirname(CLI), exist_ok=True)
    part = CLI + ".part"
    with open(part, "wb") as f:
        f.write(data)
    probe = subprocess.run([sys.executable, part, "--help"], capture_output=True, timeout=60)
    if probe.returncode != 0:
        os.remove(part)
        raise SystemExit("the new client didn't start on this computer; nothing was changed")
    if os.path.exists(CLI):
        shutil.copy2(CLI, CLI + ".prev")
    os.replace(part, CLI)
    if os.name != "nt":
        os.chmod(CLI, 0o700)


def status(name):
    out = subprocess.run([sys.executable, CLI, "--as", name, "helper", "status"],
                         capture_output=True, text=True, timeout=120)
    return out.stdout + out.stderr


# ------------------------------------------------------------------ run
def main(argv=None):
    ap = argparse.ArgumentParser(description="Update every Relay helper on this computer to the latest signed release.")
    ap.add_argument("--check", action="store_true", help="only show what was found; change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask before updating")
    ap.add_argument("--allow-unsigned", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)

    helpers, bare = find_helpers()
    client = installed_client()
    if client is None:
        print(f"No Relay client is installed on this computer ({CLI} doesn't exist).")
    else:
        release, updates_itself = client
        print(f"Relay client on this computer: {CLI}")
        print(f"  release {release if release else 'from before releases were numbered'}; "
              f"{'keeps itself updated' if updates_itself else 'does NOT keep itself updated'}")
    if not helpers:
        print("No Relay helper is installed on this computer. Nothing to update.")
        if bare:
            print(f"(Agents that joined here without a helper: {', '.join(bare)}.)")
        return 0
    print("Helpers found:")
    for name, app, hub in helpers:
        print(f"  @{name}: wakes {app}, hub {hub}")
    hubs = sorted({h for _, _, h in helpers})
    if len(hubs) > 1:
        print(f"note: these helpers use {len(hubs)} hubs; the client comes from {hubs[0]} (it is the same program)")

    data, new_release, sha, signed = signed_client(hubs[0], a.allow_unsigned)
    print(f"Latest on the hub: release {new_release} ({sha[:12]}), "
          f"{'signed by the release key' if signed else 'NOT signed'}")
    if a.check:
        return 0
    if not a.yes:
        if input(f"Update {len(helpers)} helper(s) to release {new_release}? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1

    install_client(data)
    print(f"Installed release {new_release} to {CLI} (the previous one is {CLI}.prev)")
    failed = []
    for name, app, hub in helpers:
        print(f"\n--- @{name} ({app}) ---")
        r = subprocess.run([sys.executable, CLI, "helper", "setup", hub, name, "--app", app], timeout=300)
        st = status(name)
        print(st.rstrip())
        running = st.split("\n", 1)[0].split(": ", 1)[-1].startswith("running")
        if r.returncode != 0 or not running or "updates itself from the hub: on" not in st:
            failed.append(name)
    print()
    if failed:
        print(f"Needs a look: {', '.join('@' + n for n in failed)}. Send the output above to whoever runs your hub.")
        return 1
    print(f"Done: {len(helpers)} helper(s) on release {new_release}, running, and keeping themselves updated. "
          "No more re-installs needed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
