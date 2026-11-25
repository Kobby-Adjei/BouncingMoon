"""Backups with no paid plan: a nightly copy on the volume, and one the owner downloads and keeps."""
import io
import os
import zipfile

import pytest

from fastapi.testclient import TestClient

from relay import backup as B
from relay.hub import create_app

ADMIN = "admin-test-token"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


def hub(path):
    app = create_app(data_dir=str(path), admin_token=ADMIN, caption_threads=False, media_threads=False)
    return TestClient(app), app


def test_the_owners_download_restores_everything_on_a_new_server(tmp_path):
    c, app = hub(tmp_path / "old")
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(owner), json={"handle": "claude"}).json()["token"]
    c.post("/messages", headers=H(claude), json={"channel": "direct", "text": "remember this"})
    c.post("/keys", headers=H(owner), json={"name": "instagram", "hosts": ["graph.facebook.com"], "secret": "s3cret"})

    r = c.get("/backup", headers=H(owner))
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert set(z.namelist()) == {"relay.db", "hub_secret", "README.txt"}

    new = tmp_path / "new"                           # the volume is gone: a new server, the backup unzipped into it
    new.mkdir()
    z.extractall(new)
    c2, app2 = hub(new)
    assert "remember this" in [m["text"] for m in c2.get("/messages", headers=H(claude), params={"latest": True}).json()]
    assert [k["name"] for k in c2.get("/keys", headers=H(owner)).json()] == ["instagram"]
    row = app2.state.vault.usable("claude")[0]       # the saved key still unseals
    assert app2.state.vault._open(row) == "s3cret"


def test_only_the_owner_downloads_a_backup(tmp_path):
    c, app = hub(tmp_path)
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(owner), json={"handle": "claude"}).json()["token"]
    assert c.get("/backup").status_code == 401
    assert c.get("/backup", headers=H(claude)).status_code == 403
    assert c.get("/backup", headers=H(ADMIN)).status_code == 200


def test_one_copy_a_day_and_only_the_last_seven_are_kept(tmp_path):
    c, app = hub(tmp_path)
    day = 86400
    made = [B.nightly(str(tmp_path), now=lambda d=d: 1_700_000_000 + d * day) for d in range(10)]
    assert all(made)
    assert B.nightly(str(tmp_path), now=lambda: 1_700_000_000 + 9 * day) is None      # today's is there already
    assert len(os.listdir(tmp_path / "backups")) == 7
    assert B.nightly(str(tmp_path), room=lambda n: False, now=lambda: 1_700_000_000 + 20 * day) is None   # disk full: skip


# ------------------------------------------------------------------ off-site copies (R2, B2, any S3)
import datetime as _dt
import subprocess as _sp
import sys as _sys
import urllib.error as _ue

from relay import offsite as O


def test_requests_are_signed_exactly_as_aws_documents():
    """The two worked examples in AWS's Signature Version 4 documentation for S3."""
    now = _dt.datetime(2013, 5, 24, tzinfo=_dt.timezone.utc)
    kid, sec = "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    get = O.sign("GET", "https://examplebucket.s3.amazonaws.com/test.txt", kid, sec, "us-east-1",
                 headers={"Range": "bytes=0-9"}, now=now)
    assert get["Authorization"].endswith("Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
    lst = O.sign("GET", "https://examplebucket.s3.amazonaws.com/?max-keys=2&prefix=J", kid, sec, "us-east-1", now=now)
    assert lst["Authorization"].endswith("Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")


def test_a_copy_opens_only_with_the_admin_token():
    blob = O.seal(b"the whole hub", "token-1")
    assert b"the whole hub" not in blob and O.unseal(blob, "token-1") == b"the whole hub"
    with pytest.raises(Exception):
        O.unseal(blob, "another-token")


class Bucket:
    def __init__(self):
        self.files, self.fail = {}, None

    def put(self, name, data):
        if self.fail:
            raise _ue.HTTPError("https://x", self.fail, "no", {}, None)
        self.files[name] = data

    def names(self, prefix="relay-"):
        return sorted(n for n in self.files if n.startswith(prefix))

    def delete(self, name):
        self.files.pop(name, None)


def _nights(data_dir, days):
    folder = os.path.join(data_dir, "backups")
    os.makedirs(folder, exist_ok=True)
    for d in days:
        with open(os.path.join(folder, f"relay-2026-11-{d:02d}.zip"), "wb") as f:
            f.write(f"night {d}".encode())


def test_the_newest_night_goes_off_site_once_and_only_seven_are_kept(tmp_path):
    b = Bucket()
    for d in range(1, 10):
        _nights(str(tmp_path), [d])
        assert O.sync(str(tmp_path), b, "tok", now=lambda: 1000.0 + d) == f"relay-2026-11-{d:02d}.zip.enc"
    assert O.sync(str(tmp_path), b, "tok") is None                       # already there: nothing to do
    assert b.names() == [f"relay-2026-11-{d:02d}.zip.enc" for d in range(3, 10)]
    assert O.unseal(b.files["relay-2026-11-09.zip.enc"], "tok") == b"night 9"
    assert O.status(str(tmp_path))["name"] == "relay-2026-11-09.zip.enc" and O.status(str(tmp_path))["error"] is None


def test_a_failed_copy_is_written_down_for_the_owner_and_tried_again(tmp_path):
    b = Bucket(); b.fail = 403
    _nights(str(tmp_path), [1])
    assert O.sync(str(tmp_path), b, "tok") is None
    assert "403" in O.status(str(tmp_path))["error"]
    b.fail = None
    assert O.sync(str(tmp_path), b, "tok") == "relay-2026-11-01.zip.enc"


def test_the_owner_sees_where_backups_are_and_nobody_else_does(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_BACKUPS", "0")
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token="adm"))
    owner = c.post("/owner", headers={"Authorization": "Bearer adm"}, json={"handle": "kobby"}).json()["token"]
    st = c.get("/backup/status", headers={"Authorization": f"Bearer {owner}"}).json()
    assert st["offsite"]["on"] is False and st["local"] is None
    agent = c.post("/agents", headers={"Authorization": f"Bearer {owner}"}, json={"handle": "claude"}).json()["token"]
    assert c.get("/backup/status", headers={"Authorization": f"Bearer {agent}"}).status_code == 403


def test_a_downloaded_copy_opens_on_the_command_line(tmp_path):
    enc = tmp_path / "relay-2026-11-25.zip.enc"
    enc.write_bytes(O.seal(b"zip bytes", "the-token"))
    out = tmp_path / "backup.zip"
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    r = _sp.run([_sys.executable, "-m", "relay.offsite", "open", str(enc), str(out)], cwd=root,
                env={**os.environ, "RELAY_ADMIN_TOKEN": "the-token"}, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() == b"zip bytes"
