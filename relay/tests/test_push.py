"""Notifications: the hub signs (VAPID) and encrypts (RFC 8291) each one for the device it goes to, and sends
one when an agent needs an OK, answers a person, or someone asks to join. Decrypted here, independently, the
way a phone's browser would."""
import base64
import hashlib
import hmac
import json
import re
import socket
import struct

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDFExpand
from fastapi.testclient import TestClient

from relay import push as P
from relay import vault as V
from relay.hub import create_app

ADMIN = "push-admin"
EP = "https://fcm.googleapis.com/fcm/send/device-1"


def H(tok, session=None):
    return {"Authorization": f"Bearer {tok}", **({"X-Relay-Session": session} if session else {})}


def u64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def d64(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Phone:
    """A browser's side of a subscription: its key pair and auth secret, and it can read what arrives."""
    def __init__(self, endpoint=EP):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.pub = self.key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        self.auth = b"0123456789abcdef"
        self.endpoint = endpoint

    def sub(self):
        return {"endpoint": self.endpoint, "keys": {"p256dh": u64(self.pub), "auth": u64(self.auth)}}

    def read(self, body: bytes) -> dict:
        """RFC 8188 header, RFC 8291 keys: written from the RFCs, not from relay/push.py."""
        salt, rs, idlen = body[:16], struct.unpack(">I", body[16:20])[0], body[20]
        as_pub, ct = body[21:21 + idlen], body[21 + idlen:]
        assert rs == 4096 and idlen == 65
        shared = self.key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_pub))
        prk_key = hmac.new(self.auth, shared, hashlib.sha256).digest()
        ikm = HKDFExpand(hashes.SHA256(), 32, b"WebPush: info\x00" + self.pub + as_pub).derive(prk_key)
        prk = hmac.new(salt, ikm, hashlib.sha256).digest()
        cek = HKDFExpand(hashes.SHA256(), 16, b"Content-Encoding: aes128gcm\x00").derive(prk)
        nonce = HKDFExpand(hashes.SHA256(), 12, b"Content-Encoding: nonce\x00").derive(prk)
        plain = AESGCM(cek).decrypt(nonce, ct, None)
        assert plain.endswith(b"\x02")                  # the last (and only) record
        return json.loads(plain[:-1])


class PushService:
    """Stands in for Apple/Google/Mozilla: records what it was sent, answers with `status`."""
    def __init__(self):
        self.sent, self.status = [], 201

    def __call__(self, req):
        self.sent.append({"url": req.full_url, "headers": dict(req.header_items()), "body": req.data})
        status = self.status

        class R:
            def __enter__(self): return self
            def __exit__(self, *a): return False
        r = R(); r.status = status
        if status >= 400:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, status, "gone", {}, None)
        return r


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(V, "WAIT_SECONDS", 0.2)
    monkeypatch.setenv("RELAY_LEGAL_EMAIL", "hello@relay.example")
    svc = PushService()
    app = create_app(data_dir=str(tmp_path), admin_token=ADMIN, push_opener=svc, caption_threads=False, media_threads=False,
                     vault_send=lambda *a, **k: (200, {}, b"{}"),
                     vault_resolve=lambda host: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    c = TestClient(app)
    kobby = c.post("/owner", headers=H(ADMIN), json={"handle": "kobby"}).json()["token"]
    claude = c.post("/agents", headers=H(kobby), json={"handle": "claude"}).json()["token"]
    inv = c.post("/invites", headers=H(kobby), json={"handle": "ama", "role": "human"}).json()
    ama = c.post("/invites/claim", json={"code": inv["code"]}).json()["token"]
    return c, app, svc, {"kobby": kobby, "claude": claude, "ama": ama}


def on(c, tok, phone, kinds=None):
    r = c.post("/push/devices", headers=H(tok), json={**phone.sub(), **({"kinds": kinds} if kinds is not None else {})})
    assert r.status_code == 200, r.text
    return r.json()


def sent(app, svc):
    app.state.pusher.drain()
    return svc.sent


def test_what_arrives_is_readable_only_by_that_device_and_signed_by_the_hub(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    assert c.post("/push/test", headers=H(t["kobby"])).json() == {"sent": 1}
    [got] = sent(app, svc)
    assert got["url"] == EP
    h = {k.lower(): v for k, v in got["headers"].items()}
    assert h["content-encoding"] == "aes128gcm" and int(h["ttl"]) > 0
    msg = phone.read(got["body"])
    assert msg["title"] == "Relay notifications are on" and msg["url"] == "/app#activity"
    assert Phone().key and pytest.raises(Exception, Phone().read, got["body"])     # another device can't read it
    # VAPID: a JWT for the push service's origin, signed with the key /push/key publishes
    t_, k_ = [p.split("=", 1)[1] for p in h["authorization"][len("vapid "):].split(", ")]
    assert k_ == c.get("/push/key").json()["key"]
    head, claims, sig = t_.split(".")
    assert json.loads(d64(claims))["aud"] == "https://fcm.googleapis.com"
    assert json.loads(d64(claims))["sub"] == "mailto:hello@relay.example"
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), d64(k_))
    raw = d64(sig)
    pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
               f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))


def test_only_real_push_services_and_only_people(hub):
    c, app, svc, t = hub
    bad = Phone("https://169.254.169.254/latest/meta-data")         # the hub won't POST to any address
    assert c.post("/push/devices", headers=H(t["kobby"]), json=bad.sub()).status_code == 400
    assert c.post("/push/devices", headers=H(t["kobby"]), json={**Phone().sub(), "keys": {"p256dh": "x", "auth": "y"}}).status_code == 400
    assert c.post("/push/devices", headers=H(t["claude"]), json=Phone().sub()).status_code == 403
    for ok in ("https://web.push.apple.com/QGuQ", "https://updates.push.services.mozilla.com/wpush/v2/x"):
        assert c.post("/push/devices", headers=H(t["kobby"]), json=Phone(ok).sub()).status_code == 200


def test_an_agent_answering_taps_the_person_on_the_shoulder_once_a_minute(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    m = c.post("/messages", headers=H(t["kobby"]), json={"channel": "direct", "text": "@claude draft it", "mentions": ["claude"]}).json()
    assert sent(app, svc) == []                                   # your own message: nothing
    c.post("/messages", headers=H(t["claude"]), json={"reply_to": m["id"], "text": "Here's the draft:\nline two"})
    [got] = sent(app, svc)
    msg = phone.read(got["body"])
    assert msg == {"title": "claude answered", "body": "Here's the draft:", "url": f"/app#activity/{m['thread']}", "tag": f"t{m['thread']}",
                   "badge": 0}
    c.post("/messages", headers=H(t["claude"]), json={"reply_to": m["id"], "text": "and a fix"})
    assert len(sent(app, svc)) == 1                               # the same conversation, a moment later: no second buzz


def test_an_agent_waiting_for_an_ok_tells_its_owner(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    c.post("/keys", headers=H(t["kobby"]), json={"name": "instagram", "hosts": ["graph.facebook.com"], "secret": "IGQV-not-real"})
    r = c.post("/mcp", headers=H(t["claude"], "chat-1"), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "call_api", "arguments": {"key": "instagram", "method": "POST", "url": "https://graph.facebook.com/v21.0/me/media",
                                          "json": {"caption": "New drop"}, "why": "Post it"}}}).json()["result"]["structuredContent"]
    got = [phone.read(s["body"]) for s in sent(app, svc)]
    ask = next(g for g in got if g["url"].startswith("/app#approve/"))
    assert ask["title"] == "claude wants to post on Instagram" and "instagram" in ask["body"] and "“New drop”" in ask["body"]
    assert ask["url"] == f"/app#approve/{r['request']}"
    assert "IGQV" not in json.dumps(got)                          # the key itself never goes anywhere


def test_someone_asking_to_join_tells_the_owner_and_each_kind_can_be_turned_off(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone, kinds=["approvals", "answers"])     # invites off on this device
    c.post("/invite-requests", json={"name": "Abena", "contact": "abena@example.com"})
    assert sent(app, svc) == []
    assert c.put("/push/devices", headers=H(t["kobby"]), json={"endpoint": EP, "kinds": ["invites"]}).json()["kinds"] == ["invites"]
    c.post("/invite-requests", json={"name": "Kofi", "contact": "kofi@example.com"})
    [got] = sent(app, svc)
    assert phone.read(got["body"])["title"] == "Kofi would like an invite"


def test_a_device_the_push_service_says_is_gone_is_forgotten(hub):
    c, app, svc, t = hub
    on(c, t["ama"], Phone())
    assert c.post("/push/devices/find", headers=H(t["ama"]), json={"endpoint": EP}).json()["endpoint"] == EP
    svc.status = 410
    c.post("/push/test", headers=H(t["ama"]))
    sent(app, svc)
    assert c.post("/push/devices/find", headers=H(t["ama"]), json={"endpoint": EP}).json()["endpoint"] is None


def test_a_person_manages_only_their_own_devices_and_deleting_the_account_forgets_them(hub):
    c, app, svc, t = hub
    on(c, t["ama"], Phone())
    assert c.post("/push/devices/remove", headers=H(t["kobby"]), json={"endpoint": EP}).json() == {"removed": False}
    assert c.post("/me/delete", headers=H(t["ama"]), json={"confirm": "ama"}).status_code == 200
    with app.state.store.ro() as db:
        assert db.execute("SELECT COUNT(*) n FROM push_subs").fetchone()["n"] == 0


def test_the_service_worker_is_served_from_the_root(hub):
    c, *_ = hub
    r = c.get("/sw.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"] and "notificationclick" in r.text
    assert "worker-src 'self'" in c.get("/app").headers["content-security-policy"]
    # stamped with the app's version, so a deploy that changes the app replaces the copy kept on phones
    assert "__SHELL_VERSION__" not in r.text and re.search(r'VERSION = "[0-9a-f]{16}"', r.text)
    # what it may keep for offline: never keys, approvals' details or files
    keep = re.search(r"const KEEP = (/.*/);", r.text).group(1)
    assert "keys" not in keep and "media" not in keep and "artifacts" not in keep and "approvals" not in keep
    # kept apart per person, and only when they chose to stay signed in
    assert "X-Relay-Keep" in r.text and "crypto.subtle.digest" in r.text


def test_each_notification_says_how_many_things_wait_for_the_app_icon(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    c.post("/invite-requests", json={"name": "Kofi", "contact": "kofi@example.com"})
    c.post("/invite-requests", json={"name": "Esi", "contact": "esi@example.com"})
    msgs = [phone.read(s["body"]) for s in sent(app, svc)]
    assert [m["badge"] for m in msgs] == [1, 2]                   # the number on the Home Screen icon


def test_a_muted_conversation_doesnt_buzz_but_approvals_still_do(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    m = c.post("/messages", headers=H(t["kobby"]), json={"channel": "direct", "text": "@claude draft it", "mentions": ["claude"]}).json()
    assert c.put(f"/threads/{m['thread']}/mark", headers=H(t["kobby"]), json={"muted": True}).status_code == 200
    c.post("/messages", headers=H(t["claude"]), json={"reply_to": m["id"], "text": "Here's the draft"})
    assert sent(app, svc) == []
    c.post("/invite-requests", json={"name": "Kofi", "contact": "kofi@example.com"})
    assert [phone.read(s["body"])["title"] for s in sent(app, svc)] == ["Kofi would like an invite"]



def ask_for_ok(c, t):
    c.post("/keys", headers=H(t["kobby"]), json={"name": "instagram", "hosts": ["graph.facebook.com"], "secret": "IGQV-not-real"})
    return c.post("/mcp", headers=H(t["claude"], "chat-1"), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "call_api", "arguments": {"key": "instagram", "method": "POST", "url": "https://graph.facebook.com/v21.0/me/media",
                                          "json": {"caption": "New drop"}, "why": "Post it"}}}).json()["result"]["structuredContent"]["request"]


def test_approve_from_the_notification_with_a_one_time_pass_for_that_device(hub):
    c, app, svc, t = hub
    phone, laptop = Phone(), Phone("https://updates.push.services.mozilla.com/wpush/v2/laptop")
    on(c, t["kobby"], phone)
    on(c, t["kobby"], laptop)
    cid = ask_for_ok(c, t)
    got = {s["url"]: phone.read(s["body"]) if s["url"] == EP else laptop.read(s["body"]) for s in sent(app, svc)
           if "approve" in (phone.read(s["body"]) if s["url"] == EP else laptop.read(s["body"]))["url"]}
    p_act, l_act = got[EP]["act"], got[laptop.endpoint]["act"]
    assert p_act != l_act and p_act.startswith("rly_act_")                         # one pass per device
    assert c.post("/push/act", json={"t": "rly_act_" + "x" * 40, "ok": True}).status_code == 410   # a made-up pass
    r = c.post("/push/act", json={"t": p_act, "ok": True})
    assert r.status_code == 200 and r.json()["status"] == "done" and r.json()["site"] == "Instagram"
    assert c.post("/push/act", json={"t": p_act, "ok": True}).status_code == 410      # once
    assert c.post("/push/act", json={"t": l_act, "ok": False}).status_code == 410     # the other device's pass is spent too


def test_a_pass_stops_working_when_its_device_is_turned_off_or_the_request_is_answered(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    cid = ask_for_ok(c, t)
    act = next(phone.read(s["body"]) for s in sent(app, svc) if "approve" in phone.read(s["body"])["url"])["act"]
    c.post("/push/devices/remove", headers=H(t["kobby"]), json={"endpoint": EP})
    assert c.post("/push/act", json={"t": act, "ok": True}).status_code == 410
    on(c, t["kobby"], phone)
    cid2 = ask_for_ok(c, t)
    act2 = [phone.read(s["body"]) for s in sent(app, svc)][-1]["act"]
    assert c.post(f"/approvals/{cid2}/decline", headers=H(t["kobby"])).status_code == 200   # answered in the app
    assert c.post("/push/act", json={"t": act2, "ok": True}).status_code == 409


def session_of(c, tok):
    """A signed-in session for this person (as the app holds after a passkey or email sign-in)."""
    code = c.post("/auth/handoff", headers=H(tok)).json()["code"]
    return c.post("/auth/handoff/claim", json={"code": code}).json()["token"]


def test_a_device_whose_sign_in_ended_gets_no_more_passes_or_notifications(hub):
    c, app, svc, t = hub
    sess = session_of(c, t["kobby"])
    stray = Phone("https://updates.push.services.mozilla.com/wpush/v2/stray")
    on(c, sess, stray, kinds=["approvals"])                      # someone held this session for a moment
    assert [d["this"] for d in c.get("/push/devices", headers=H(sess)).json()] == [True]
    c.post("/auth/signout", headers=H(sess))                     # signed out: that device is let go
    ask_for_ok(c, t)
    assert [s for s in sent(app, svc) if s["url"] == stray.endpoint] == []
    with app.state.store.ro() as db:
        assert db.execute("SELECT COUNT(*) FROM push_subs WHERE endpoint=?", (stray.endpoint,)).fetchone()[0] == 0


def test_a_session_that_simply_expires_takes_its_device_and_its_passes_with_it(hub):
    c, app, svc, t = hub
    sess = session_of(c, t["kobby"])
    phone = Phone()
    on(c, sess, phone)
    ask_for_ok(c, t)
    act = next(phone.read(s["body"]) for s in sent(app, svc) if "approve" in phone.read(s["body"])["url"]).get("act")
    assert act
    with app.state.store.tx() as db:                               # the session ends without a sign-out
        db.execute("DELETE FROM sessions")
    assert c.post("/push/act", json={"t": act, "ok": True}).status_code == 410


def test_any_device_of_yours_can_be_removed_from_the_list(hub):
    c, app, svc, t = hub
    on(c, t["kobby"], Phone())
    on(c, t["kobby"], Phone("https://web.push.apple.com/lost-phone"))
    devs = c.get("/push/devices", headers=H(t["kobby"])).json()
    assert len(devs) == 2 and all("endpoint" not in d for d in devs)
    assert c.get("/push/devices", headers=H(t["ama"])).json() == []                     # only your own
    assert c.post("/push/devices/remove", headers=H(t["ama"]), json={"endpoint": "dev_" + devs[1]["id"]}).json() == {"removed": False}
    assert c.post("/push/devices/remove", headers=H(t["kobby"]), json={"endpoint": "dev_" + devs[1]["id"]}).json() == {"removed": True}
    assert len(c.get("/push/devices", headers=H(t["kobby"])).json()) == 1


def test_the_notification_shows_the_exact_address_and_buttons_only_when_it_shows_everything(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    c.post("/keys", headers=H(t["kobby"]), json={"name": "gmail", "hosts": ["gmail.googleapis.com"], "secret": "ya29-not-real"})
    c.post("/keys", headers=H(t["kobby"]), json={"name": "stripe", "hosts": ["api.stripe.com"], "secret": "sk_not_real"})

    def call(key, method, url, body):
        c.post("/mcp", headers=H(t["claude"], "chat-1"), json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
            "name": "call_api", "arguments": {"key": key, "method": method, "url": url, "json": body, "why": "x"}}})
        return [phone.read(s["body"]) for s in sent(app, svc)][-1]
    g = call("gmail", "DELETE", "https://gmail.googleapis.com/gmail/v1/users/me/messages/1", None)
    assert g["title"] == "claude wants to delete on gmail.googleapis.com" and "DELETE gmail.googleapis.com/gmail" in g["body"]
    assert "act" not in g and "Tap to review" in g["body"]                                    # deleting: open Relay
    s = call("stripe", "POST", "https://api.stripe.com/v1/transfers", {"description": "Refresh the report (read-only)\n\n\n\n.", "amount": 5000000})
    assert "\n\n" not in s["body"] and "POST api.stripe.com/v1/transfers" in s["body"] and "act" not in s     # more than it shows


def test_approving_from_the_notification_tells_the_agent(hub):
    c, app, svc, t = hub
    phone = Phone()
    on(c, t["kobby"], phone)
    cid = ask_for_ok(c, t)
    act = next(phone.read(s["body"]) for s in sent(app, svc) if "approve" in phone.read(s["body"])["url"])["act"]
    assert c.post("/push/act", json={"t": act, "ok": False}).status_code == 200
    got = c.post("/mcp", headers=H(t["claude"], "chat-1"), json={"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                 "params": {"name": "check_replies", "arguments": {}}}).json()["result"]["structuredContent"]["messages"]
    assert any(m["text"].startswith("✋") for m in got)
