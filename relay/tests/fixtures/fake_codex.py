#!/usr/bin/env python3
"""A stand-in for the `codex` command, for the Relay helper's end-to-end test.

It answers `codex login status` and `codex exec --help` like Codex does, and for
`codex exec ... <prompt>` it does what a real Codex would do with the Relay connector: it runs
the http_headers_helper it was given, then calls Relay's MCP tools check_replies and reply.
It records the argv it got (never the key) to $FAKE_CODEX_LOG so the test can check the
safety flags."""
import json
import os
import subprocess
import sys
import urllib.request

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in using ChatGPT")
    sys.exit(0)
if args[:1] == ["exec"] and "--help" in args:
    print("--sandbox --ephemeral --skip-git-repo-check --ignore-user-config --cd -c --color")
    sys.exit(0)
if args[:1] != ["exec"]:
    sys.exit(2)

log = os.environ.get("FAKE_CODEX_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": args, "cwd": os.getcwd()}) + "\n")

conf = {}
for i, a in enumerate(args):
    if a == "-c":
        k, _, v = args[i + 1].partition("=")
        conf[k] = v
unq = lambda v: v[1:-1] if v[:1] in "'\"" else v
url = unq(conf["mcp_servers.relay.url"])
helper = unq(conf["mcp_servers.relay.http_headers_helper"])
headers = json.loads(subprocess.run(helper, shell=True, capture_output=True, text=True, check=True).stdout)


def call(method, params, mid=[0]):
    mid[0] += 1
    req = urllib.request.Request(url, method="POST", data=json.dumps(
        {"jsonrpc": "2.0", "id": mid[0], "method": method, "params": params}).encode(),
        headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)["result"]


call("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "codex-fake"}})
got = call("tools/call", {"name": "check_replies", "arguments": {}})
for m in got["structuredContent"]["messages"]:
    call("tools/call", {"name": "reply", "arguments": {"message_id": m["id"],
                                                       "text": f"fake codex answering #{m['id']}"}})
print("answered", len(got["structuredContent"]["messages"]))
