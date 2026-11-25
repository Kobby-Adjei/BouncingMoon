"""As a hub fills up: conversations come a page at a time, long ones read back page by page, and who
may see what is answered from a small index instead of every message ever sent."""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "scale-admin"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def hub(tmp_path):
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False)
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    codex = c.post("/agents", headers=H(kobby), json={"handle": "codex"}).json()["token"]
    inv = c.post("/invites", headers=H(kobby), json={"handle": "ama", "role": "human"}).json()
    ama = c.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    return c, app, tmp_path, {"kobby": kobby, "claude": claude, "codex": codex, "ama": ama}


def talk(convs):
    """Conversations, leaving out the hub's own "joined the hub" notices."""
    return [x for x in convs if x["last"]["from"] != "relay"]


def say(c, tok, text, to=(), reply_to=None):
    body = {"text": text, "mentions": list(to)}
    body.update({"reply_to": reply_to} if reply_to else {"channel": "direct"})
    r = c.post("/messages", headers=H(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_one_busy_conversation_no_longer_pushes_the_others_off_the_list(hub):
    c, _, _, t = hub
    quiet = say(c, t["kobby"], "@codex the old one", to=["codex"])
    busy = say(c, t["kobby"], "@claude let's go", to=["claude"])
    for i in range(250):                           # far more than the 200 the list used to be built from
        say(c, t["claude"], f"step {i}", reply_to=busy["id"])
    convs = talk(c.get("/threads?limit=40", headers=H(t["kobby"])).json())
    assert [x["thread"] for x in convs] == [busy["thread"], quiet["thread"]]
    top = convs[0]
    assert top["count"] == 251 and top["last"]["text"] == "step 249" and set(top["people"]) == {"kobby", "claude"}


def test_conversations_come_a_page_at_a_time_newest_first(hub):
    c, _, _, t = hub
    ids = [say(c, t["kobby"], f"@claude job {i}", to=["claude"])["thread"] for i in range(7)]
    first = c.get("/threads?limit=3", headers=H(t["kobby"])).json()
    assert [x["thread"] for x in first] == ids[::-1][:3]
    nxt = c.get(f"/threads?limit=3&before={first[-1]['last']['id']}", headers=H(t["kobby"])).json()
    assert [x["thread"] for x in nxt] == ids[::-1][3:6]


def test_a_long_conversation_reads_back_a_page_at_a_time(hub):
    c, _, _, t = hub
    root = say(c, t["kobby"], "@claude start", to=["claude"])
    for i in range(130):
        say(c, t["claude"], f"line {i}", reply_to=root["id"])
    newest = c.get(f"/messages?thread={root['thread']}&latest=true&limit=60", headers=H(t["kobby"])).json()
    assert newest[-1]["text"] == "line 129" and len(newest) == 60               # it opens on the newest, not the oldest
    back = c.get(f"/messages?thread={root['thread']}&latest=true&limit=60&before={newest[0]['id']}",
                 headers=H(t["kobby"])).json()
    assert back[-1]["id"] < newest[0]["id"] and back[-1]["text"] == "line 69" and len(back) == 60
    rest = c.get(f"/messages?thread={root['thread']}&latest=true&limit=60&before={back[0]['id']}",
                 headers=H(t["kobby"])).json()
    assert rest[0]["text"] == "@claude start" and len(rest) == 11


def test_the_list_stays_private_between_people(hub):
    c, _, _, t = hub
    mine = say(c, t["kobby"], "@claude private plans", to=["claude"])
    say(c, t["ama"], "hello from ama")
    assert mine["thread"] not in [x["thread"] for x in c.get("/threads", headers=H(t["ama"])).json()]
    # named in it: now it's hers to see too
    say(c, t["kobby"], "@ama what do you think?", to=["ama"], reply_to=mine["id"])
    assert mine["thread"] in [x["thread"] for x in c.get("/threads", headers=H(t["ama"])).json()]


def test_a_hub_from_before_the_index_builds_it_once_on_start(hub):
    c, _, path, t = hub
    a = say(c, t["kobby"], "@claude one", to=["claude"])
    say(c, t["claude"], "two", reply_to=a["id"])
    db = sqlite3.connect(str(path / "relay.db"))
    db.execute("DELETE FROM convs"); db.execute("DELETE FROM members"); db.execute("DELETE FROM meta WHERE key='convs_built'")
    db.commit(); db.close()
    c2 = TestClient(create_app(data_dir=str(path), admin_token=ADMIN, caption_threads=False, media_threads=False))
    convs = talk(c2.get("/threads", headers=H(t["kobby"])).json())
    assert len(convs) == 1 and convs[0]["count"] == 2 and set(convs[0]["people"]) == {"kobby", "claude"}


# ------------------------------------------------------------------ closing the loop
def mcp(c, tok, name, args):
    r = c.post("/mcp", headers={**H(tok), "Accept": "application/json, text/event-stream", "X-Relay-Session": "chat-1"},
               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": args}})
    return r.json()


def test_you_see_when_an_agent_read_your_message(hub):
    c, _, _, t = hub
    m = say(c, t["kobby"], "@claude can you hear me?", to=["claude"])
    first = c.get(f"/messages?thread={m['thread']}", headers=H(t["kobby"])).json()
    assert first[0]["read"] == {}                                   # sent, not read yet
    mcp(c, t["claude"], "check_replies", {})                       # claude's chat picks it up
    after = c.get(f"/messages?thread={m['thread']}", headers=H(t["kobby"])).json()
    assert list(after[0]["read"]) == ["claude"] and after[0]["read"]["claude"] > 0


def test_a_listener_waiting_is_not_a_read(hub):
    c, _, _, t = hub
    m = say(c, t["kobby"], "@codex ping", to=["codex"])
    c.get("/inbox?wait=0.1", headers=H(t["codex"]))                 # the helper keeping watch
    assert c.get(f"/messages?thread={m['thread']}", headers=H(t["kobby"])).json()[0]["read"] == {}
    c.get("/inbox", headers={**H(t["codex"]), "X-Relay-Session": "s1"})   # codex itself reading
    assert "codex" in c.get(f"/messages?thread={m['thread']}", headers=H(t["kobby"])).json()[0]["read"]


def test_new_conversations_stay_bold_until_you_open_them(hub):
    c, _, _, t = hub
    m = say(c, t["kobby"], "@claude draft the launch post", to=["claude"])
    row = lambda: next(x for x in c.get("/threads", headers=H(t["kobby"])).json() if x["thread"] == m["thread"])
    assert row()["unread"] is False                                # the newest is yours
    say(c, t["claude"], "Here's a draft", reply_to=m["id"])
    assert row()["unread"] is True                                  # an answer came
    c.get(f"/messages?thread={m['thread']}&latest=true", headers=H(t["kobby"]))   # you open it
    assert row()["unread"] is False
    assert row()["first"] == {"from": "kobby", "text": "@claude draft the launch post"}   # what it's named after


def test_what_was_there_before_the_update_isnt_all_new(hub):
    c, app, path, t = hub
    m = say(c, t["kobby"], "@claude old work", to=["claude"])
    say(c, t["claude"], "done", reply_to=m["id"])
    db = sqlite3.connect(str(path / "relay.db")); db.execute("DELETE FROM meta WHERE key='seen_floor'"); db.commit(); db.close()
    c2 = TestClient(create_app(data_dir=str(path), admin_token=ADMIN, caption_threads=False, media_threads=False))
    assert not any(x["unread"] for x in c2.get("/threads", headers=H(t["kobby"])).json())
