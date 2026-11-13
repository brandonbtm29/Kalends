# ruff: noqa: F811 - the panel/store fixtures are imported from test_v050
"""0.15.0: notifications for everything, browser notifications, and alerts for people you share with."""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from argon2 import PasswordHasher
from conftest import COWORKER_SLUG, PARTNER_PASSWORD, PARTNER_SLUG
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient
from test_v050 import NOW, csrf_rev, panel, store  # noqa: F401

from kalends import webpush
from kalends.alerts import DAILY_CAP, Alerts
from kalends.config import ConfigError
from kalends.notify import Note
from kalends.web.app import create_app
from kalends.web.auth import feed_token
from kalends.web.viewer import person_viewer

NY = ZoneInfo("America/New_York")


def at(m: int, d: int, hh: int, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(2026, m, d, hh, mm, ss, tzinfo=NY)


def clock(rt, when: datetime) -> None:
    rt.kalends.now = lambda: when.astimezone(UTC)
    rt.kalends.today = lambda: when.astimezone(NY).date()


class Browser:
    """A pretend browser: its push keys, and the decryption a real one does."""

    def __init__(self):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.auth = os.urandom(16)
        self.public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        self.endpoint = "https://push.example.net/send/" + secrets.token_hex(8)

    def subscription(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": webpush.b64e(self.public), "auth": webpush.b64e(self.auth)},
        }

    def read(self, body: bytes) -> dict:
        salt, idlen = body[:16], body[20]
        sender = body[21 : 21 + idlen]
        shared = self.key.exchange(
            ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender)
        )
        ikm = HKDF(hashes.SHA256(), 32, self.auth, b"WebPush: info\x00" + self.public + sender).derive(shared)
        cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
        nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
        plain = AESGCM(cek).decrypt(nonce, body[21 + idlen :], None)
        assert plain.endswith(b"\x02")
        return json.loads(plain[:-1])


class PushService:
    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        return httpx.Response(self.status.get(str(req.url), 201))

    def to(self, browser: Browser) -> list[dict]:
        return [browser.read(r.content) for r in self.calls if str(r.url) == browser.endpoint]


@pytest.fixture
def push(panel, monkeypatch):
    rt, _ = panel
    svc = PushService()
    monkeypatch.setattr(rt.alerts, "_client", lambda: httpx.Client(transport=httpx.MockTransport(svc)))
    return svc


@pytest.fixture
def told(panel, monkeypatch):
    """Everything Kalends tells you (the owner)."""
    rt, _ = panel
    notes: list[Note] = []
    monkeypatch.setattr(rt.notifier, "owner", lambda note, wait=False: notes.append(note))
    return notes


@pytest.fixture
def ntfy(panel, monkeypatch):
    """Alerts sent to the ntfy topics of people you share with."""
    rt, _ = panel
    sent: list[tuple[str, Note]] = []

    def record(server, topic, note):
        sent.append((topic, note))
        return "Sent"

    monkeypatch.setattr(rt.notifier, "ntfy_to", record)
    return sent


def public(rt) -> TestClient:
    app = create_app(
        rt.cfg,
        kalends=rt.kalends,
        background_refresh=False,
        seen=rt.seen,
        requests=rt.requests,
        notifier=rt.notifier,
        alerts=rt.alerts,
    )
    return TestClient(app, follow_redirects=False)


def update(rt, mutate) -> None:
    rt.apply(rt.store.update(mutate), fetch=False)


def alerts_on(aid, **extra):
    def mutate(d):
        d.audiences[aid]["alerts"] = {"enabled": True, **extra}

    return mutate


def edit_feed(rt, cid: str, old: str, new: str) -> None:
    st = rt.kalends.cache.states[cid]
    assert old in st.text, old
    st.text = st.text.replace(old, new)
    st.version += 1


def token_of(html: str) -> str:
    return re.search(r'name="token" value="([^"]+)"', html).group(1)


# -- browser notifications --------------------------------------------------------------------------


def test_web_push_is_encrypted_for_one_browser_and_signed():
    key = webpush.new_private_key()
    b, other = Browser(), Browser()
    msg = {"title": "Dinner moved", "body": "Thu Nov 19, 7:00 PM – 9:00 PM", "url": "https://x"}
    sub = b.subscription()
    body = webpush.encrypt(json.dumps(msg).encode(), sub["keys"]["p256dh"], sub["keys"]["auth"])
    assert b.read(body) == msg
    with pytest.raises(InvalidTag):
        other.auth = b.auth
        other.read(body)
    header = webpush.vapid_header(b.endpoint, key, "https://pc.example.ts.net")
    token, k = re.fullmatch(r"vapid t=(\S+), k=(\S+)", header).groups()
    head, claims, sig = token.split(".")
    raw = webpush.b64d(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), webpush.b64d(k))
    public.verify(
        encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
        f"{head}.{claims}".encode(),
        ec.ECDSA(hashes.SHA256()),
    )
    c = json.loads(webpush.b64d(claims))
    assert c["aud"] == "https://push.example.net" and c["sub"] == "https://pc.example.ts.net"
    assert k == webpush.public_key(key)
    svc = PushService()
    flat = {"endpoint": b.endpoint, **sub["keys"]}
    with httpx.Client(transport=httpx.MockTransport(svc)) as client:
        ok = webpush.send(client, flat, msg, key, "https://pc.example.ts.net")
        assert ok.ok and not ok.gone and svc.calls[0].headers["Content-Encoding"] == "aes128gcm"
        svc.status[b.endpoint] = 410
        gone = webpush.send(client, flat, msg, key, "https://pc.example.ts.net")
        assert gone.gone and not gone.ok
        assert webpush.send(client, {**flat, "endpoint": "http://plain"}, msg, key, "x").gone
    with pytest.raises(ValueError):
        webpush.encrypt(b"x" * 5000, sub["keys"]["p256dh"], sub["keys"]["auth"])


def test_settings_defaults_and_checks(store):
    cfg = store.current()
    r = cfg.notify.routes
    assert r["reminders"] == ["ntfy", "webpush"] and r["summary"] == ["ntfy", "webpush", "email"]
    assert r["requests"] == ["ntfy", "webpush", "email", "discord"]
    assert not cfg.notify.reminders.enabled and cfg.notify.problems.enabled and cfg.notify.activity.enabled
    assert not cfg.audiences["partner"].alerts.enabled
    with pytest.raises(ConfigError):
        store.update(lambda d: d.section("notify").update({"routes": {"reminders": ["pager"]}}))
    with pytest.raises(ConfigError):
        store.update(lambda d: d.section("notify").update({"routes": {"gossip": ["ntfy"]}}))

    def mutate(d):
        d.section("notify")["reminders"] = {"enabled": True, "calendars": ["lab", "gone"]}
        d.ensure_self()
        d.audiences["me"]["alerts"] = {"enabled": True}

    cfg = store.update(mutate)
    assert cfg.notify.reminders.calendars == ["lab"]  # a removed calendar drops out quietly
    assert cfg.self_audience[1].alerts.enabled is False  # you get your own notifications instead


def test_each_kind_goes_only_where_it_is_routed(panel, monkeypatch):
    rt, c = panel

    def channels(d):
        n = d.section("notify")
        n["ntfy"] = {"enabled": True, "topic": "kalends-test-topic"}
        n["discord"] = {"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/abc"}

    update(rt, channels)
    sent = []
    monkeypatch.setattr(rt.notifier, "send", lambda ch, note: sent.append((ch, note.kind)))
    rt.notifier.owner(Note("a", "b", kind="reminders"), wait=True)
    rt.notifier.owner(Note("a", "b", kind="requests"), wait=True)
    assert sent == [("ntfy", "reminders"), ("ntfy", "requests"), ("discord", "requests")]
    # untick Discord for meeting requests, tick it for reminders
    csrf, rev = csrf_rev(c, "/notifications")
    form = {
        "csrf": csrf,
        "rev": rev,
        "kinds_form": "1",
        "ntfy_enabled": "on",
        "discord_enabled": "on",
        "route_requests_ntfy": "on",
        "route_reminders_discord": "on",
        "prob_enabled": "on",
        "act_enabled": "on",
        "act_first": "on",
        "act_lockouts": "on",
        "act_expiring": "3",
        "prob_after": "60",
    }
    assert c.post("/notifications", data=form).status_code == 303
    assert rt.cfg.notify.routes["requests"] == ["ntfy"] and rt.cfg.notify.routes["reminders"] == ["discord"]
    sent.clear()
    rt.notifier.owner(Note("a", "b", kind="requests"), wait=True)
    assert sent == [("ntfy", "requests")]


def test_your_browsers_on_the_panel(panel, push):
    rt, c = panel
    html = c.get("/notifications").text
    key = re.search(r'data-key="([^"]+)"', html).group(1)
    assert key == rt.alerts.vapid_public()
    env, yaml_text = (
        rt.store.env_path.read_text(encoding="utf-8"),
        rt.store.config_path.read_text(encoding="utf-8"),
    )
    assert "KALENDS_VAPID_KEY=" in env and rt.cfg.notify.webpush.vapid_private_key not in yaml_text
    assert ">What goes where<" in html and html.count('name="route_') == 7 * 4
    b = Browser()
    csrf, _ = csrf_rev(c, "/notifications")
    assert c.post("/notifications/push", json={"subscription": b.subscription()}).status_code == 403
    ua = {"User-Agent": "Mozilla/5.0 (Macintosh) Chrome/131 Safari/537"}
    r = c.post("/notifications/push", json={"subscription": b.subscription()}, headers={"X-CSRF": csrf, **ua})
    assert r.json() == {"ok": True}
    assert push.to(b)[0]["body"] == "Notifications are on for this browser."
    assert "Mac · Chrome" in c.get("/notifications").text and "webpush" in rt.notifier.enabled()
    c.post("/notifications/test/webpush", data={"csrf": csrf})
    assert push.to(b)[-1]["title"] == "Kalends test"
    # a browser that turned notifications off is forgotten on the next send
    push.status[b.endpoint] = 410
    rt.notifier.send("webpush", Note("x", "y"))
    assert rt.alerts.subs.owner_devices() == [] and "webpush" not in rt.notifier.enabled()
    push.status.clear()
    r = c.post("/notifications/push", json={"subscription": b.subscription()}, headers={"X-CSRF": csrf})
    sid = rt.alerts.subs.owner_devices()[0]["id"]
    c.post(f"/notifications/push/{sid}/delete", data={"csrf": csrf})
    assert rt.alerts.subs.owner_devices() == []


# -- reminders and the morning summary ----------------------------------------------------------------


def test_reminders_before_events_once(panel, told):
    rt, _ = panel
    update(rt, lambda d: d.section("notify").update({"reminders": {"enabled": True, "minutes": 10}}))
    clock(rt, at(10, 27, 9, 49))
    rt.alerts.tick()
    assert told == []
    clock(rt, at(10, 27, 9, 50, 20))
    rt.alerts.tick()
    assert [n.title for n in told] == ["In 10 min: Doctor appointment"]  # the #private tag is left out
    n = told[0]
    assert n.kind == "reminders" and n.urgent and "10:00 AM – 11:00 AM" in n.body and "Personal" in n.body
    clock(rt, at(10, 27, 9, 50, 50))
    rt.alerts.tick()
    again = Alerts(lambda: rt.cfg, lambda: rt.kalends, rt.notifier, rt.store.config_path.parent)
    clock(rt, at(10, 27, 9, 51, 30))
    again.tick()  # after a restart: still only once
    assert len(told) == 1
    # Kalends was off for an hour: no pile of late reminders
    clock(rt, at(10, 27, 15, 55))
    rt.alerts.tick()
    assert [n.title for n in told[1:]] == ["In 10 min: Group meeting (moved)"]


def test_morning_summary(panel, told):
    rt, _ = panel
    update(rt, lambda d: d.section("notify").update({"summary": {"enabled": True, "at": "07:30"}}))
    clock(rt, at(10, 28, 7, 29))
    rt.alerts.tick()
    assert told == []
    clock(rt, at(10, 28, 7, 31))
    rt.alerts.tick()
    (n,) = told
    assert n.kind == "summary" and n.title.startswith("Today: ")
    assert (
        "7:00 AM · Gym" in n.body and "7:00 PM · Dinner with Sam" in n.body and "1:1 with advisor" in n.body
    )
    clock(rt, at(10, 28, 8, 0))
    rt.alerts.tick()
    clock(rt, at(10, 29, 11, 0))  # too late in the day to be useful
    rt.alerts.tick()
    assert len(told) == 1


# -- calendar problems -----------------------------------------------------------------------------------


def test_calendar_problems(panel, told):
    rt, _ = panel
    st = rt.kalends.cache.states["lab"]
    t0 = NOW
    st.last_error, st.last_attempt = "ConnectError: no route", t0 + timedelta(minutes=30)
    clock(rt, t0 + timedelta(minutes=30))
    rt.alerts.check_feeds()
    assert told == []
    clock(rt, t0 + timedelta(hours=2))
    rt.alerts.check_feeds()
    rt.alerts.check_feeds()
    assert [n.title for n in told] == ["Lab isn't updating"] and "2 hours" in told[0].body
    st.last_error = None
    rt.alerts.check_feeds()
    assert told[-1].title == "Lab is updating again"
    good = st.text
    st.text, st.version = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", st.version + 1
    assert rt.alerts.check_feeds() == {"lab"}
    assert told[-1].title == "Lab came back empty" and told[-1].kind == "problems"
    st.text, st.version = good, st.version + 1
    assert rt.alerts.check_feeds() == set()
    assert told[-1].title == "Lab has events again" and len(told) == 4


# -- changes ---------------------------------------------------------------------------------------------


def test_changes_on_calendars_you_pick(panel, told):
    rt, _ = panel
    update(rt, lambda d: d.section("notify").update({"changes": {"calendars": ["family"], "days_ahead": 14}}))
    rt.alerts.after_refresh()
    assert told == []
    edit_feed(
        rt,
        "family",
        "DTSTART;TZID=America/New_York:20261027T183000",
        "DTSTART;TZID=America/New_York:20261028T183000",
    )
    edit_feed(
        rt,
        "family",
        "DTEND;TZID=America/New_York:20261027T200000",
        "DTEND;TZID=America/New_York:20261028T200000",
    )
    edit_feed(rt, "family", "SUMMARY:Family dinner", "SUMMARY:Family dinner\r\nSTATUS:CANCELLED")
    rt.alerts.after_refresh()
    (n,) = told
    assert n.kind == "changes" and n.title == "2 changes on Family"
    assert n.body.splitlines() == [
        "Moved: Grandma visit → Wed Oct 28, 6:30 PM – 8:00 PM (was Tue Oct 27, 6:30 PM – 8:00 PM)",
        "Cancelled: Sun Nov 1, 5:00 PM – 7:00 PM · Family dinner",
    ]
    rt.alerts.after_refresh()
    assert len(told) == 1


def test_an_emptied_calendar_is_a_problem_not_a_cancellation(panel, told):
    rt, _ = panel
    update(rt, lambda d: d.section("notify").update({"changes": {"calendars": ["personal"]}}))
    rt.alerts.after_refresh()
    st = rt.kalends.cache.states["personal"]
    st.last_attempt = NOW
    st.text, st.version = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", st.version + 1
    rt.alerts.after_refresh()
    assert [n.title for n in told] == ["Personal came back empty"]


# -- people you share with ---------------------------------------------------------------------------------


def test_people_get_only_what_their_audience_sees(panel, ntfy):
    rt, _ = panel
    update(rt, alerts_on("coworkers"))
    update(rt, alerts_on("partner"))
    pub = public(rt)
    html = pub.get(f"/a/{COWORKER_SLUG}/alerts").text
    assert (
        "The ntfy app" in html and "Notifications on this device" in html and "Email" not in html
    )  # no email set up
    r = pub.post(
        f"/a/{COWORKER_SLUG}/alerts", data={"token": token_of(html), "changes": "on", "channel": "ntfy"}
    )
    assert r.status_code == 303 and r.headers["location"].endswith("?new=1")
    manage = pub.get(r.headers["location"]).text
    topic = re.search(r'<code class="topic">([^<]+)</code>', manage).group(1)
    assert (
        ntfy[-1] == (topic, ntfy[-1][1]) and ntfy[-1][1].title == "Alerts are on for Brandon - availability"
    )
    # the partner page needs its password first
    assert "login" in pub.get(f"/a/{PARTNER_SLUG}/alerts").headers["location"]
    pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD, "next": "alerts"})
    html = pub.get(f"/a/{PARTNER_SLUG}/alerts").text
    pub.post(f"/a/{PARTNER_SLUG}/alerts", data={"token": token_of(html), "changes": "on", "channel": "ntfy"})
    partner_topic = ntfy[-1][0]
    rt.alerts.after_refresh()
    ntfy.clear()
    for old, new in (
        ("20261027T100000", "20261027T140000"),
        ("20261027T110000", "20261027T150000"),
        ("20261028T190000", "20261028T200000"),
        ("20261028T210000", "20261028T220000"),
    ):
        edit_feed(rt, "personal", f"America/New_York:{old}", f"America/New_York:{new}")  # doctor, then dinner
    rt.alerts.after_refresh()
    got = {t: n for t, n in ntfy}
    coworkers, partner = got[topic], got[partner_topic]
    assert coworkers.title == "Brandon - availability: 2 changes"
    assert coworkers.body.splitlines() == [
        "Cancelled: Tue Oct 27, 10:00 AM – 11:00 AM · Busy",
        "Added: Tue Oct 27, 2:00 PM – 3:00 PM · Busy",
    ]  # the doctor's appointment is just busy time to coworkers, and dinner is outside their hours
    assert (
        "Moved: Dinner with Sam → Wed Oct 28, 8:00 PM – 10:00 PM (was Wed Oct 28, 7:00 PM – 9:00 PM)"
        in partner.body
    )
    assert "Doctor" not in partner.body  # #private: busy for everyone


def test_browser_alerts_reminders_in_their_time_zone(panel, push):
    rt, _ = panel
    update(rt, alerts_on("partner"))
    pub = public(rt)
    pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD})
    html = pub.get(f"/a/{PARTNER_SLUG}/alerts").text
    assert re.search(r'data-key="([^"]+)"', html).group(1) == rt.alerts.vapid_public()
    b = Browser()
    body = {
        "token": token_of(html),
        "subscription": b.subscription(),
        "changes": False,
        "reminders": True,
        "lead": 30,
        "tz": "America/Denver",
    }
    r = pub.post(f"/a/{PARTNER_SLUG}/alerts/push", json=body, headers={"User-Agent": "iPhone Safari/605"})
    assert r.json()["ok"] and r.json()["manage"].startswith("/alerts/")
    assert push.to(b)[0]["title"] == "Alerts are on for Brandon's week"
    assert pub.post(f"/a/{PARTNER_SLUG}/alerts/push", json={**body, "token": "nope"}).status_code == 400
    clock(rt, at(10, 28, 18, 30, 10))
    rt.alerts.tick()
    msg = push.to(b)[-1]
    assert msg["title"] == "In 30 min: Dinner with Sam"
    assert msg["body"] == "Wed Oct 28, 5:00 PM – 7:00 PM · Brandon's week"  # their clock, not yours
    assert msg["url"].endswith(f"/a/{PARTNER_SLUG}")
    clock(rt, at(10, 28, 18, 31))
    rt.alerts.tick()
    assert len(push.to(b)) == 2


def test_email_alerts_need_confirming(panel, monkeypatch):
    rt, _ = panel
    update(
        rt,
        lambda d: d.section("notify").update(
            {"email": {"enabled": True, "smtp_host": "smtp.example.org", "username": "me@example.org"}}
        ),
    )
    update(rt, alerts_on("partner", reminders=False))
    mails = []
    monkeypatch.setattr(
        rt.notifier,
        "_email",
        lambda to, subject, body, ics, record=True, headers=None: (
            mails.append((to, subject, body, headers)) or "ok"
        ),
    )
    pub = public(rt)
    pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD})
    html = pub.get(f"/a/{PARTNER_SLUG}/alerts").text
    assert "Before events start" not in html  # reminders aren't offered on this audience
    bad = pub.post(
        f"/a/{PARTNER_SLUG}/alerts",
        data={"token": token_of(html), "changes": "on", "channel": "email", "email": "not-an-address"},
    )
    assert bad.status_code == 400 and "look right" in bad.text
    ok = pub.post(
        f"/a/{PARTNER_SLUG}/alerts",
        data={"token": token_of(html), "changes": "on", "channel": "email", "email": "sam@example.org"},
    )
    assert "Check your email" in ok.text
    to, subject, body, _ = mails[-1]
    assert to == "sam@example.org" and subject == "Confirm alerts for Brandon's week"
    link = re.search(r"https?://\S+/alerts/(\S+)/confirm", body)
    token = link.group(1)
    rt.alerts.after_refresh()
    edit_feed(rt, "personal", "SUMMARY:Dinner with Sam", "SUMMARY:Dinner with Sam and Alex")
    rt.alerts.after_refresh()
    assert len(mails) == 1  # nothing until the address is confirmed
    page = pub.get(f"/alerts/{token}/confirm").text
    assert "Yes, turn them on" in page
    pub.post(f"/alerts/{token}", data={"action": "confirm"})
    rt.alerts.after_refresh()  # from now on, changes are compared with this
    edit_feed(rt, "personal", "SUMMARY:Dinner with Sam and Alex", "SUMMARY:Dinner with Alex")
    rt.alerts.after_refresh()
    to, subject, body, headers = mails[-1]
    assert (
        subject == "Brandon's week: a change"
        and "Renamed: Dinner with Sam and Alex → Dinner with Alex" in body
    )
    assert headers["List-Unsubscribe"] == f"<https://pc.example.ts.net/alerts/{token}>"
    assert f"/alerts/{token}" in body


def test_a_new_link_or_password_stops_alerts(panel, ntfy):
    rt, _ = panel
    update(rt, alerts_on("coworkers"))
    update(rt, alerts_on("partner"))
    pub = public(rt)

    def signup(base: str) -> str:
        html = pub.get(f"{base}/alerts").text
        r = pub.post(f"{base}/alerts", data={"token": token_of(html), "changes": "on", "channel": "ntfy"})
        return r.headers["location"].split("?")[0]

    coworkers = signup(f"/a/{COWORKER_SLUG}")
    pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": PARTNER_PASSWORD})
    partner = signup(f"/a/{PARTNER_SLUG}")
    pid = []
    update(rt, lambda d: pid.append(d.add_person("Benjamin", "coworkers")))
    ben = signup(f"/p/{rt.cfg.people[pid[0]].slug}")
    assert len(rt.alerts.subs.viewers()) == 3
    assert pub.get(partner).status_code == 200
    new_hash = PasswordHasher().hash("a new password")
    update(rt, lambda d: d.set_secret(d.audiences["partner"], "password_hash", new_hash, "PARTNER_HASH"))
    assert pub.get(partner).status_code == 404  # gone: the password changed
    update(
        rt,
        lambda d: d.people[pid[0]].update({"expires": (rt.kalends.today() - timedelta(days=1)).isoformat()}),
    )
    assert pub.get(ben).status_code == 404
    assert pub.get(coworkers).status_code == 200
    update(rt, lambda d: d.audiences["coworkers"].update({"slug": "coworkerSlug_brandNewLink01"}))
    assert pub.get(coworkers).status_code == 404 and rt.alerts.subs.viewers() == []


def test_manage_page_daily_cap_and_switching_off(panel, ntfy):
    rt, c = panel
    update(rt, alerts_on("coworkers"))
    pub = public(rt)
    html = pub.get(f"/a/{COWORKER_SLUG}/alerts").text
    r = pub.post(
        f"/a/{COWORKER_SLUG}/alerts", data={"token": token_of(html), "changes": "on", "channel": "ntfy"}
    )
    manage = r.headers["location"].split("?")[0]
    saved = pub.post(manage, data={"action": "save", "reminders": "on", "lead": "60"})
    assert "Saved." in saved.text
    (sub,) = rt.alerts.subs.viewers()
    assert sub["reminders"] and not sub["changes"] and sub["lead"] == 60
    rt.alerts.subs.update(sub["id"], day=rt.kalends.today().isoformat(), sent=DAILY_CAP)
    assert rt.alerts._send_sub(rt.alerts.subs.get(sub["id"]), "x", "y", None) is False
    # the panel lists it and can stop it
    tab = c.get("/audiences/coworkers?tab=alerts").text
    assert "Who gets alerts" in tab and "ntfy app" in tab and "1 hour before" in tab
    csrf, rev = csrf_rev(c, "/audiences/coworkers?tab=alerts")
    c.post(f"/audiences/coworkers/alerts/{sub['id']}/delete", data={"csrf": csrf})
    assert rt.alerts.subs.viewers() == []
    # switching alerts off removes the page link and everyone's alerts
    r = pub.post(
        f"/a/{COWORKER_SLUG}/alerts", data={"token": token_of(html), "changes": "on", "channel": "ntfy"}
    )
    csrf, rev = csrf_rev(c, "/audiences/coworkers?tab=alerts")
    c.post("/audiences/coworkers/alerts", data={"csrf": csrf, "rev": rev, "alerts_changes": "on"})
    assert not rt.cfg.audiences["coworkers"].alerts.enabled and rt.alerts.subs.viewers() == []
    assert pub.get(f"/a/{COWORKER_SLUG}/alerts").status_code == 404
    stop = pub.post(r.headers["location"].split("?")[0], data={"action": "stop"})
    assert stop.status_code == 404


# -- link activity -------------------------------------------------------------------------------------------


def test_link_activity(panel, told):
    rt, _ = panel
    pid = []
    update(rt, lambda d: pid.append(d.add_person("Benjamin", "partner")))
    person = rt.cfg.people[pid[0]]
    pub = public(rt)
    base = f"/p/{person.slug}"
    pub.post(f"{base}/login", data={"password": PARTNER_PASSWORD})
    pub.get(f"{base}/week/2026-10-26")
    pub.get(f"{base}/week/2026-10-26")
    assert [n.title for n in told] == ["Benjamin opened your calendar"]
    pub.get(f"{base}/feed/{feed_token(rt.cfg, person_viewer(rt.cfg, pid[0]))}.ics")
    assert told[-1].title == "Benjamin subscribed to your calendar" and told[-1].kind == "activity"
    for _ in range(7):
        pub.post(f"/a/{PARTNER_SLUG}/login", data={"password": "wrong"})
    assert [n.title for n in told].count("Too many wrong passwords") == 1
    update(
        rt,
        lambda d: d.people[pid[0]].update({"expires": (rt.kalends.today() + timedelta(days=2)).isoformat()}),
    )
    rt.alerts.check_expiring()
    rt.alerts.check_expiring()
    assert told[-1].title == "Benjamin's link ends on Mon Oct 26" and len(told) == 4
    # your own view never counts as someone else opening a link
    update(rt, lambda d: d.ensure_self())
    rt.alerts.link_opened(rt.cfg.self_audience[1], None)
    assert len(told) == 4


# -- the pages ---------------------------------------------------------------------------------------------


def test_notifications_page_saves_every_kind(panel):
    rt, c = panel
    csrf, rev = csrf_rev(c, "/notifications")
    form = {
        "csrf": csrf,
        "rev": rev,
        "kinds_form": "1",
        "ntfy_server": "https://ntfy.sh",
        "route_reminders_ntfy": "on",
        "route_reminders_email": "on",
        "route_summary_webpush": "on",
        "rem_enabled": "on",
        "rem_minutes": "15",
        "rem_cal": ["lab", "nope"],
        "sum_enabled": "on",
        "sum_at": "06:45",
        "sum_skip_empty": "on",
        "prob_enabled": "on",
        "prob_after": "180",
        "act_enabled": "on",
        "act_first": "on",
        "act_expiring": "7",
        "chg_cal": ["family"],
        "chg_days": "30",
    }
    assert c.post("/notifications", data=form).status_code == 303
    n = rt.cfg.notify
    assert n.routes["reminders"] == ["ntfy", "email"] and n.routes["summary"] == ["webpush"]
    assert n.routes["requests"] == []  # nothing ticked for it
    assert (n.reminders.enabled, n.reminders.minutes, n.reminders.calendars) == (True, 15, ["lab"])
    assert n.summary.at.strftime("%H:%M") == "06:45" and n.summary.skip_empty
    assert n.problems.after_minutes == 180 and n.activity.expiring_days == 7 and not n.activity.lockouts
    assert n.changes.calendars == ["family"] and n.changes.days_ahead == 30
    html = c.get("/notifications").text
    assert 'name="rem_cal" value="lab" checked' in html and ">1 calendar<" in html


def test_public_pages_offer_alerts_and_work_as_home_screen_apps(panel):
    rt, c = panel
    pub = public(rt)
    page = pub.get(f"/a/{COWORKER_SLUG}/week/2026-10-26")
    assert "Get alerts" not in page.text
    assert f'<link rel="manifest" href="/a/{COWORKER_SLUG}/manifest.webmanifest">' in page.text
    csp = page.headers["Content-Security-Policy"]
    assert "manifest-src 'self'" in csp and "worker-src 'self'" in csp and "connect-src 'self'" in csp
    assert pub.get(f"/a/{COWORKER_SLUG}/alerts").status_code == 404
    csrf, rev = csrf_rev(c, "/audiences/coworkers?tab=alerts")
    c.post(
        "/audiences/coworkers/alerts",
        data={
            "csrf": csrf,
            "rev": rev,
            "alerts_enabled": "on",
            "alerts_changes": "on",
            "alerts_days": "7",
            "alerts_channel": ["webpush", "ntfy"],
        },
    )
    a = rt.cfg.audiences["coworkers"].alerts
    assert (
        a.enabled
        and a.changes
        and not a.reminders
        and a.days_ahead == 7
        and a.channels == ["webpush", "ntfy"]
    )
    assert (
        f'href="/a/{COWORKER_SLUG}/alerts">Get alerts' in pub.get(f"/a/{COWORKER_SLUG}/week/2026-10-26").text
    )
    m = pub.get(f"/a/{COWORKER_SLUG}/manifest.webmanifest").json()
    assert m["start_url"] == f"/a/{COWORKER_SLUG}" and m["display"] == "standalone"
    for client in (pub, c):
        sw = client.get("/sw.js")
        assert (
            sw.status_code == 200
            and "javascript" in sw.headers["content-type"]
            and "showNotification" in sw.text
        )
    assert c.get("/manifest.webmanifest").json()["start_url"] == "/"
    assert ">Alerts" in c.get("/audiences/coworkers").text
    update(rt, lambda d: d.ensure_self())
    assert "tab=alerts" not in c.get("/audiences/me").text  # your own view has no alerts tab


def test_reminders_skip_busy_blocks_for_busy_audiences(panel, ntfy):
    rt, _ = panel
    update(rt, alerts_on("coworkers"))
    pub = public(rt)
    html = pub.get(f"/a/{COWORKER_SLUG}/alerts").text
    pub.post(
        f"/a/{COWORKER_SLUG}/alerts",
        data={"token": token_of(html), "reminders": "on", "lead": "10", "channel": "ntfy"},
    )
    ntfy.clear()
    clock(rt, at(10, 27, 9, 50, 30))  # the doctor's appointment is only "Busy" for coworkers
    rt.alerts.tick()
    clock(rt, at(10, 27, 10, 50, 30))  # POLY 701 at 11:00 shows as the "Class" label
    rt.alerts.tick()
    assert [n.title for _, n in ntfy] == ["In 10 min: Class"]
