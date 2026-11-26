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


# ------------------------------------------------------------------ search
def test_search_finds_what_was_said_who_was_there_and_marks_the_words(hub):
    c, _, _, t = hub
    a = say(c, t["kobby"], "@claude cut the launch video to 30 seconds", to=["claude"])
    say(c, t["claude"], "Done: the launch cut is ready", reply_to=a["id"])
    b = say(c, t["kobby"], "@codex check the invoices", to=["codex"])
    got = c.get("/search?q=launch", headers=H(t["kobby"])).json()
    assert [x["thread"] for x in got] == [a["thread"]]
    hit = got[0]["hits"][0]
    assert "\x02launch\x03" in hit["text"].lower() and hit["from"] == "claude"     # newest match first, word marked
    assert got[0]["first"]["text"].startswith("@claude cut") and "said" in got[0]["why"]
    assert [x["thread"] for x in c.get("/search?q=laun", headers=H(t["kobby"])).json()] == [a["thread"]]   # as you type
    assert c.get("/search?q=launch invoices", headers=H(t["kobby"])).json() == []                         # every word
    assert [x["thread"] for x in c.get("/search?q=codex", headers=H(t["kobby"])).json()] == [b["thread"]]  # who's in it
    assert c.get("/search?q=a", headers=H(t["kobby"])).json() == []                                       # too short


def test_search_never_shows_other_peoples_conversations(hub):
    c, _, _, t = hub
    say(c, t["kobby"], "@claude the secret launch plan", to=["claude"])
    assert c.get("/search?q=secret", headers=H(t["ama"])).json() == []


def test_search_reads_what_was_there_before_and_keeps_up_with_edits(hub):
    c, app, path, t = hub
    m = say(c, t["kobby"], "@claude old notes about pricing", to=["claude"])
    db = sqlite3.connect(str(path / "relay.db"))
    db.execute("DELETE FROM meta WHERE key='fts_built'"); db.execute("DROP TABLE msg_fts"); db.commit(); db.close()
    c2 = TestClient(create_app(data_dir=str(path), admin_token=ADMIN, caption_threads=False, media_threads=False))
    assert [x["thread"] for x in c2.get("/search?q=pricing", headers=H(t["kobby"])).json()] == [m["thread"]]
    with app.state.store.tx() as d:                                 # "(deleted)" replaces a deleted account's words
        d.execute("UPDATE messages SET text='(deleted)' WHERE id=?", (m["id"],))
    assert c2.get("/search?q=pricing", headers=H(t["kobby"])).json() == []


# ------------------------------------------------------------------ shortcuts
def test_a_saved_hand_off_runs_again_in_one_tap(hub):
    c, _, _, t = hub
    sc = c.post("/shortcuts", headers=H(t["kobby"]), json={"title": "Post the cut", "text": "@claude cut it and send it to @codex",
                                                              "mentions": ["claude", "codex"]}).json()
    assert sc["title"] == "Post the cut" and sc["mentions"] == ["claude", "codex"]
    assert c.post("/shortcuts", headers=H(t["claude"]), json={"text": "x"}).status_code == 403          # people only
    m = c.post(f"/shortcuts/{sc['id']}/run", headers=H(t["kobby"])).json()
    assert m["from"] == "kobby" and m["thread"] == m["id"] and set(m["mentions"]) == {"claude", "codex"}
    assert c.get("/shortcuts", headers=H(t["kobby"])).json()[0]["uses"] == 1
    assert c.post(f"/shortcuts/{sc['id']}/run", headers=H(t["ama"])).status_code == 404               # yours only
    assert c.delete(f"/shortcuts/{sc['id']}", headers=H(t["kobby"])).json() == {"removed": sc["id"]}
    assert c.get("/shortcuts", headers=H(t["kobby"])).json() == []


def test_an_agents_card_lists_your_conversations_with_it(hub):
    c, _, _, t = hub
    a = say(c, t["kobby"], "@claude first job", to=["claude"])
    say(c, t["kobby"], "@codex other job", to=["codex"])
    b = say(c, t["kobby"], "@claude second job", to=["claude"])
    got = [x["thread"] for x in c.get("/threads?with=claude", headers=H(t["kobby"])).json()]
    assert got == [b["thread"], a["thread"]]                                     # newest first, only claude's
    assert c.get("/threads?with=claude", headers=H(t["ama"])).json() == []        # still only what you may see


def test_conversations_and_search_narrow_by_date(hub):
    c, app, _, t = hub
    old = say(c, t["kobby"], "@claude the old launch plan", to=["claude"])
    new = say(c, t["kobby"], "@claude the new launch plan", to=["claude"])
    with app.state.store.tx() as d:                               # the old one was last active ten days ago
        d.execute("UPDATE messages SET time=time-864000 WHERE thread=?", (old["thread"],))
    import time as _t
    day = _t.time() - 86400
    recent = [x["thread"] for x in c.get(f"/threads?since={day}", headers=H(t["kobby"])).json()]
    assert new["thread"] in recent and old["thread"] not in recent
    before = [x["thread"] for x in c.get(f"/threads?until={day}", headers=H(t["kobby"])).json()]
    assert before == [old["thread"]]
    assert [x["thread"] for x in c.get(f"/search?q=launch&since={day}", headers=H(t["kobby"])).json()] == [new["thread"]]
    assert [x["thread"] for x in c.get(f"/search?q=launch&until={day}", headers=H(t["kobby"])).json()] == [old["thread"]]


def test_a_pinned_conversation_stays_on_top_and_a_muted_one_stops_nagging(hub):
    c, app, _, t = hub
    old = say(c, t["kobby"], "@codex the old one", to=["codex"])
    for i in range(5):
        say(c, t["kobby"], f"@claude job {i}", to=["claude"])
    r = c.put(f"/threads/{old['thread']}/mark", headers=H(t["kobby"]), json={"pinned": True})
    assert r.json() == {"thread": old["thread"], "pinned": True, "muted": False}
    first = talk(c.get("/threads?limit=3", headers=H(t["kobby"])).json())
    assert first[0]["thread"] == old["thread"] and first[0]["pinned"] and len(first) == 4      # pinned, then a page
    nxt = c.get(f"/threads?limit=3&before={first[-1]['last']['id']}", headers=H(t["kobby"])).json()
    assert old["thread"] not in [x["thread"] for x in nxt]                                     # never twice
    assert not any(x["pinned"] for x in c.get("/threads", headers=H(t["ama"])).json())        # only yours
    # muted: a new answer there isn't counted as something new for the daily email, and doesn't buzz
    say(c, t["codex"], "done", reply_to=old["id"])
    with app.state.store.ro() as db:
        before = db.execute("SELECT COUNT(*) FROM marks").fetchone()[0]
    c.put(f"/threads/{old['thread']}/mark", headers=H(t["kobby"]), json={"muted": True})
    row = next(x for x in c.get("/threads", headers=H(t["kobby"])).json() if x["thread"] == old["thread"])
    assert row["muted"] and row["pinned"]
    # unpin and unmute: nothing left stored
    c.put(f"/threads/{old['thread']}/mark", headers=H(t["kobby"]), json={"pinned": False, "muted": False})
    with app.state.store.ro() as db:
        assert before == 1 and db.execute("SELECT COUNT(*) FROM marks").fetchone()[0] == 0


def test_only_people_mark_only_conversations_they_can_see_and_pins_are_capped(hub):
    c, _, _, t = hub
    mine = say(c, t["kobby"], "@claude private", to=["claude"])
    assert c.put(f"/threads/{mine['thread']}/mark", headers=H(t["ama"]), json={"pinned": True}).status_code == 404
    assert c.put(f"/threads/{mine['thread']}/mark", headers=H(t["claude"]), json={"pinned": True}).status_code == 403
    assert c.put("/threads/99999/mark", headers=H(t["kobby"]), json={"pinned": True}).status_code == 404
    for i in range(10):
        th = say(c, t["kobby"], f"@claude {i}", to=["claude"])["thread"]
        assert c.put(f"/threads/{th}/mark", headers=H(t["kobby"]), json={"pinned": True}).status_code == 200
    r = c.put(f"/threads/{mine['thread']}/mark", headers=H(t["kobby"]), json={"pinned": True})
    assert r.status_code == 429 and "Unpin one" in r.json()["detail"]
