"""Email sign-in for people (Supabase magic links, relayed by the hub)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from relay.hub import create_app

ADMIN = "admin-test-token"


class FakeSupabase:
    """Stands in for Supabase Auth: records sent links, and knows which tokens are good."""
    def __init__(self):
        self.sent, self.tokens, self.codes, self.verified = [], {}, {}, []
        self.refuse = None          # (status, body) to answer /otp with instead of 200
    def __call__(self, method, url, headers, body):
        assert headers["apikey"] == "sb_publishable_test"
        if url.startswith("https://sb.example/auth/v1/otp"):
            if self.refuse:
                return self.refuse
            self.sent.append({"url": url, "body": json.loads(body)})
            return 200, b"{}"
        if url == "https://sb.example/auth/v1/verify":
            b = json.loads(body)
            self.verified.append(b)
            assert b["type"] == "email"
            key = b.get("token_hash") or (b.get("email"), b.get("token"))
            u = self.codes.pop(key, None)          # one use, like the real thing
            if not u:
                return 403, b'{"code":403,"error_code":"otp_expired","msg":"Token has expired or is invalid"}'
            return 200, json.dumps({"access_token": "jwt-x", "user": u}).encode()
        if url == "https://sb.example/auth/v1/user":
            tok = headers["Authorization"].removeprefix("Bearer ")
            u = self.tokens.get(tok)
            return (200, json.dumps(u).encode()) if u else (401, b'{"msg":"bad jwt"}')
        raise AssertionError(url)


def H(tok):
    return {"Authorization": f"Bearer {tok}"}


@pytest.fixture
def setup(tmp_path):
    sb = FakeSupabase()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, supabase_url="https://sb.example",
                     supabase_key="sb_publishable_test", supabase_send=sb)
    c = TestClient(app, base_url="https://hub.example")
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    return c, sb, owner


def test_config_says_whether_email_sign_in_is_on(setup, tmp_path):
    c, _, _ = setup
    assert c.get("/auth/config").json() == {"email": True, "claimed": True}
    off = TestClient(create_app(data_dir=str(tmp_path / "x"), admin_token=ADMIN))
    # no owner yet: the sign-in page may ask for the admin key; once claimed it says invite-only
    assert off.get("/auth/config").json() == {"email": False, "claimed": False}
    assert off.post("/auth/email", json={"email": "a@b.co"}).status_code == 404


def test_links_go_only_to_emails_on_the_hub_and_the_answer_never_tells(setup):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "Kobby@Example.com"})
    assert c.post("/auth/email", json={"email": "stranger@example.com"}).json() == {"sent": True}
    assert sb.sent == []                                   # nothing sent to strangers...
    assert c.post("/auth/email", json={"email": "kobby@example.com"}).json() == {"sent": True}
    assert len(sb.sent) == 1                               # ...same answer, but only now a link
    assert sb.sent[0]["body"]["email"] == "kobby@example.com"
    assert "redirect_to=https%3A%2F%2Fhub.example%2Fapp" in sb.sent[0]["url"]
    assert c.post("/auth/email", json={"email": "not-an-email"}).status_code == 400


def test_a_good_link_becomes_a_session_that_works_and_can_end(setup):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    sb.tokens["jwt-good"] = {"email": "kobby@example.com", "email_confirmed_at": "2026-11-18T00:00:00Z"}
    sb.tokens["jwt-unconfirmed"] = {"email": "kobby@example.com"}
    sb.tokens["jwt-stranger"] = {"email": "x@example.com", "email_confirmed_at": "2026-11-18T00:00:00Z"}
    assert c.post("/auth/session", json={"access_token": "jwt-bad"}).status_code == 401
    assert c.post("/auth/session", json={"access_token": "jwt-unconfirmed"}).status_code == 401
    r = c.post("/auth/session", json={"access_token": "jwt-stranger"})
    assert r.status_code == 403 and "isn't on this hub" in r.json()["detail"]
    s = c.post("/auth/session", json={"access_token": "jwt-good"}).json()
    assert s["handle"] == "kobby" and s["owner"] is True and s["token"].startswith("rly_sess_")
    me = c.get("/agents/me", headers=H(s["token"])).json()
    assert me["handle"] == "kobby" and me["owner"] is True
    # the session can run the hub like the owner's key
    assert c.post("/people", headers=H(s["token"]), json={"handle": "ama", "email": "ama@example.com"}).status_code == 200
    c.post("/auth/signout", headers=H(s["token"]))
    assert c.get("/agents/me", headers=H(s["token"])).status_code == 401


def test_people_and_emails_are_owner_business(setup):
    c, sb, owner = setup
    agent = c.post("/agents", headers=H(owner), json={"handle": "codex"}).json()["token"]
    assert c.post("/people", headers=H(agent), json={"handle": "ama", "email": "ama@example.com"}).status_code == 403
    assert c.post("/people", headers=H(owner), json={"handle": "ama", "email": "ama@example.com"}).json() == \
        {"handle": "ama", "email": "ama@example.com"}
    assert c.post("/people", headers=H(owner), json={"handle": "ama2", "email": "AMA@example.com"}).status_code == 409
    assert c.put("/people/codex/email", headers=H(owner), json={"email": "c@example.com"}).status_code == 409
    # emails are visible to the owner only
    seen_by_owner = {a["handle"]: a["email"] for a in c.get("/agents", headers=H(owner)).json()}
    seen_by_agent = {a["handle"]: a["email"] for a in c.get("/agents", headers=H(agent)).json()}
    assert seen_by_owner["ama"] == "ama@example.com" and seen_by_agent["ama"] is None
    # removing the person removes their email, so the link can't be sent any more
    c.delete("/agents/ama", headers=H(owner))
    c.post("/auth/email", json={"email": "ama@example.com"})
    assert sb.sent == []


CONFIRMED = "2026-11-18T00:00:00Z"


@pytest.mark.parametrize("refusal, status, words", [
    ((429, b'{"code":429,"error_code":"over_email_send_rate_limit","msg":"email rate limit exceeded"}'), 429, "Too many emails"),
    ((429, b'{"code":429,"error_code":"over_request_rate_limit","msg":"For security purposes, you can only request this after 41 seconds."}'), 429, "Too many emails"),
    ((429, b'{"code":429,"msg":"email rate limit exceeded"}'), 429, "Too many emails"),
    ((400, b'{"code":400,"error_code":"email_address_not_authorized","msg":"Email address \\"kobby@example.com\\" not authorized"}'), 503, "isn't set up for outside addresses"),
    ((500, b'{"code":500,"error_code":"unexpected_failure","msg":"Error sending magic link email to kobby@example.com"}'), 502, "couldn't be sent"),
    ((0, b'{"error_code":"unreachable","msg":"timed out"}'), 502, "couldn't be sent"),
])
def test_when_supabase_wont_send_the_hub_logs_why_and_says_so(setup, capsys, refusal, status, words):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    sb.refuse = refusal
    r = c.post("/auth/email", json={"email": "kobby@example.com"})
    assert r.status_code == status and words in r.json()["detail"]
    out = capsys.readouterr().out
    assert "k***@example.com" in out and f"status={refusal[0]}" in out
    assert "kobby@example.com" not in out                  # never the whole address, even when Supabase echoes it
    # strangers still get the same quiet answer, and Supabase isn't asked
    assert c.post("/auth/email", json={"email": "stranger@example.com"}).json() == {"sent": True}
    assert "stranger" not in capsys.readouterr().out


def test_a_code_from_the_email_signs_in_and_works_once(setup):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    sb.codes[("kobby@example.com", "123456")] = {"email": "kobby@example.com", "email_confirmed_at": CONFIRMED}
    assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": "12"}).status_code == 400
    r = c.post("/auth/verify", json={"email": "Kobby@Example.com", "code": " 123 456 "}).json()
    assert r["handle"] == "kobby" and r["owner"] is True and r["token"].startswith("rly_sess_")
    assert c.get("/agents/me", headers=H(r["token"])).json()["handle"] == "kobby"
    again = c.post("/auth/verify", json={"email": "kobby@example.com", "code": "123456"})
    assert again.status_code == 401 and "didn't work" in again.json()["detail"]


def test_codes_for_emails_not_on_the_hub_look_wrong_and_never_reach_supabase(setup):
    c, sb, _ = setup
    r = c.post("/auth/verify", json={"email": "stranger@example.com", "code": "123456"})
    assert r.status_code == 401 and "didn't work" in r.json()["detail"]
    assert sb.verified == []


def test_guessing_codes_is_cut_off(setup):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    for _ in range(5):
        assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": "000000"}).status_code == 401
    sb.codes[("kobby@example.com", "123456")] = {"email": "kobby@example.com", "email_confirmed_at": CONFIRMED}
    r = c.post("/auth/verify", json={"email": "kobby@example.com", "code": "123456"})
    assert r.status_code == 429 and len(sb.verified) == 5


def test_a_token_hash_link_signs_in_only_for_people_on_the_hub(setup):
    c, sb, owner = setup
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    sb.codes["hash-good"] = {"email": "kobby@example.com", "email_confirmed_at": CONFIRMED}
    sb.codes["hash-stranger"] = {"email": "x@example.com", "email_confirmed_at": CONFIRMED}
    assert c.post("/auth/verify", json={"token_hash": "hash-good"}).json()["handle"] == "kobby"
    used = c.post("/auth/verify", json={"token_hash": "hash-good"})
    assert used.status_code == 401 and "already used" in used.json()["detail"]
    assert c.post("/auth/verify", json={"token_hash": "hash-stranger"}).status_code == 403


def test_email_sessions_from_before_the_scanner_fix_are_ended_once(tmp_path):
    from relay.hub import create_app as mk
    sb = FakeSupabase()
    kw = dict(data_dir=str(tmp_path), admin_token=ADMIN, supabase_url="https://sb.example",
              supabase_key="sb_publishable_test", supabase_send=sb)
    c = TestClient(mk(**kw), base_url="https://hub.example")
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    store = c.app.state.store
    with store.tx() as db:                      # a session left over from before the fix
        db.execute("DELETE FROM hub_flags")
        db.execute("INSERT INTO sessions(token_hash,handle,created,expires,last_used) VALUES ('h','kobby',1,9e12,1)")
    c2 = TestClient(mk(**kw), base_url="https://hub.example")      # the next start ends it, once
    with store.ro() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        db_flag = db.execute("SELECT COUNT(*) FROM hub_flags").fetchone()[0]
    assert db_flag == 1
    with store.tx() as db:                      # sessions made after the fix survive later restarts
        db.execute("INSERT INTO sessions(token_hash,handle,created,expires,last_used) VALUES ('h2','kobby',2,9e12,2)")
    mk(**kw)
    with store.ro() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 1
    assert c2.get("/agents/me", headers=H(owner)).json()["handle"] == "kobby"   # keys untouched


# ---------------------------------------------------------------- the hub's own codes, sent by Resend
class FakeResend:
    def __init__(self):
        self.sent, self.reply = [], (200, b'{"id":"e_1"}')

    def __call__(self, method, url, headers, body):
        self.sent.append({"url": url, "auth": headers.get("Authorization"), **json.loads(body)})
        return self.reply

    def code(self):
        return re.search(r"code is (\d{3}) (\d{3})", self.sent[-1]["text"]).expand(r"\1\2")


@pytest.fixture
def rs(tmp_path):
    fake = FakeResend()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, resend_key="re_test", email_from="Relay <in@relay.test>",
                     resend_send=fake)
    c = TestClient(app, base_url="https://hub.example")
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    c.put("/people/kobby/email", headers=H(owner), json={"email": "Kobby@Example.com"})
    return c, fake


def test_resend_emails_a_code_with_nothing_to_click_and_it_signs_in_once(rs):
    c, fake = rs
    assert c.get("/auth/config").json()["email"] is True
    assert c.post("/auth/email", json={"email": "kobby@example.com"}).json() == {"sent": True}
    m = fake.sent[-1]
    assert m["url"] == "https://api.resend.com/emails" and m["auth"] == "Bearer re_test"
    assert m["to"] == ["kobby@example.com"] and m["from"] == "Relay <in@relay.test>"
    assert "http" not in m["text"] and "href" not in m["html"]          # a mail scanner has nothing to open
    code = fake.code()
    assert code in m["subject"].replace(" ", "")
    r = c.post("/auth/verify", json={"email": "KOBBY@example.com", "code": code[:3] + " " + code[3:]})
    assert r.status_code == 200 and r.json()["handle"] == "kobby" and r.json()["token"].startswith("rly_sess_")
    # a code works once
    assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": code}).status_code == 401


def test_resend_codes_are_rate_limited_and_die_after_five_wrong_tries(rs):
    c, fake = rs
    c.post("/auth/email", json={"email": "kobby@example.com"})
    # one email a minute per address
    assert c.post("/auth/email", json={"email": "kobby@example.com"}).status_code == 429
    good = fake.code()
    wrong = "000000" if good != "000000" else "111111"
    for _ in range(5):
        assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": wrong}).status_code == 401
    assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": good}).status_code == 429


def test_resend_says_nothing_about_who_is_on_the_hub(rs):
    c, fake = rs
    assert c.post("/auth/email", json={"email": "stranger@example.com"}).json() == {"sent": True}
    assert fake.sent == []
    assert c.post("/auth/verify", json={"email": "stranger@example.com", "code": "123456"}).status_code == 401


def test_resend_refusals_say_what_to_do(rs):
    c, fake = rs
    fake.reply = (403, b'{"name":"validation_error","message":"You can only send testing emails to your own email address"}')
    r = c.post("/auth/email", json={"email": "kobby@example.com"})
    assert r.status_code == 503 and "set up email sending" in r.json()["detail"]
    # nothing went out, so there's no minute to wait before trying again
    fake.reply = (200, b'{"id":"e_2"}')
    assert c.post("/auth/email", json={"email": "kobby@example.com"}).status_code == 200


def test_when_resend_refuses_supabase_sends_the_code_instead(tmp_path):
    """Before a sending domain is verified, Resend only mails its own account's address. The hub doesn't
    fail then: Supabase sends that email, and its code works as it always did."""
    sb, rs = FakeSupabase(), FakeResend()
    rs.reply = (403, b'{"message":"You can only send testing emails to your own email address"}')
    c = TestClient(create_app(data_dir=str(tmp_path), admin_token=ADMIN, supabase_url="https://sb.example",
                              supabase_key="sb_publishable_test", supabase_send=sb, resend_key="re_test",
                              resend_send=rs), base_url="https://hub.example")
    owner = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    c.put("/people/kobby/email", headers=H(owner), json={"email": "kobby@example.com"})
    assert c.post("/auth/email", json={"email": "kobby@example.com"}).json() == {"sent": True}
    assert len(rs.sent) == 1 and len(sb.sent) == 1
    sb.codes[("kobby@example.com", "424242")] = {"email": "kobby@example.com", "email_confirmed_at": "now"}
    r = c.post("/auth/verify", json={"email": "kobby@example.com", "code": "424242"})
    assert r.status_code == 200 and r.json()["handle"] == "kobby"
    # and once Resend works, its own codes are used
    rs.reply = (200, b'{"id":"e_3"}')
    c.post("/auth/email", json={"email": "kobby@example.com"})
    assert c.post("/auth/verify", json={"email": "kobby@example.com", "code": rs.code()}).status_code == 200
