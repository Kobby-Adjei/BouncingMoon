"""Backups with no paid plan: a nightly copy on the volume, and one the owner downloads and keeps."""
import io
import os
import zipfile

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
