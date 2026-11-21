"""Signed helper releases: a helper only installs a newer version signed by the owner's release key."""
import hashlib
import json
import os
import shutil

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi.testclient import TestClient

from relay import cli
from relay.cli import RelayError
from relay.hub import create_app

CLI = cli._cli_path()
SOURCE = open(CLI, "rb").read()


def as_release(n: int) -> bytes:
    return cli.re.sub(rb"^HELPER_RELEASE = \d+$", b"HELPER_RELEASE = %d" % n, SOURCE, flags=cli.re.M)


# ------------------------------------------------------------------ the signature itself
def test_ed25519_matches_rfc8032_and_an_independent_implementation():
    seed = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
    assert cli.ed25519_public(seed).hex() == "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
    assert cli.ed25519_sign(seed, b"").hex() == (
        "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b")
    for i in range(3):                       # both directions against the `cryptography` package
        seed, msg = os.urandom(32), os.urandom(40 + i)
        theirs = Ed25519PrivateKey.from_private_bytes(seed)
        pub = theirs.public_key().public_bytes_raw()
        assert cli.ed25519_public(seed) == pub
        assert cli.ed25519_verify(pub, msg, theirs.sign(msg))
        Ed25519PublicKey.from_public_bytes(pub).verify(cli.ed25519_sign(seed, msg), msg)   # raises if wrong


def test_a_changed_message_signature_or_key_does_not_verify():
    seed = os.urandom(32)
    pub, sig = cli.ed25519_public(seed), cli.ed25519_sign(seed, b"release 2")
    assert cli.ed25519_verify(pub, b"release 2", sig)
    assert not cli.ed25519_verify(pub, b"release 3", sig)
    assert not cli.ed25519_verify(pub, b"release 2", sig[:-1] + bytes([sig[-1] ^ 1]))
    assert not cli.ed25519_verify(cli.ed25519_public(os.urandom(32)), b"release 2", sig)
    assert not cli.ed25519_verify(pub, b"release 2", sig[:10])


# ------------------------------------------------------------------ keys and signing
@pytest.fixture
def key(tmp_path):
    path = str(tmp_path / "owner" / "release-key")
    pub = cli.release_keygen(path)
    return path, pub


def test_the_key_is_made_private_and_never_overwritten(key):
    path, pub = key
    assert os.stat(path).st_mode & 0o777 == 0o600 and len(pub) == 64
    with pytest.raises(RelayError, match="already exists"):
        cli.release_keygen(path)


def test_a_signed_release_checks_out_and_anything_else_does_not(key):
    path, pub = key
    data = as_release(2)
    sig = cli.release_sign(data, path)
    assert cli.check_signed(data, sig, (pub,)) == 2
    other = cli.release_keygen(path + "-other")
    with pytest.raises(RelayError, match="not from the release key"):
        cli.check_signed(data, sig, (other,))
    with pytest.raises(RelayError, match="exact file"):
        cli.check_signed(data + b"#", sig, (pub,))
    with pytest.raises(RelayError, match="release number"):
        cli.check_signed(data, {**sig, "release": 3}, (pub,))      # the number is part of what is signed
    with pytest.raises(RelayError, match="exact file"):
        cli.check_signed(data, None, (pub,))


# ------------------------------------------------------------------ the helper updating itself
class Hub:
    url = "https://hub.example.com"

    def __init__(self, data, signed):
        self.data, self.signed = data, signed

    def get(self, path, **kw):
        return {"sha256": hashlib.sha256(self.data).hexdigest(), "signed": self.signed,
                "release": cli.release_of(self.data.decode())}


@pytest.fixture
def installed(tmp_path):
    p = tmp_path / "bin" / "cli.py"
    p.parent.mkdir()
    shutil.copy2(CLI, p)
    p.write_bytes(as_release(1))
    return str(p)


def test_a_newer_release_signed_by_the_key_is_installed(installed, key):
    path, pub = key
    new = as_release(2)
    assert cli.self_update(Hub(new, cli.release_sign(new, path)), path=installed, fetch=lambda u: new, keys=(pub,))
    assert open(installed, "rb").read() == new


@pytest.mark.parametrize("case", ["unsigned", "other key", "older", "same number", "signature for another file"])
def test_nothing_unsigned_older_or_signed_by_someone_else_is_installed(installed, key, case):
    path, pub = key
    new = as_release(1 if case == "same number" else 0 if case == "older" else 2)
    new += b"\n# changed\n" if case == "same number" else b""
    if case == "unsigned":
        signed = None
    elif case == "other key":
        signed = cli.release_sign(new, cli.release_keygen(path + "-attacker") and path + "-attacker")
    elif case == "signature for another file":
        signed = cli.release_sign(as_release(2) + b"#", path)
        signed["release"] = 2
    else:
        signed = cli.release_sign(new, path)
    logs = []
    assert not cli.self_update(Hub(new, signed), logs.append, path=installed, fetch=lambda u: new, keys=(pub,))
    assert open(installed, "rb").read() == as_release(1)


def test_before_any_key_is_built_in_updates_still_work_by_sha256(installed):
    new = as_release(2)
    assert cli.self_update(Hub(new, None), path=installed, fetch=lambda u: new, keys=())


# ------------------------------------------------------------------ the hub, the commands, this repo
def test_the_hub_serves_the_release_number_and_its_signature(tmp_path):
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token="x"))
    info = c.get("/client").json()
    assert info["release"] == cli.HELPER_RELEASE
    on_disk = json.load(open(os.path.join(os.path.dirname(CLI), cli.SIG_FILE)))
    assert info["signed"] == (on_disk or None)


def test_keygen_sign_and_check_commands(tmp_path, capsys):
    k = str(tmp_path / "release-key")
    assert cli.main(["release", "keygen", "--key", k]) == 0
    pub = capsys.readouterr().out.strip().splitlines()[-1]
    f = tmp_path / "cli.py"
    f.write_bytes(cli.re.sub(rb"^RELEASE_KEYS: tuple = \(\)", b'RELEASE_KEYS: tuple = ("%s",)' % pub.encode(),
                             as_release(5), flags=cli.re.M))
    assert cli.main(["release", "check", str(f)]) == 1             # not signed yet
    assert cli.main(["release", "sign", str(f), "--key", k]) == 0
    assert json.load(open(tmp_path / "cli.sig.json"))["release"] == 5
    assert cli.main(["release", "check", str(f)]) == 0
    assert "release 5: signed" in capsys.readouterr().out


def test_this_repo_never_ships_a_release_number_below_its_signature():
    sig = json.load(open(os.path.join(os.path.dirname(CLI), cli.SIG_FILE))) or {}
    assert cli.HELPER_RELEASE >= sig.get("release", 0)
    if cli.RELEASE_KEYS and sig.get("sha256") == hashlib.sha256(SOURCE).hexdigest():
        assert cli.check_signed(SOURCE, sig) == cli.HELPER_RELEASE
