"""update-helpers.py: one script that updates every helper on a computer, only to a signed release."""
import hashlib
import importlib.util
import json
import os

import pytest
from fastapi.testclient import TestClient

from relay import cli
from relay.hub import create_app

WEB = os.path.join(os.path.dirname(cli._cli_path()), "web", "update-helpers.py")
SIG = json.load(open(os.path.join(os.path.dirname(cli._cli_path()), cli.SIG_FILE))) or {}
SOURCE = open(cli._cli_path(), "rb").read()


@pytest.fixture
def script(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("update_helpers", WEB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "AGENTS", str(tmp_path / "agents"))
    monkeypatch.setattr(mod, "CLI", str(tmp_path / "bin" / "cli.py"))
    return mod


def test_it_trusts_exactly_the_key_built_into_the_helper(script):
    assert script.RELEASE_KEYS == cli.RELEASE_KEYS
    seed = os.urandom(32)
    pub, sig = cli.ed25519_public(seed), cli.ed25519_sign(seed, b"m")
    assert script.verify(pub, b"m", sig) and not script.verify(pub, b"n", sig)


def test_it_finds_each_helper_and_the_app_it_wakes(script, tmp_path):
    a = tmp_path / "agents"
    a.mkdir()
    (a / "muse.json").write_text(json.dumps({"url": "https://hub.example/", "handle": "muse", "token": "t"}))
    (a / "muse.helper.json").write_text(json.dumps({"app": "codex"}))
    (a / "muse.helper-state.json").write_text("{}")
    (a / "codex.json").write_text(json.dumps({"url": "https://hub.example", "handle": "codex", "token": "t"}))
    assert script.find_helpers() == ([("muse", "codex", "https://hub.example")], ["codex"])


def serve(script, monkeypatch, data, signed):
    info = {"sha256": hashlib.sha256(data).hexdigest(), "signed": signed}
    monkeypatch.setattr(script, "fetch", lambda url, limit=0: json.dumps(info).encode() if url.endswith("/client") else data)


@pytest.mark.skipif(SIG.get("sha256") != hashlib.sha256(SOURCE).hexdigest(), reason="this cli.py is not signed yet")
def test_the_owners_signed_release_is_accepted(script, monkeypatch):
    serve(script, monkeypatch, SOURCE, SIG)
    data, release, sha, ok = script.signed_client("https://hub.example")
    assert ok and release == cli.HELPER_RELEASE and data == SOURCE


@pytest.mark.parametrize("case", ["unsigned", "signed by someone else", "signature for another file", "tampered"])
def test_anything_not_signed_by_the_owner_is_refused(script, monkeypatch, case):
    data = SOURCE
    seed = os.urandom(32)
    sha = hashlib.sha256(data).hexdigest()
    signed = {"unsigned": None,
              "signed by someone else": {"release": cli.HELPER_RELEASE, "sha256": sha, "signature":
                                         cli.ed25519_sign(seed, cli.release_message(cli.HELPER_RELEASE, sha)).hex()},
              "signature for another file": {**SIG, "sha256": "0" * 64},
              "tampered": SIG}[case]
    if case == "tampered":
        data = SOURCE + b"\n# changed after signing\n"
    serve(script, monkeypatch, data, signed)
    with pytest.raises(SystemExit, match="not signed"):
        script.signed_client("https://hub.example")


def test_the_hub_serves_it_with_its_own_address(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token="x"))
    body = c.get("/update-helpers.py").text
    assert 'DEFAULT_HUB = "http://testserver"' in body and "__RELAY_HUB__" not in body


def test_it_says_plainly_when_nothing_is_installed(script, capsys):
    assert script.installed_client() is None
    assert script.main(["--check"]) == 0
    out = capsys.readouterr().out
    assert "No Relay client is installed" in out and "No Relay helper is installed" in out
    assert "before releases were numbered" not in out
