#!/usr/bin/env bash
# Photograph every screen of Relay: phone (light and dark) and desktop, with sample data.
# Run before handing over any UI change:   relay/tools/screens.sh [out-dir]
# With --heavy: a hub filled up like a real one (dozens of conversations, long links, raw data, code).
# Needs node with Playwright (and Python with Pillow for the contact sheets).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
heavy=""; [ "${1:-}" = "--heavy" ] && { heavy=1; shift; }
out="${1:-$repo/relay-screens}"
data="$(mktemp -d)"
port="${RELAY_SCREENS_PORT:-$((8800 + RANDOM % 900))}"   # its own port: two runs never collide
mkdir -p "$out"
cd "$repo"
RELAY_DATA="$data" RELAY_BACKUPS=0 python -m uvicorn relay.hub:app --port "$port" >"$out/hub.log" 2>&1 &
pid=$!
trap 'kill $pid 2>/dev/null; rm -rf "$data"' EXIT
for _ in $(seq 1 40); do curl -sf "http://localhost:$port/health/live" >/dev/null && break; sleep 0.25; done
if [ -n "$heavy" ]; then
  node "$here/heavy.js" "http://localhost:$port" "$data" "$out"
else
  node "$here/screens.js" "http://localhost:$port" "$data" "$out"
  python3 "$here/sheets.py" "$out" || echo "(contact sheets skipped: needs Pillow)"
fi
echo "Screens in $out"
