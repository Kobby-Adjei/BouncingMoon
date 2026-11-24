"""The installed helper keeps itself up to date from the hub: customers never re-install for a fix.
These cover the checks every update gets; test_release.py covers the signature on top (keys=())."""
import hashlib
import os
import shutil

import pytest
from fastapi.testclient import TestClient

from relay import cli
from relay.cli import RelayError
from relay.hub import create_app

CLI = cli._cli_path()
SOURCE = open(CLI, "rb").read()
NEWER = SOURCE + b"\n# a newer release\n"


def sha(b):
    return hashlib.sha256(b).hexdigest()


class Hub:
    url = "https://hub.example.com"

    def __init__(self, serves):
        self.serves = serves

    def get(self, path, **kw):
        if self.serves is None:
            raise RelayError("GET /client -> 404: Not Found")      # a hub from before self-update
        return {"sha256": sha(self.serves)}


@pytest.fixture
def installed(tmp_path):
    p = tmp_path / "bin" / "cli.py"
    p.parent.mkdir()
    shutil.copy2(CLI, p)
    return str(p)


def test_a_newer_helper_on_the_hub_replaces_this_one_and_the_old_is_kept(installed):
    logs = []
    assert cli.self_update(Hub(NEWER), logs.append, path=installed, fetch=lambda url: NEWER, keys=())
    assert open(installed, "rb").read() == NEWER
    assert open(installed + ".prev", "rb").read() == SOURCE
    assert not os.path.exists(installed + ".part")
    assert os.stat(installed).st_mode & 0o777 == os.stat(CLI).st_mode & 0o777
    assert "updated the Relay helper" in logs[-1]


def test_nothing_happens_when_it_is_already_current_or_the_hub_is_older(installed):
    fetched = []
    assert not cli.self_update(Hub(SOURCE), path=installed, fetch=fetched.append, keys=())
    assert not cli.self_update(Hub(None), path=installed, fetch=fetched.append, keys=())
    assert fetched == [] and open(installed, "rb").read() == SOURCE


@pytest.mark.parametrize("served, download, why", [
    (NEWER, NEWER + b"tampered", "did not match"),                          # not what the hub announced
    (b"#!/usr/bin/env python3\nprint('hi')\n",) * 2 + ("keep itself updated",),   # could never update again
    (SOURCE + b"\ndef broken(:\n",) * 2 + ("SyntaxError",),                # not valid Python
    (b"#!/usr/bin/env python3\nimport sys\ndef self_update(): pass\nsys.exit(3)\n",) * 2 + ("did not start",),
])
def test_a_bad_download_never_replaces_the_working_helper(installed, served, download, why):
    logs = []
    assert not cli.self_update(Hub(served), logs.append, path=installed, fetch=lambda url: download, keys=())
    assert open(installed, "rb").read() == SOURCE
    assert not os.path.exists(installed + ".part") or why == "SyntaxError"
    assert why in logs[-1]


def test_the_hub_announces_exactly_the_helper_it_serves(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token="x"))
    info, body = c.get("/client").json(), c.get("/cli.py").content
    assert info["sha256"] == sha(body) and info["size"] == len(body)


def test_the_running_helper_checks_at_start_and_restarts_on_the_new_version(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("RELAY_NO_AUTO_UPDATE", raising=False)

    class Quiet:
        url = "https://hub.example.com"
        def get(self, path, **kw):
            return {"handle": "codex"} if path == "/agents/me" else {"messages": [], "cursor": 0}
        def put(self, *a):
            pass

    checked, restarted = [], []
    monkeypatch.setattr(cli, "self_update", lambda hub, log: checked.append(1) or True)
    monkeypatch.setattr(cli, "_report", lambda *a: None)
    cli.listen(Quiet(), app="codex", log=lambda m: None, restart=lambda lock, out: restarted.append(lock))
    assert checked == [1] and len(restarted) == 1 and restarted[0] is not None   # handed its lock over

    checked.clear()
    cli._write_json(cli.helper_files("codex")["config"], {"auto_update": False})  # the owner turned it off
    assert not cli.auto_update_on("codex")
    def inbox_stops(self, path, **kw):            # the first inbox poll comes after the update check
        if path == "/agents/me":
            return {"handle": "codex"}
        raise KeyboardInterrupt
    Quiet.get = inbox_stops
    with pytest.raises(KeyboardInterrupt):
        cli.listen(Quiet(), app="codex", log=lambda m: None, restart=lambda *a: restarted.append(1))
    assert checked == []
