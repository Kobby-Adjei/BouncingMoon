#!/usr/bin/env bash
# Relay acceptance demo, CLI only.
#   An agent that cannot download asks for media by CAPABILITY.
#   A node that can (here: simulated; for real: Muse's machine) fetches and digests it.
#   The requester is told in its inbox and can now see the video without watching it.
# Usage (from the repo root):  relay/demo.sh <media-url> [out-dir]
set -euo pipefail
MEDIA="${1:?usage: relay/demo.sh <media-url> [out-dir]}"
OUT="${2:-./relay-demo-out}"; mkdir -p "$OUT"
PORT="${PORT:-8787}"; DATA="$(mktemp -d)"
export RELAY_ADMIN_TOKEN="$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')"
R="python3 -m relay.cli --url http://127.0.0.1:$PORT"

RELAY_DATA="$DATA" python3 -m uvicorn relay.hub:app --port "$PORT" --log-level warning & HUB=$!
trap 'kill $HUB 2>/dev/null; rm -rf "$DATA"' EXIT
until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null; do sleep 0.2; done

token() { $R --token "$RELAY_ADMIN_TOKEN" register "$@" | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])'; }
CLAUDE="$(token claude)"
NODE="$(token muse-laptop)"

echo "== 1. claude asks for the video, addressed to the capability 'fetch' (not to anyone by name)"
JOB=$($R --token "$CLAUDE" job fetch --param "url=$MEDIA" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
echo "   job $JOB queued"

echo "== 2. a node that CAN fetch wakes up and does the work (fetch, then the digest it triggers)"
$R --token "$NODE" node --cap fetch --cap digest --once --poll 0.2

echo "== 3. claude's inbox"
$R --token "$CLAUDE" inbox --ack

echo "== 4. the jobs"
$R --token "$CLAUDE" jobs

echo "== 5. what claude can now see"
SRC=$($R --token "$CLAUDE" job get "$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["artifacts"][0])')
$R --token "$CLAUDE" meta "$SRC" > "$OUT/meta.json"
python3 - "$OUT/meta.json" <<'PY'
import json,sys
m=json.load(open(sys.argv[1])); d=m["meta"]["digest"]["result"]
print(f"   source  {m['name']}  {m['size']/1e6:.1f} MB  sha256 {m['id'][:16]}...")
print(f"   digest  {d['duration']:.1f}s  {d['width']}x{d['height']}  {d['shots']} shots  audio={d['has_audio']}")
for x in m["derived"]: print(f"   derived {x['kind']:<14} {x['name']}  {x['size']/1e3:.0f} KB")
PY
SHEET=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["meta"]["digest"]["result"]["contact_sheet"])' "$OUT/meta.json")
$R --token "$CLAUDE" download "$SHEET" -o "$OUT/contact_sheet.jpg" >/dev/null
echo "   contact sheet -> $OUT/contact_sheet.jpg"
