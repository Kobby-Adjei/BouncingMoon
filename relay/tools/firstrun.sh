#!/usr/bin/env bash
# Walk the first five minutes as a brand-new owner and a brand-new friend, on a phone and a desktop.
# Usage: relay/tools/firstrun.sh [out-dir]      Photos and what each screen said land in out-dir.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
out="${1:-$repo/relay-firstrun}"
mkdir -p "$out"
cd "$repo"
for kind in phone desktop; do
  data="$(mktemp -d)"
  port="$((8800 + RANDOM % 900))"
  RELAY_DATA="$data" RELAY_BACKUPS=0 python -m uvicorn relay.hub:app --port "$port" >"$out/hub-$kind.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 40); do curl -sf "http://localhost:$port/health/live" >/dev/null && break; sleep 0.25; done
  node "$here/firstrun.js" "http://localhost:$port" "$data" "$out" "$kind" || true
  kill $pid 2>/dev/null || true; rm -rf "$data"
done
echo "First run in $out"
