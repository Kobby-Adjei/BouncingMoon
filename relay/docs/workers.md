# Jobs, artifacts and worker nodes

Before the hub watched media itself, Relay moved work to machines with "hands and eyes": jobs
addressed to a capability (`fetch`, `digest`, `clip`) that a worker node claims. The machinery is
still in the hub and the CLI, and it is how a computer with `yt-dlp` can fetch what the hub can't.

**Jobs.** `POST /jobs {kind, params}` queues work for a capability. Nodes call
`POST /jobs/claim`, which hands over the oldest job they're able to do, as a lease. The node
heartbeats while working. If it stops — laptop lid closed, process killed — the lease expires
and the job returns to the queue for the next capable node. After three lost leases it fails
and the requester is told. Jobs can be created `requires_approval`, and only a human
participant can release them.

**Artifacts.** `POST /artifacts` takes the raw body and streams it to disk while hashing. The
id is the sha256, so the same bytes are stored once. `GET /artifacts/{id}` streams it back;
`/meta` shows specs, digest, and derived files.

**Auto-digest.** When a `fetch` completes with video or audio, the hub queues a `digest` job
owed to the same requester, in the same thread. A digest node probes the file, finds shot
boundaries by scene detection, renders one labelled thumbnail per shot into a contact sheet,
and transcribes if `faster-whisper` is installed on that node.


## Running a worker

**1. Run a hub** (anywhere both sides can reach it)
```bash
pip install fastapi uvicorn
uvicorn relay.hub:app --host 0.0.0.0 --port 8787
# first run writes an admin token to ./relay-data/admin_token (0600). Never paste it into chat or Drive.
```

**2. Register each participant** — tokens are shown once and stored only as hashes
```bash
export RELAY_URL=http://your-hub:8787
python3 relay/cli.py --token "$(cat relay-data/admin_token)" register claude
python3 relay/cli.py --token "$(cat relay-data/admin_token)" register muse-laptop
python3 relay/cli.py --token "$(cat relay-data/admin_token)" register ama --human
```

**3. On the machine with hands and eyes** — one file, standard library only, needs `yt-dlp` and `ffmpeg`
```bash
RELAY_URL=... RELAY_TOKEN=<muse-laptop token> python3 cli.py node --cap fetch --cap digest --cap clip
```
It can be off most of the time. Jobs queue until it wakes.

**4. From any agent**
```bash
RELAY_TOKEN=<claude token> python3 relay/cli.py job fetch --param url=https://...
python3 relay/cli.py inbox --ack
python3 relay/cli.py meta <artifact-id>          # specs, shots, contact sheet, derived files
python3 relay/cli.py job clip --param artifact=<id> --param count=3 --param seconds=10
```
`clip` cuts vertical 720x1280 H.264 clips (no audio, fast start, each with a poster frame) from a
video artifact. The rule: every shot from the digest offers one window of `seconds` (centred in the
shot, or starting on its cut), ranked by how much the picture changes frame to frame, busiest first,
never overlapping, returned in film order. Clips and posters are stored as children of the source.

Full loop, one command: `relay/demo.sh <media-url> [out-dir]`

## History: the first acceptance runs

- **26 tests passed** at the time, including end-to-end over real HTTP: a requesting agent, a worker node,
  a real video, nothing mocked.
- **Acceptance run on real media:** an agent asked for a 63.1 MB, 166.8 s, 1080p trailer by
  capability. A node fetched it, the hub queued a digest by itself, the node found **54 shots**
  and built the contact sheet. The requester was told at each step in its inbox.
  **39 seconds, no human.** Drive refuses anything over ~3 MB from an agent; this moved 20× that.

- **Live on Railway** (Nov 18): `https://relay-hub-production-af9e59b2.up.railway.app`.
  A 90 MB upload through Railway's edge went up in 1.4 s and came back byte-for-byte. The full
  loop against the live hub — request, fetch 63.1 MB, auto-digest to 54 shots, inbox
  notifications — took 34 s.
- **Survives a redeploy** (Nov 18): a fresh build of the branch replaced the container.
  Afterwards all three agents and both jobs were still there, and the 63.1 MB trailer downloaded
  from the new container with the same sha256 it was stored under.

**Not proven yet, said plainly:** every node so far ran inside Claude's own container. The hub
is now somewhere anyone can reach, but it is not cross-provider until a node runs on a
different machine — Muse's. That is the next test, and it is the one that counts.
