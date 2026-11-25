"""Privacy between people: a person and their agents see only the conversations they're in."""
import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def hub(tmp_path):
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, caption_threads=False, media_threads=False)
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    muse = c.post("/agents", headers=H(kobby), json={"handle": "muse"}).json()["token"]
    # ama: another person on the hub, with her own agent
    inv = c.post("/invites", headers=H(kobby), json={"handle": "ama", "role": "human"}).json()
    ama = c.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    ama_bot = c.post("/agents", headers=H(kobby), json={"handle": "ama-bot"}).json()["token"]
    with app.state.store.tx() as db:
        db.execute("UPDATE agents SET person='ama' WHERE handle='ama-bot'")
    return c, {"kobby": kobby, "claude": claude, "muse": muse, "ama": ama, "ama-bot": ama_bot}


def say(c, tok, text, to=(), reply_to=None, **kw):
    body = {"text": text, "mentions": list(to), **kw}
    body.update({"reply_to": reply_to} if reply_to else {"channel": "direct"})
    r = c.post("/messages", headers=H(tok), json=body)
    assert r.status_code == 200, r.text
    return r.json()


def texts(c, tok, **params):
    return [m["text"] for m in c.get("/messages", headers=H(tok), params={"latest": True, "limit": 200, **params}).json()]


def test_a_conversation_is_seen_only_by_the_people_whose_agents_are_in_it(hub):
    c, t = hub
    secret = say(c, t["claude"], "the launch password is hunter2", to=["muse"])
    say(c, t["muse"], "noted", reply_to=secret["id"])
    for who in ("kobby", "claude", "muse"):                 # kobby's circle: kobby and both his agents
        assert "the launch password is hunter2" in texts(c, t[who])
    for who in ("ama", "ama-bot"):                           # ama's circle sees nothing of it
        seen = texts(c, t[who])
        assert "the launch password is hunter2" not in seen and "noted" not in seen
        assert texts(c, t[who], thread=secret["thread"]) == []


def test_a_conversation_across_two_people_is_seen_by_both_and_only_that_one(hub):
    c, t = hub
    say(c, t["claude"], "private to kobby", to=["muse"])
    shared = say(c, t["claude"], "hi ama-bot, can you check the deck?", to=["ama-bot"])
    say(c, t["ama-bot"], "on it", reply_to=shared["id"])
    for who in ("ama", "ama-bot"):
        seen = texts(c, t[who])
        assert "hi ama-bot, can you check the deck?" in seen and "on it" in seen
        assert "private to kobby" not in seen
    assert "on it" in texts(c, t["kobby"])                   # kobby's agent is in it too


def test_nobody_can_follow_reply_into_or_subscribe_to_someone_elses_conversation(hub):
    c, t = hub
    secret = say(c, t["claude"], "kobby only", to=["muse"])
    bot = H(t["ama-bot"])
    assert c.post("/follows", headers=bot, json={"thread": secret["thread"]}).status_code == 404
    r = c.post("/messages", headers=bot, json={"text": "let me in", "reply_to": secret["id"]})
    assert r.status_code == 404 and "not found" in r.text           # it doesn't exist, as far as ama-bot knows
    assert c.post("/subscriptions", headers=bot, json={"channel": "direct"}).status_code == 400
    say(c, t["claude"], "still kobby only", reply_to=secret["id"])
    box = c.get("/inbox", headers=bot, params={"unclaimed": True}).json()
    assert box["messages"] == []
    assert "let me in" not in texts(c, t["kobby"])


def test_rooms_other_than_conversations_stay_open(hub):
    c, t = hub
    assert c.post("/subscriptions", headers=H(t["ama-bot"]), json={"channel": "news"}).json()["ok"]
    c.post("/messages", headers=H(t["claude"]), json={"channel": "news", "text": "v0.4 is out"})
    assert "v0.4 is out" in texts(c, t["ama"])
    box = c.get("/inbox", headers=H(t["ama-bot"]), params={"unclaimed": True}).json()
    assert [m["text"] for m in box["messages"]] == ["v0.4 is out"]


def test_files_go_only_to_their_makers_circle_and_the_conversations_they_were_shared_in(hub):
    c, t = hub
    up = c.post("/artifacts", headers=H(t["claude"]), params={"name": "plan.pdf"}, content=b"%PDF secret plan").json()
    other = c.post("/artifacts", headers=H(t["claude"]), params={"name": "b.pdf"}, content=b"%PDF other").json()
    assert c.get(f"/artifacts/{up['id']}", headers=H(t["ama-bot"])).status_code == 404
    assert c.get(f"/artifacts/{up['id']}/meta", headers=H(t["ama"])).status_code == 404
    assert c.get(f"/artifacts/{up['id']}", headers=H(t["muse"])).content == b"%PDF secret plan"   # same circle
    # ama-bot can't smuggle a file it can't see into its own conversation
    r = c.post("/messages", headers=H(t["ama-bot"]), json={"channel": "direct", "text": "x", "artifacts": [other["id"]]})
    assert r.status_code == 400
    say(c, t["claude"], "here's the plan", to=["ama-bot"], artifacts=[up["id"]])
    assert c.get(f"/artifacts/{up['id']}", headers=H(t["ama-bot"])).content == b"%PDF secret plan"
    assert c.get(f"/artifacts/{other['id']}", headers=H(t["ama-bot"])).status_code == 404


def test_jobs_are_seen_by_the_circle_that_asked_and_the_node_doing_them(hub):
    c, t = hub
    j = c.post("/jobs", headers=H(t["claude"]), json={"kind": "fetch", "params": {"url": "https://example.com/a.mp4"},
                                                      "requires_approval": True}).json()
    assert [x["id"] for x in c.get("/jobs", headers=H(t["muse"])).json()] == [j["id"]]
    assert c.get("/jobs", headers=H(t["ama-bot"])).json() == []
    assert c.get(f"/jobs/{j['id']}", headers=H(t["ama"])).status_code == 404
    assert c.post(f"/jobs/{j['id']}/approve", headers=H(t["ama"])).status_code == 404     # not hers to OK
    assert c.post(f"/jobs/{j['id']}/approve", headers=H(t["kobby"])).json()["status"] == "queued"


def test_the_admin_token_still_sees_everything(hub):
    c, t = hub
    say(c, t["claude"], "kobby only", to=["muse"])
    assert "kobby only" in texts(c, ADMIN)


def test_someone_elses_file_cant_be_turned_into_a_media_link(hub):
    c, t = hub
    up = c.post("/artifacts", headers=H(t["claude"]), params={"name": "cut.mp4"}, content=b"\x00secret-cut").json()
    r = c.post("/media", headers=H(t["ama-bot"]), json={"artifact": up["id"]})
    assert r.status_code in (400, 404) and "link" not in r.json()
    assert "link" in c.post("/media", headers=H(t["muse"]), json={"artifact": up["id"]}).json()   # kobby's circle may


def test_messages_carry_a_card_for_each_file_so_people_can_see_it(hub):
    c, t = hub
    up = c.post("/artifacts", headers=H(t["claude"]), params={"name": "cut.mp4"}, content=b"\x00not-really-a-video").json()
    m = c.post("/media", headers=H(t["claude"]), json={"artifact": up["id"]}).json()
    say(c, t["claude"], "here's the cut", to=["muse"], media=[m["id"]])
    got = c.get("/messages", headers=H(t["kobby"]), params={"latest": True}).json()[-1]
    assert got["text"] == "here's the cut" and len(got["media"]) == 1
    card = got["media"][0]
    assert card["id"] == m["id"] and card["name"] == "cut.mp4" and card["page"].startswith("/m/")
    assert c.get(card["page"]).status_code in (200, 202)          # the page opens for a person, no key
    assert c.get("/messages", headers=H(t["ama"]), params={"latest": True}).json() == [] or \
        all(x["text"] != "here's the cut" for x in c.get("/messages", headers=H(t["ama"])).json())
