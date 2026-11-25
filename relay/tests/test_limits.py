"""Hard to knock over: uploads have a cap, a full disk refuses new files and says so, app sign-ups
are rate-limited and forgotten when unused."""
import pytest
from fastapi.testclient import TestClient

from relay import media as M
from relay import oauth as O
from relay.hub import create_app

ADMIN = "admin-test-token"
CB = "https://claude.ai/api/mcp/auth_callback"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def hub(tmp_path):
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False,
                     max_upload_mb=1)
    c = TestClient(app)
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(owner), json={"handle": "claude"}).json()["token"]
    return c, app, claude


def full_disk(monkeypatch, free_share):
    monkeypatch.setattr(M, "disk", lambda path: (int(free_share * 10 * 2**30), 10 * 2**30))


def test_uploads_have_a_cap(hub):
    c, app, claude = hub
    assert c.post("/artifacts", headers=H(claude), params={"name": "a.bin"}, content=b"x" * 2**20).status_code == 200
    assert c.post("/artifacts", headers=H(claude), params={"name": "b.bin"}, content=b"x" * (2**20 + 1)).status_code == 413


def test_the_default_cap_is_500_mb(tmp_path):
    assert create_app(data_dir=str(tmp_path), admin_token=ADMIN).state.media.max_upload == 500 * 2**20


def test_a_nearly_full_disk_refuses_new_files_and_health_says_so(hub, monkeypatch):
    c, app, claude = hub
    assert c.get("/health").json()["ok"] is True
    full_disk(monkeypatch, 0.10)                     # under 15% free: no new files
    r = c.post("/artifacts", headers=H(claude), params={"name": "a.bin"}, content=b"x" * 10)
    assert r.status_code == 507 and "nearly full" in r.text
    with pytest.raises(M.MediaError, match="nearly full"):
        app.state.media.room_for(10)
    assert c.get("/health").status_code == 200       # 10% free: still healthy, just no new files
    full_disk(monkeypatch, 0.04)                     # under 5%: health says so, deploys still go out
    h = c.get("/health")
    assert h.status_code == 503 and h.json() == {"ok": False, "version": h.json()["version"], "disk": "nearly full"}
    assert c.get("/health/live").status_code == 200


def test_app_sign_ups_are_rate_limited_per_address(hub, monkeypatch):
    c, app, claude = hub
    monkeypatch.setattr(O, "REGISTER_PER_HOUR", 3)
    reg = lambda ip: c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [CB]},
                            headers={"X-Forwarded-For": f"1.2.3.4, {ip}"}).status_code
    assert [reg("203.0.113.1") for _ in range(4)] == [201, 201, 201, 429]
    assert reg("203.0.113.2") == 201                 # someone else is unaffected


def test_apps_that_never_finished_signing_in_are_forgotten_after_a_day(hub):
    c, app, claude = hub
    for _ in range(3):
        c.post("/oauth/register", json={"client_name": "x", "redirect_uris": [CB]})
    with app.state.store.tx() as db:
        db.execute("UPDATE oauth_clients SET created=created-90000")
    c.post("/oauth/register", json={"client_name": "y", "redirect_uris": [CB]})
    with app.state.store.ro() as db:
        assert [r["name"] for r in db.execute("SELECT name FROM oauth_clients")] == ["y"]


def test_messages_have_a_length_limit(hub):
    c, app, claude = hub
    ok = c.post("/messages", headers=H(claude), json={"channel": "direct", "text": "x" * 20000})
    assert ok.status_code == 200
    assert c.post("/messages", headers=H(claude), json={"channel": "direct", "text": "x" * 20001}).status_code == 422
    assert c.post("/messages", headers=H(claude), json={"channel": "c" * 65, "text": "hi"}).status_code == 422


def test_housekeeping_clears_leftovers_but_never_shared_files(hub):
    import os
    import time as _t
    c, app, claude = hub
    m = app.state.media
    os.makedirs(m.tmp, exist_ok=True)
    old_work = os.path.join(m.tmp, "work_m_dead"); os.makedirs(old_work)
    old_part = os.path.join(m.tmp, "f_0123456789abcdef.part"); open(old_part, "wb").write(b"half")
    new_part = os.path.join(m.tmp, "f_fedcba9876543210.part"); open(new_part, "wb").write(b"busy")
    week_ago = _t.time() - 7 * 86400
    for p in (old_work, old_part):
        os.utime(p, (week_ago, week_ago))
    up = c.post("/artifacts", headers=H(claude), params={"name": "kept.bin"}, content=b"shared").json()
    assert m.tidy() == 2
    assert not os.path.exists(old_work) and not os.path.exists(old_part) and os.path.exists(new_part)
    assert os.path.exists(app.state.store.blob_path(up["id"]))
