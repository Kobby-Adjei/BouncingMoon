"""The Relay helper: a signed-in Codex or Claude Code on the user's own computer answers Relay
messages by itself. Service files and commands are checked for every platform here; the Linux
path also runs for real, end to end, with a stand-in `codex` (tests/fixtures/fake_codex.py)."""
import json
import os
import plistlib
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET

import pytest
import uvicorn
from fastapi.testclient import TestClient

from relay import cli
from relay.hub import create_app

ADMIN = "helper-admin"
HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CODEX = os.path.join(HERE, "fixtures", "fake_codex.py")
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))
    for k in ("RELAY_URL", "RELAY_TOKEN", "RELAY_CONFIG", "RELAY_AGENT"):
        monkeypatch.delenv(k, raising=False)
    return h


@pytest.fixture
def fake_bin(tmp_path, monkeypatch):
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("codex", "claude"):
        p = d / name
        p.write_text("#!/bin/sh\nexit 0\n")
        p.chmod(0o755)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")
    return d


# ------------------------------------------------------------------ the commands it runs
def test_codex_runs_with_relay_tools_only_and_no_shell(home, fake_bin):
    argv, cwd = cli.helper_argv("codex", "codex", "https://hub.example.com")
    assert argv[0] == str(fake_bin / "codex") and argv[1] == "exec"
    assert argv[-1] == cli.HELPER_PROMPT
    pairs = {argv[i]: argv[i + 1] for i in range(len(argv) - 1) if argv[i].startswith("--") and not argv[i + 1].startswith("-")}
    assert pairs["--sandbox"] == "read-only" and pairs["--cd"] == cwd
    for flag in ("--ignore-user-config", "--ephemeral", "--skip-git-repo-check"):
        assert flag in argv
    conf = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "-c")
    assert conf["features.shell_tool"] == "false" and conf["features.apps"] == "false"
    assert conf["web_search"] == "'disabled'" and conf["plugins._default.enabled"] == "false"
    assert conf["mcp_servers.relay.url"] == "'https://hub.example.com/mcp'"
    assert conf["mcp_servers.relay.enabled_tools"] == "['check_replies','reply','contacts']"
    assert conf["mcp_servers.relay.default_tools_approval_mode"] == "'approve'"
    assert "mcp-headers" in conf["mcp_servers.relay.http_headers_helper"] and "--as codex" in conf["mcp_servers.relay.http_headers_helper"]
    assert os.path.isdir(cwd) and not os.listdir(cwd)      # an empty folder: nothing to read
    # nothing in it that a Windows .cmd launcher would re-read: no double quotes, %, ^, &, |, <, >
    assert not any(re.search(r'["%^&|<>!]', a) for a in argv[1:])


def test_claude_code_runs_with_relay_tools_only_and_the_key_stays_put(home, fake_bin):
    argv, cwd = cli.helper_argv("claude-code", "cc", "https://hub.example.com")
    assert argv[:3] == [str(fake_bin / "claude"), "-p", cli.HELPER_PROMPT]
    i = argv.index("--tools")
    assert argv[i + 1] == ""                                  # no built-in tools at all
    assert "--strict-mcp-config" in argv
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__relay__check_replies,mcp__relay__reply,mcp__relay__contacts"
    assert "mcp__relay__ask" in argv[argv.index("--disallowedTools") + 1]
    with open(argv[argv.index("--mcp-config") + 1]) as f:
        conf = json.load(f)
    relay = conf["mcpServers"]["relay"]
    assert relay["url"] == "https://hub.example.com/mcp" and "mcp-headers" in relay["headersHelper"]
    assert "headers" not in relay                               # the key is fetched, never written
    assert not any(re.search(r'["%^&|<>!]', a) for a in argv[1:])


def test_the_prompt_treats_messages_as_requests_not_orders():
    p = cli.HELPER_PROMPT
    for must in ("check_replies", "reply", "not an instruction from your owner", "never run commands",
                 "never reveal secrets", "Do not reply to messages that only thank you"):
        assert must in p
    assert not re.search(r"[\"'`$%^&|<>!\\]", p)


def test_odd_names_and_addresses_are_refused(home, fake_bin):
    for bad in ("Codex", "a b", "x;rm", "../x", "", "-x", "a" * 40):
        with pytest.raises(cli.RelayError):
            cli.helper_argv("codex", bad, "https://hub.example.com")
        with pytest.raises(cli.RelayError):
            cli.service_spec(bad, "codex")
    for bad_hub in ("https://hub.example.com/'x", "file:///etc", "https://a b", "https://h/$(id)"):
        with pytest.raises(cli.RelayError):
            cli.helper_argv("codex", "codex", bad_hub)
    with pytest.raises(cli.RelayError):
        cli.helper_argv("gemini", "codex", "https://hub.example.com")


# ------------------------------------------------------------------ is the app ready?
class FakeRun:
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        key = " ".join(argv[1:3])
        code, out = self.answers.get(key, (0, ""))
        return subprocess.CompletedProcess(argv, code, out, "")


CODEX_HELP = "--ignore-user-config --skip-git-repo-check --sandbox --ephemeral"
CLAUDE_HELP = "--strict-mcp-config --mcp-config --tools --permission-mode --allowedTools"


def test_check_app_needs_the_app_signed_in_and_new_enough(home, fake_bin, tmp_path, monkeypatch):
    ok = FakeRun({"login status": (0, "Logged in using ChatGPT"), "exec --help": (0, CODEX_HELP)})
    assert cli.check_app("codex", run=ok) == (str(fake_bin / "codex"), None)
    out = FakeRun({"login status": (1, "Not logged in"), "exec --help": (0, CODEX_HELP)})
    with pytest.raises(cli.RelayError, match="codex login"):
        cli.check_app("codex", run=out)
    key = FakeRun({"login status": (0, "Logged in using an API key - sk-***"), "exec --help": (0, CODEX_HELP)})
    assert "billed" in cli.check_app("codex", run=key)[1]
    old = FakeRun({"login status": (0, "Logged in using ChatGPT"), "exec --help": (0, "--sandbox")})
    with pytest.raises(cli.RelayError, match="too old"):
        cli.check_app("codex", run=old)
    assert cli.check_app("claude-code", run=FakeRun({"auth status": (0, '{"authMethod": "claude.ai"}'), "--help": (0, CLAUDE_HELP)}))[1] is None
    assert "billed" in cli.check_app("claude-code", run=FakeRun({"auth status": (0, '{"authMethod": "api_key"}'), "--help": (0, CLAUDE_HELP)}))[1]
    with pytest.raises(cli.RelayError, match="isn't signed in"):
        cli.check_app("claude-code", run=FakeRun({"auth status": (1, '{"loggedIn": false}')}))
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(cli.RelayError, match="isn't installed"):
        cli.check_app("codex", run=ok)


def test_a_signed_out_app_never_uses_up_the_invite(home, fake_bin, monkeypatch):
    def refuse(app, run=None):
        raise cli.RelayError("Codex isn't signed in")
    joined = []
    monkeypatch.setattr(cli, "check_app", refuse)
    monkeypatch.setattr(cli, "join", lambda *a: joined.append(a))
    with pytest.raises(cli.RelayError):
        cli.helper_setup("https://hub.example.com", "inv_abc", "codex")
    assert joined == []


# ------------------------------------------------------------------ service files
ODD = "/Users/Ann O'Neil/100% \"real\" $HOME/a&b<c>"


def spec_with_odd_paths(name="codex"):
    return {"name": name, "app": "codex", "label": f"com.relay.helper.{name}",
            "argv": [ODD + "/python3", ODD + "/cli.py", "--as", name, "listen", "--app", "codex", "--log", ODD + "/x.log"],
            "path": "/opt/homebrew/bin:/usr/bin:" + ODD, "log": ODD + "/x.log", "home": "/Users/ann",
            "workdir": ODD + "/helper"}


def systemd_split(line):
    """Undo systemd's quoting for ExecStart (enough of systemd.syntax for our own output)."""
    out, i = [], 0
    while i < len(line):
        if line[i] == " ":
            i += 1; continue
        assert line[i] == '"'
        i += 1; buf = ""
        while line[i] != '"':
            if line[i] == "\\":
                i += 1
            buf += line[i]; i += 1
        out.append(buf.replace("%%", "%").replace("$$", "$")); i += 1
    return out


def test_systemd_unit_quotes_every_argument():
    unit = cli.systemd_unit(spec_with_odd_paths())
    lines = dict(l.split("=", 1) for l in unit.splitlines() if "=" in l and not l.startswith("["))
    assert systemd_split(lines["ExecStart"]) == spec_with_odd_paths()["argv"]
    assert "Restart=always" in unit and "WantedBy=default.target" in unit
    env = [l for l in unit.splitlines() if l.startswith("Environment=") and "PATH" in l][0]
    assert systemd_split(env.split("=", 1)[1]) == ["PATH=" + spec_with_odd_paths()["path"]]
    bad = spec_with_odd_paths(); bad["argv"] = bad["argv"] + ["a\nExecStartPre=/bin/sh"]
    with pytest.raises(cli.RelayError):
        cli.systemd_unit(bad)


def test_launchd_plist_round_trips_exactly():
    spec = spec_with_odd_paths()
    p = plistlib.loads(cli.launchd_plist(spec))
    assert p["Label"] == "com.relay.helper.codex" and p["ProgramArguments"] == spec["argv"]
    assert p["RunAtLoad"] is True and p["KeepAlive"] is True
    assert p["EnvironmentVariables"]["PATH"] == spec["path"]
    assert p["StandardOutPath"] == spec["log"] == p["StandardErrorPath"]


def test_windows_task_xml_is_valid_and_escaped():
    spec = spec_with_odd_paths()
    spec["argv"][0] = r"C:\Users\Ann & Co\AppData\Local\Programs\Python\Python312\pythonw.exe"
    xml = cli.windows_task_xml(spec, r"DESKTOP\Ann & Co")
    root = ET.fromstring(xml.encode("utf-16"))
    ex = root.find("t:Actions/t:Exec", NS)
    assert ex.find("t:Command", NS).text == spec["argv"][0]
    assert ex.find("t:Arguments", NS).text == subprocess.list2cmdline(spec["argv"][1:])
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == r"DESKTOP\Ann & Co"
    s = root.find("t:Settings", NS)
    assert s.find("t:ExecutionTimeLimit", NS).text == "PT0S"
    assert s.find("t:RestartOnFailure/t:Count", NS).text == "999"
    assert root.find("t:Principals/t:Principal/t:RunLevel", NS).text == "LeastPrivilege"


def test_windows_startup_script_escapes_percent():
    cmd = cli.windows_startup_cmd(spec_with_odd_paths())
    assert cmd.startswith("@echo off\r\nstart \"\" /b ") and "100%% " in cmd and "100% " not in cmd


def test_each_platform_writes_its_service_where_it_belongs(home, tmp_path, monkeypatch):
    calls = FakeRun({})
    spec = dict(cli.service_spec("codex", "codex"), home=str(home))
    mac = cli.install_service(spec, platform="darwin", run=calls)
    assert mac["kind"] == "launchd" and mac["file"] == str(home / "Library/LaunchAgents/com.relay.helper.codex.plist")
    assert plistlib.loads(open(mac["file"], "rb").read())["ProgramArguments"] == spec["argv"]
    assert any(c[:2] == ["launchctl", "bootstrap"] for c in calls.calls)

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("RELAY_HELPER_RUNNER", raising=False)
    monkeypatch.setattr(cli, "_systemd_ok", lambda run=None: True)
    lin = cli.install_service(spec, platform="linux", run=calls)
    assert lin["kind"] == "systemd" and lin["file"] == str(tmp_path / "xdg/systemd/user/relay-helper-codex.service")
    assert ["systemctl", "--user", "enable", "--now", "relay-helper-codex.service"] in calls.calls

    monkeypatch.setenv("USERDOMAIN", "DESKTOP"); monkeypatch.setenv("USERNAME", "ann")
    win = cli.install_service(spec, platform="win32", run=calls)
    assert win["kind"] == "schtasks" and win["task"] == "RelayHelper-codex"
    create = [c for c in calls.calls if c[:2] == ["schtasks", "/Create"]][0]
    assert create[create.index("/XML") + 1] == win["file"]
    root = ET.fromstring(open(win["file"], "rb").read())
    assert root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text == "DESKTOP\\ann"

    off = []
    cli.uninstall_service("codex", mac, run=lambda a, **k: off.append(a) or subprocess.CompletedProcess(a, 0))
    assert not os.path.exists(mac["file"]) and any(a[:2] == ["launchctl", "bootout"] for a in off)


# ------------------------------------------------------------------ the listener
class FakeHub:
    url = "https://hub.example.com"

    def __init__(self, msgs):
        self.msgs, self.puts = msgs, []

    def get(self, path, **kw):
        return {"handle": "codex"} if path == "/agents/me" else {"messages": self.msgs, "cursor": 0}

    def put(self, path, body):
        self.puts.append((path, body))


def test_a_failed_wake_is_retried_not_forgotten(home, fake_bin, monkeypatch):
    hub = FakeHub([{"id": 5, "from": "asker", "text": "hi"}])
    monkeypatch.setattr(cli, "_wake", lambda *a: 1)
    cli.listen(hub, app="codex", once=True, log=lambda m: None)
    assert not open(os.path.join(cli.relay_dir(), "codex.listen")).read().strip() == "5"
    monkeypatch.setattr(cli, "_wake", lambda *a: 0)
    cli.listen(hub, app="codex", once=True, log=lambda m: None)
    assert open(os.path.join(cli.relay_dir(), "codex.listen")).read().strip() == "5"
    assert ("/agents/me/listener", {"app": "codex", "woke": None}) in hub.puts
    assert cli._read_json(cli.helper_files("codex")["state"])["last_exit"] == 0


def test_wakes_are_capped_per_hour(home, fake_bin, monkeypatch):
    hub = FakeHub([{"id": 9, "from": "asker", "text": "again"}])
    woke = []
    monkeypatch.setattr(cli, "_wake", lambda *a: woke.append(1) or 0)

    class Stop(Exception):
        pass

    def no_sleep(s):
        raise Stop
    monkeypatch.setattr(cli.time, "sleep", no_sleep)
    with pytest.raises(Stop):
        cli.listen(hub, app="codex", max_wakes=0, log=lambda m: None)
    assert woke == []


# ------------------------------------------------------------------ the one-line installers
@pytest.fixture
def hub(tmp_path):
    return TestClient(create_app(data_dir=str(tmp_path / "hubdata"), admin_token=ADMIN))


def test_installers_are_served_with_this_hubs_address_and_no_secrets(hub):
    for path in ("/install.sh", "/static/install.sh", "/install.ps1", "/static/install.ps1"):
        r = hub.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"] == "text/plain; charset=utf-8"
        assert r.headers["x-content-type-options"] == "nosniff"
        body = r.text
        assert "http://testserver" in body and "__RELAY_HUB__" not in body
        assert ADMIN not in body and not re.search(r"rly_[A-Za-z0-9_-]{20}|inv_[A-Za-z0-9_-]{20}", body)
    sh = hub.get("/install.sh").text
    assert sh.startswith("#!/bin/sh") and sh.rstrip().splitlines()[-1] == 'relay_install "$@"'
    assert "helper setup" in sh and "rm " not in sh
    assert subprocess.run(["sh", "-n"], input=sh, text=True).returncode == 0
    ps = hub.get("/install.ps1").text
    assert ps.rstrip().splitlines()[-1] == "Install-RelayHelper" and "param(" in ps and "helper setup" in ps
    assert hub.get("/install.sh", headers={"host": "evil.example.com'$(id)"}).status_code == 400


def test_the_hub_shows_when_the_helper_is_running(hub):
    inv = hub.post("/invites", json={"handle": "codex", "hours": 1}, headers={"Authorization": f"Bearer {ADMIN}"}).json()
    tok = hub.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    H = {"Authorization": f"Bearer {tok}"}
    agent = lambda: next(a for a in hub.get("/agents", headers=H).json() if a["handle"] == "codex")
    assert agent()["listener"] is None
    hub.get("/inbox", params={"wait": 0.1}, headers=H)
    assert agent()["listener"] is None                      # a plain long-poll is not the helper
    r = hub.put("/agents/me/listener", json={"app": "codex", "woke": None}, headers=H).json()
    assert r["app"] == "codex" and r["seen"] and r["woke"] is None
    hub.put("/agents/me/listener", json={"app": "codex", "woke": time.time() + 9999}, headers=H)
    lis = agent()["listener"]
    assert lis["woke"] <= time.time() + 1                   # never in the future
    assert hub.put("/agents/me/listener", json={"app": "rm -rf"}, headers=H).status_code == 400
    assert hub.put("/agents/me/listener", json={"app": "codex"},
                   headers={"Authorization": f"Bearer {ADMIN}"}).status_code == 403


# ------------------------------------------------------------------ end to end, for real
def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="POSIX shell installer")
def test_one_line_install_then_a_message_gets_answered(tmp_path):
    port = free_port()
    app = create_app(data_dir=str(tmp_path / "hub"), admin_token=ADMIN)
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"

    def admin(path, body):
        req = urllib.request.Request(url + path, method="POST", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {ADMIN}", "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            return json.load(r)
    code = admin("/invites", {"handle": "codex", "hours": 1, "provider": "codex"})["code"]
    asker = admin("/invites", {"handle": "asker", "hours": 1})["code"]

    home, bindir = tmp_path / "home", tmp_path / "bin"
    home.mkdir(); bindir.mkdir()
    shutil.copy(FAKE_CODEX, bindir / "codex"); (bindir / "codex").chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("RELAY_")}
    env.update(HOME=str(home), PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
               FAKE_CODEX_LOG=str(tmp_path / "codex.log"), RELAY_HELPER_RUNNER="background")
    with urllib.request.urlopen(url + "/install.sh") as r:
        script = r.read().decode()
    run = subprocess.run(["sh", "-s", "--", code, "codex"], input=script, env=env,
                         capture_output=True, text=True, timeout=120)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "Done \u2014 codex is listening" in run.stdout
    assert "rly_" not in run.stdout + run.stderr             # the key is never printed
    relay_cli = home / ".relay" / "bin" / "cli.py"
    assert relay_cli.read_text() == open(cli.__file__).read()
    assert "[mcp_servers.relay]" in (home / ".codex" / "config.toml").read_text()
    try:
        # someone else writes to codex; the helper wakes "codex", which answers through Relay
        other = tmp_path / "other"; other.mkdir()
        oenv = dict(env, HOME=str(other))
        o = lambda *a: subprocess.run([sys.executable, str(relay_cli), *a], env=oenv,
                                      capture_output=True, text=True, timeout=60)
        assert o("join", url, asker).returncode == 0
        t0 = time.time()
        assert o("post", "--channel", "general", "--text", "hi codex", "--mention", "codex").returncode == 0
        answer = ""
        while time.time() - t0 < 15 and "fake codex answering" not in answer:
            time.sleep(0.3)
            answer = o("inbox").stdout
        assert "fake codex answering" in answer, answer
        assert time.time() - t0 < 15
        calls = [json.loads(l) for l in open(tmp_path / "codex.log")]
        argv = calls[-1]["argv"]
        assert argv[argv.index("--sandbox") + 1] == "read-only" and "features.shell_tool=false" in argv
        assert not any("rly_" in a for a in argv)
        status = subprocess.run([sys.executable, str(relay_cli), "--as", "codex", "helper", "status"],
                                env=env, capture_output=True, text=True).stdout
        assert "running" in status and "exit 0" in status
        # running the line again (say, after a restart) reuses the joined agent and never leaves two
        again = subprocess.run(["sh", "-s", "--", code, "codex"], input=script, env=env,
                               capture_output=True, text=True, timeout=120)
        assert again.returncode == 0 and "already joined" in again.stdout, again.stdout + again.stderr
        procs = subprocess.run(["pgrep", "-f", f"{relay_cli} --as codex listen"], capture_output=True, text=True).stdout.split()
        assert len(procs) == 1, procs
    finally:
        out = subprocess.run([sys.executable, str(relay_cli), "--as", "codex", "helper", "uninstall"],
                             env=env, capture_output=True, text=True)
        srv.should_exit = True
    assert "stopped process" in out.stdout, out.stdout + out.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_hung_app_is_stopped_with_everything_it_started(home, tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    monkeypatch.setattr(cli, "helper_argv", lambda *a, **k: (
        ["sh", "-c", f"sleep 60 & echo $! > {pidfile}; wait"], str(tmp_path)))
    code = cli._wake(None, "codex", FakeHub([]), "codex", dict(os.environ), None, 1.5, lambda m: None)
    assert code == 124
    child = int(pidfile.read_text())
    time.sleep(0.3)
    try:                                   # gone, or a zombie waiting to be reaped
        state = open(f"/proc/{child}/stat").read().rsplit(")", 1)[1].split()[0]
    except OSError:
        state = "gone"
    assert state in ("gone", "Z"), state


def test_run_mode_keeps_its_old_behaviour(home, monkeypatch):
    hub = FakeHub([{"id": 5, "from": "asker", "text": "hi"}])
    cli.listen(hub, run="exit 3", once=True, log=lambda m: None)
    assert open(os.path.join(cli.relay_dir(), "codex.listen")).read().strip() == "5"
    assert hub.puts == []                                   # only the helper reports in
