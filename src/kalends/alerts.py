"""Notifications beyond meeting requests, for you and for the people you share with.

For you (through the channels picked for each kind on the Notifications page):

* **reminders** a few minutes before your events, and a **morning summary** of the day;
* **calendar problems**: a calendar that stops updating, comes back empty, or recovers;
* **link activity**: a link opened (or subscribed to) for the first time, a person's link
  about to end, too many wrong passwords;
* **changes** (added, moved, cancelled) on calendars you choose, typically ones other
  people edit.

For the people you share with, when an audience allows it: alerts about *what that audience
can see*, never more (a busy-only audience hears about busy times, not titles), by browser
notification, email (after they confirm their address) or the ntfy app. Making a new link
or changing the password stops the alerts that were turned on through the old one.

Two small files beside the config hold the state: ``alerts.json`` (who gets alerts and
where) and ``alerts-state.json`` (what was already said, so nothing repeats after a
restart). Both are git-ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import zoneinfo
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import webpush
from .config import AppConfig, AudienceCfg, Level, PersonCfg
from .events import Event
from .notify import Note, Notifier, panel_address
from .policy import VisibleEvent, _strip_tags, visible_events
from .render.week import fmt_day, fmt_time
from .sources import USER_AGENT

log = logging.getLogger("kalends.alerts")

FILE = "alerts.json"
STATE_FILE = "alerts-state.json"
VIEWER_CHANNELS = ("webpush", "email", "ntfy")
LEADS = {10: "10 minutes", 30: "30 minutes", 60: "1 hour", 1440: "1 day"}  # what people can pick
DAILY_CAP = 25  # alerts per person per day, so a busy day of edits can't flood anyone
MAX_SUBS = 20  # per link
LATE = timedelta(minutes=10)  # reminders this late (Kalends was off) are skipped
CONFIRM_DAYS = 3  # email addresses not confirmed by then are forgotten
MAX_LINES = 8
EMPTY_GRACE = timedelta(hours=24)  # an emptied calendar is believed after this long
REPO = "https://github.com/brandonbtm29/Kalends"


def fp(text: str | None) -> str:
    """A short fingerprint (to notice a new link or password without storing it)."""
    return hashlib.sha256((text or "").encode()).hexdigest()[:16]


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _dt(text: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _zone(name: str | None, fallback: ZoneInfo) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name and name in zoneinfo.available_timezones() else fallback
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return fallback


def when_text(start: datetime, end: datetime, all_day: bool, tz: ZoneInfo) -> str:
    """'Thu Nov 19, 7:00 PM – 9:00 PM', 'Thu Nov 19, all day', 'Thu Nov 19 – Sat Nov 21'."""
    if all_day:
        last = (end - timedelta(days=1)).date() if end.date() > start.date() else start.date()
        first = start.date()
        return f"{fmt_day(first)}, all day" if last == first else f"{fmt_day(first)} – {fmt_day(last)}"
    s, e = start.astimezone(tz), end.astimezone(tz)
    if s.date() == e.date():
        return f"{fmt_day(s.date())}, {fmt_time(s)} – {fmt_time(e)}"
    return f"{fmt_day(s.date())} {fmt_time(s)} – {fmt_day(e.date())} {fmt_time(e)}"


def clean_title(cfg: AppConfig, text: str) -> str:
    """An event's title for your own messages, without the #private / #hide tags."""
    return _strip_tags(text, [cfg.overrides.private_tag, cfg.overrides.hide_tag]) or "(no title)"


def lead_text(minutes: int) -> str:
    if minutes <= 0:
        return "Now"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return "Tomorrow" if days == 1 else f"In {days} days"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"In {hours} hour{'s' if hours > 1 else ''}"
    return f"In {minutes} min"


def device_label(user_agent: str) -> str:
    ua = user_agent or ""
    system = next(
        (
            name
            for key, name in (
                ("iPhone", "iPhone"),
                ("iPad", "iPad"),
                ("Android", "Android"),
                ("Macintosh", "Mac"),
                ("Windows", "Windows"),
                ("Linux", "Linux"),
            )
            if key in ua
        ),
        "Browser",
    )
    browser = next(
        (
            name
            for key, name in (
                ("Edg/", "Edge"),
                ("Firefox/", "Firefox"),
                ("Chrome/", "Chrome"),
                ("Safari/", "Safari"),
            )
            if key in ua
        ),
        "",
    )
    return f"{system} · {browser}" if browser else system


class _JsonFile:
    def __init__(self, path: Path | None, prefix: str):
        self.path = path
        self.prefix = prefix
        self.lock = threading.RLock()
        self.data: dict = {}
        if path and path.is_file():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                self.data = loaded if isinstance(loaded, dict) else {}
            except (OSError, ValueError):
                log.warning("couldn't read %s; starting fresh", path.name)

    def save(self) -> None:
        if self.path is None:
            return
        with self.lock:
            text = json.dumps(self.data, indent=1, sort_keys=True)
        try:
            fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.prefix, suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            if os.name != "nt":
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            log.warning("couldn't save %s: %s", self.path.name, exc)


class Subscriptions(_JsonFile):
    """Who gets alerts, and where: your browsers, and the people you share with."""

    def __init__(self, path: Path | None):
        super().__init__(path, ".alerts-")
        self.data.setdefault("subs", {})

    @property
    def subs(self) -> dict[str, dict]:
        return self.data["subs"]

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(s) for s in self.subs.values()]

    def get(self, sid: str) -> dict | None:
        with self.lock:
            s = self.subs.get(sid)
            return dict(s) if s else None

    def by_token(self, token: str) -> dict | None:
        found = None
        with self.lock:
            for s in self.subs.values():
                if hmac.compare_digest(str(s.get("token", "")).encode(), token.encode()):
                    found = dict(s)
        return found

    def add(self, sub: dict) -> dict:
        sub = dict(sub)
        sub.setdefault("id", secrets.token_hex(8))
        sub.setdefault("token", secrets.token_urlsafe(24))
        sub.setdefault("created", _iso(datetime.now(UTC)))
        with self.lock:
            self.subs[sub["id"]] = sub
        self.save()
        return dict(sub)

    def update(self, sid: str, **changes) -> dict | None:
        with self.lock:
            s = self.subs.get(sid)
            if s is None:
                return None
            s.update(changes)
            out = dict(s)
        self.save()
        return out

    def remove(self, sid: str) -> bool:
        with self.lock:
            gone = self.subs.pop(sid, None) is not None
        if gone:
            self.save()
        return gone

    def owner_devices(self) -> list[dict]:
        return [s for s in self.all() if s.get("who") == "owner" and s.get("channel") == "webpush"]

    def viewers(self, audience_id: str | None = None) -> list[dict]:
        return [
            s
            for s in self.all()
            if s.get("who") == "viewer" and (audience_id is None or s.get("audience") == audience_id)
        ]

    def by_endpoint(self, endpoint: str) -> dict | None:
        return next((s for s in self.all() if s.get("endpoint") == endpoint), None)


class AlertState(_JsonFile):
    """What was already said, so nothing repeats (or goes missing) across restarts."""

    def __init__(self, path: Path | None):
        super().__init__(path, ".alerts-state-")
        for key in ("reminded", "feeds", "snapshots", "activity"):
            self.data.setdefault(key, {})


class Alerts:
    """Works out what to tell whom, and sends it."""

    def __init__(
        self,
        cfg: Callable[[], AppConfig],
        kalends: Callable,
        notifier: Notifier,
        folder: Path | None = None,
        vapid_key: Callable[[], str] | None = None,
    ):
        self._cfg = cfg
        self._kalends = kalends  # returns the Kalends object (it is swapped on some changes)
        self.notifier = notifier
        self.subs = Subscriptions(folder / FILE if folder else None)
        self.state = AlertState(folder / STATE_FILE if folder else None)
        self._key = vapid_key
        self._memory_key: str | None = None
        self._lock = threading.Lock()
        self._upcoming: tuple[tuple, list[Event]] | None = None
        notifier.push = self.push_owner
        notifier.has_push = lambda: bool(self.subs.owner_devices())

    @property
    def cfg(self) -> AppConfig:
        return self._cfg()

    @property
    def g(self):
        return self._kalends()

    def now(self) -> datetime:
        return self.g.now()

    # -- keys and links ---------------------------------------------------------------------

    def vapid_private(self) -> str:
        if self._key is not None:
            return self._key()
        if self._memory_key is None:
            self._memory_key = webpush.new_private_key()
        return self._memory_key

    def vapid_public(self) -> str:
        return webpush.public_key(self.vapid_private())

    def contact(self) -> str:
        base = self.cfg.server.base_url.rstrip("/")
        return base if base.startswith("https://") else REPO

    def public_url(self, path: str) -> str:
        return f"{self.cfg.server.base_url.rstrip('/')}{path}"

    def manage_url(self, sub: dict) -> str:
        return self.public_url(f"/alerts/{sub['token']}")

    def ntfy_server(self) -> str:
        n = self.cfg.notify.ntfy
        return n.server if not n.token else "https://ntfy.sh"  # people can't use a private server's token

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=15, headers={"User-Agent": USER_AGENT})

    # -- your browsers ------------------------------------------------------------------------

    def add_owner_device(self, endpoint: str, p256dh: str, auth: str, user_agent: str) -> dict:
        old = self.subs.by_endpoint(endpoint)
        if old:
            self.subs.remove(old["id"])
        return self.subs.add(
            {
                "who": "owner",
                "channel": "webpush",
                "endpoint": endpoint,
                "p256dh": p256dh,
                "auth": auth,
                "label": device_label(user_agent),
            }
        )

    def push_owner(self, note: Note) -> str:
        devices = self.subs.owner_devices()
        if not devices:
            raise ValueError("no browsers have notifications turned on")
        msg = {
            "title": note.title,
            "body": note.body,
            "url": note.url or panel_address(self.cfg),
            "tag": note.kind,
        }
        ok = 0
        with self._client() as client:
            for d in devices:
                if self._push(client, d, msg, urgent=note.urgent):
                    ok += 1
        if not ok:
            raise ValueError("none of your browsers could be reached")
        return f"Sent to {ok} browser{'s' if ok != 1 else ''}"

    def _push(self, client: httpx.Client, sub: dict, msg: dict, urgent: bool = False) -> bool:
        try:
            res = webpush.send(
                client,
                sub,
                msg,
                self.vapid_private(),
                self.contact(),
                ttl=3600 if urgent else 24 * 3600,
                urgency="high" if urgent else "normal",
            )
        except Exception as exc:  # noqa: BLE001 - a bad subscription must not stop the others
            res = webpush.PushResult(False, False, type(exc).__name__)
        self._record(sub, res.ok, res.detail, gone=res.gone)
        return res.ok

    def _record(self, sub: dict, ok: bool, detail: str, gone: bool = False) -> None:
        if gone or (not ok and int(sub.get("failures", 0)) >= 9):
            # (no addresses or topics in the log)
            log.info("alerts: forgetting a %s subscription (%s)", sub.get("channel"), detail)
            self.subs.remove(sub["id"])
            return
        if ok:
            self.subs.update(sub["id"], last_ok=_iso(datetime.now(UTC)), failures=0, last_error=None)
        else:
            self.subs.update(sub["id"], failures=int(sub.get("failures", 0)) + 1, last_error=detail[:200])

    # -- people you share with -------------------------------------------------------------------

    @staticmethod
    def link_fingerprints(aud: AudienceCfg, person: PersonCfg | None) -> tuple[str, str]:
        """(link, password) fingerprints: a new link or password stops older alerts."""
        if person is not None:
            pw = person.password_hash or (aud.password_hash if person.use_audience_password else None)
            return fp(person.slug), fp(pw)
        return fp(aud.slug), fp(aud.password_hash)

    def valid_viewer(
        self, sub: dict, cfg: AppConfig | None = None, confirmed: bool = True
    ) -> tuple[str, AudienceCfg, PersonCfg | None] | None:
        """The audience a viewer's alerts follow, or None when they should stop (or wait)."""
        cfg = cfg or self.cfg
        pid = sub.get("person")
        person = None
        if pid:
            person = cfg.people.get(pid)
            if person is None or person.expired(self.g.today()):
                return None
            aid = person.audience
        else:
            aid = sub.get("audience") or ""
        aud = cfg.audiences.get(aid)
        if (
            aud is None
            or aud.self_view
            or not aud.alerts.enabled
            or sub.get("channel") not in aud.alerts.channels
        ):
            return None
        if person is None and not aud.shared_link:
            return None
        if (sub.get("link"), sub.get("pw")) != self.link_fingerprints(aud, person):
            return None
        if confirmed and sub.get("channel") == "email" and not sub.get("confirmed"):
            return None
        return aid, aud, person

    def subscribe(
        self,
        aid: str,
        aud: AudienceCfg,
        person_id: str | None,
        person: PersonCfg | None,
        channel: str,
        *,
        changes: bool,
        reminders: bool,
        lead: int,
        tz: str | None,
        label: str = "",
        page_url: str = "",
        **where: str,
    ) -> dict:
        link, pw = self.link_fingerprints(aud, person)
        if channel == "webpush":
            old = self.subs.by_endpoint(where.get("endpoint", ""))
            if old:
                self.subs.remove(old["id"])
        sub = {
            "who": "viewer",
            "channel": channel,
            "audience": aid,
            "person": person_id,
            "link": link,
            "pw": pw,
            "changes": bool(changes and aud.alerts.changes),
            "reminders": bool(reminders and aud.alerts.reminders),
            "lead": lead if lead in LEADS else 30,
            "tz": tz if tz and tz in zoneinfo.available_timezones() else None,
            "label": label or (device_label("") if channel == "webpush" else channel),
            "page": page_url,
            **where,
        }
        if channel == "email":
            sub["confirmed"] = False
        if channel == "ntfy":
            sub["topic"] = "kalends-" + secrets.token_urlsafe(18).replace("_", "x").replace("-", "y")
        sub = self.subs.add(sub)
        self._welcome(sub, aud)
        return self.subs.get(sub["id"]) or sub

    def _welcome(self, sub: dict, aud: AudienceCfg) -> None:
        what = []
        if sub.get("changes"):
            what.append("when plans change")
        if sub.get("reminders"):
            what.append(f"{LEADS.get(int(sub.get('lead') or 30), '30 minutes')} before events")
        about = " and ".join(what) or "nothing yet"
        if sub["channel"] == "email":
            body = (
                f"Someone asked for alerts about {aud.title} to be sent to this address ({about}).\n\n"
                f"To turn them on, open this link:\n{self.manage_url(sub)}/confirm\n\n"
                "If that wasn't you, ignore this email and nothing more will be sent."
            )
            self._send_sub(sub, f"Confirm alerts for {aud.title}", body, None, count=False)
        else:
            self._send_sub(sub, f"Alerts are on for {aud.title}", f"You'll hear {about}.", sub.get("page"))

    def confirm_email(self, sub: dict) -> dict | None:
        return self.subs.update(sub["id"], confirmed=True)

    def _send_sub(
        self, sub: dict, title: str, body: str, url: str | None, urgent: bool = False, count: bool = True
    ) -> bool:
        today = self.g.today().isoformat()
        if count:
            sent = int(sub.get("sent", 0)) if sub.get("day") == today else 0
            if sent >= DAILY_CAP:
                return False
            self.subs.update(sub["id"], day=today, sent=sent + 1)
        ch = sub.get("channel")
        try:
            if ch == "webpush":
                with self._client() as client:
                    return self._push(
                        client,
                        sub,
                        {"title": title, "body": body, "url": url or "", "tag": sub["id"]},
                        urgent=urgent,
                    )
            if ch == "email":
                stop = self.manage_url(sub)
                parts = [body, f"Open: {url}" if url else "", f"Change or stop these alerts: {stop}"]
                text = "\n\n".join(x for x in parts if x) + "\n"
                self.notifier.email_to(
                    sub["email"],
                    title,
                    text,
                    headers={"List-Unsubscribe": f"<{stop}>", "Auto-Submitted": "auto-generated"},
                )
            elif ch == "ntfy":
                note = Note(title=title, body=body, url=url, tags="calendar", urgent=urgent)
                self.notifier.ntfy_to(self.ntfy_server(), sub["topic"], note)
            else:
                return False
        except Exception as exc:  # noqa: BLE001
            self._record(sub, False, type(exc).__name__)
            log.warning("alert to %s failed: %s", ch, type(exc).__name__)
            return False
        self._record(sub, True, "Sent")
        return True

    def prune(self, cfg: AppConfig | None = None) -> int:
        """Stop alerts whose link, password or audience setting no longer allows them."""
        cfg = cfg or self.cfg
        now = datetime.now(UTC)
        gone = 0
        for s in self.subs.viewers():
            stale = (
                s.get("channel") == "email"
                and not s.get("confirmed")
                and (now - (_dt(s.get("created")) or now)) > timedelta(days=CONFIRM_DAYS)
            )
            if stale or self.valid_viewer(s, cfg, confirmed=False) is None:
                self.subs.remove(s["id"])
                gone += 1
        return gone

    def viewer_subs(self, audience_id: str) -> list[dict]:
        return [s for s in self.subs.viewers(audience_id) if self.valid_viewer(s) is not None]

    def count_for(self, audience_id: str, person_id: str | None) -> int:
        return sum(1 for s in self.subs.viewers(audience_id) if s.get("person") == person_id)

    # -- the owner's notes --------------------------------------------------------------------

    def tell_owner(
        self,
        kind: str,
        title: str,
        body: str,
        url: str | None = None,
        tags: str = "calendar",
        urgent: bool = False,
        wait: bool = True,
    ) -> None:
        self.notifier.owner(
            Note(title=title, body=body, url=url, tags=tags, kind=kind, urgent=urgent), wait=wait
        )

    def panel(self, path: str = "") -> str:
        return f"{panel_address(self.cfg)}{path}"

    # -- link activity --------------------------------------------------------------------------

    def link_opened(self, audience: AudienceCfg, person: PersonCfg | None, what: str = "page") -> None:
        a = self.cfg.notify.activity
        if not (a.enabled and a.first_open) or audience.self_view:
            return
        who = person.name if person else None
        if what == "feed":
            title = f"{who} subscribed to your calendar" if who else f"Someone subscribed to {audience.title}"
            body = f"{audience.title}, in a calendar app" + (f" (through {who}'s own link)" if who else "")
        else:
            title = f"{who} opened your calendar" if who else f"Someone opened {audience.title}"
            link = "their own link" if who else "its shared link"
            body = f"The first time {link} for {audience.title} was used."
        self.tell_owner(
            "activity", title, body, self.panel("/people" if who else "/sharing"), tags="eyes", wait=False
        )

    def lockout(self, audience: AudienceCfg, person: PersonCfg | None, key: str) -> None:
        a = self.cfg.notify.activity
        if not (a.enabled and a.lockouts):
            return
        now = self.now()
        with self.state.lock:
            last = _dt(self.state.data["activity"].get(f"lock:{key}"))
            if last and now - last < timedelta(hours=1):
                return
            self.state.data["activity"][f"lock:{key}"] = _iso(now)
        self.state.save()
        where = f"{person.name}'s link" if person else f"the shared link for {audience.title}"
        self.tell_owner(
            "activity",
            "Too many wrong passwords",
            f"Someone typed the wrong password 5 times on {where}. That device is locked out for 15 minutes.",
            self.panel("/sharing"),
            tags="warning",
            wait=False,
        )

    def check_expiring(self) -> None:
        a = self.cfg.notify.activity
        if not (a.enabled and a.expiring_days):
            return
        today = self.g.today()
        changed = False
        for pid, person in self.cfg.people.items():
            if not person.enabled or person.expires is None:
                continue
            left = (person.expires - today).days
            key = f"expiring:{pid}"
            if (
                0 <= left <= a.expiring_days
                and self.state.data["activity"].get(key) != person.expires.isoformat()
            ):
                when = "today" if left == 0 else "tomorrow" if left == 1 else f"on {fmt_day(person.expires)}"
                self.tell_owner(
                    "activity",
                    f"{person.name}'s link ends {when}",
                    "It stops working after that day. Change the end date on their page to keep it going.",
                    self.panel(f"/people/{pid}"),
                    tags="hourglass",
                )
                with self.state.lock:
                    self.state.data["activity"][key] = person.expires.isoformat()
                changed = True
        if changed:
            self.state.save()

    # -- calendar problems ----------------------------------------------------------------------

    def _counts(self) -> dict[str, int]:
        g = self.g
        today = g.today()
        tz = self.cfg.tz
        s = datetime.combine(today - timedelta(days=30), time.min, tzinfo=tz)
        e = datetime.combine(today + timedelta(days=120), time.min, tzinfo=tz)
        out: dict[str, int] = {}
        for ev in g.events_between(s, e):
            out[ev.calendar_id] = out.get(ev.calendar_id, 0) + 1
        return out

    def check_feeds(self) -> set[str]:
        """Note calendars that broke, emptied or recovered; returns the ones that look emptied."""
        cfg, g, now = self.cfg, self.g, self.now()
        p = cfg.notify.problems
        counts = self._counts()
        suspects: set[str] = set()
        feeds = self.state.data["feeds"]
        for cid, cal in cfg.calendars.items():
            if cal.local:
                continue
            st = g.cache.states.get(cid)
            if st is None or st.last_attempt is None:
                continue  # not tried yet since starting
            rec = feeds.setdefault(cid, {})
            status = rec.get("status")
            name = cal.name
            if st.last_error:
                since = _dt(rec.get("ok")) or _dt(rec.get("seen")) or now
                rec.setdefault("seen", _iso(now))
                if status != "failing" and now - since >= timedelta(minutes=p.after_minutes):
                    rec["status"] = "failing"
                    if p.enabled:
                        hours = max(1, round((now - since).total_seconds() / 3600))
                        span = f"{hours} hour{'s' if hours != 1 else ''}"
                        self.tell_owner(
                            "problems",
                            f"{name} isn't updating",
                            f"Kalends hasn't been able to download it for {span} ({st.last_error}). "
                            "Viewers still see the last copy.",
                            self.panel(f"/calendars/{cid}"),
                            tags="warning",
                        )
                continue
            rec["ok"] = _iso(now)
            rec.setdefault("seen", _iso(now))
            count = counts.get(cid, 0)
            before = int(rec.get("count", 0))
            if status == "failing":
                rec["status"] = "ok"
                if p.enabled:
                    self.tell_owner(
                        "problems",
                        f"{name} is updating again",
                        "It downloaded fine just now.",
                        self.panel("/calendars"),
                        tags="white_check_mark",
                    )
            if count == 0 and before >= 3 and rec.get("status") != "empty":
                rec["status"], rec["empty_since"] = "empty", _iso(now)
                if p.enabled:
                    self.tell_owner(
                        "problems",
                        f"{name} came back empty",
                        f"It downloaded, but with no events (it had {before}). If that's a mistake at the "
                        "calendar's end, viewers' pages will look empty too.",
                        self.panel(f"/calendars/{cid}"),
                        tags="warning",
                    )
            elif count > 0 and rec.get("status") == "empty":
                rec["status"] = "ok"
                rec.pop("empty_since", None)
                if p.enabled:
                    self.tell_owner(
                        "problems",
                        f"{name} has events again",
                        f"{count} events are back.",
                        self.panel("/calendars"),
                        tags="white_check_mark",
                    )
            elif rec.get("status") is None:
                rec["status"] = "ok"
            if rec.get("status") == "empty":
                since = _dt(rec.get("empty_since")) or now
                if now - since < EMPTY_GRACE:
                    suspects.add(cid)
            if count or rec.get("status") != "empty":
                rec["count"] = count
        for cid in list(feeds):
            if cid not in cfg.calendars:
                del feeds[cid]
        self.state.save()
        return suspects

    # -- changes --------------------------------------------------------------------------------

    def _window(self, days: int) -> tuple[datetime, datetime]:
        tz = self.cfg.tz
        today = self.g.today()
        return (
            datetime.combine(today, time.min, tzinfo=tz),
            datetime.combine(today + timedelta(days=days + 1), time.min, tzinfo=tz),
        )

    @staticmethod
    def _diff(
        old: dict, new: dict, lo: datetime, hi: datetime, pair
    ) -> list[tuple[str, dict | None, dict | None]]:
        def inside(r: dict) -> bool:
            s = _dt(r["s"])
            return s is not None and lo <= s < hi

        o = {k: r for k, r in old.items() if inside(r)}
        n = {k: r for k, r in new.items() if inside(r)}
        removed = sorted((o[k] for k in o.keys() - n.keys()), key=lambda r: r["s"])
        added = sorted((n[k] for k in n.keys() - o.keys()), key=lambda r: r["s"])
        out: list[tuple[str, dict | None, dict | None]] = []
        for r in list(removed):
            key = pair(r)
            match = next((a for a in added if key is not None and pair(a) == key), None)
            if match is not None:
                out.append(("moved", r, match))
                removed.remove(r)
                added.remove(match)
        out += [("added", None, a) for a in added] + [("cancelled", r, None) for r in removed]
        for k in o.keys() & n.keys():
            a, b = o[k], n[k]
            if (a["t"], a.get("l", ""), a["e"]) != (b["t"], b.get("l", ""), b["e"]):
                out.append(("changed", a, b))
        out.sort(key=lambda c: (c[2] or c[1])["s"])
        return out

    @staticmethod
    def _line(kind: str, a: dict | None, b: dict | None, tz: ZoneInfo, prefix: str = "") -> str:
        def w(r: dict) -> str:
            return when_text(_dt(r["s"]), _dt(r["e"]), bool(r.get("ad")), tz)

        if kind == "added":
            return f"{prefix}Added: {w(b)} · {b['t']}"
        if kind == "cancelled":
            return f"{prefix}Cancelled: {w(a)} · {a['t']}"
        if kind == "moved":
            return f"{prefix}Moved: {b['t']} → {w(b)} (was {w(a)})"
        if a["t"] != b["t"]:
            return f"{prefix}Renamed: {a['t']} → {b['t']} ({w(b)})"
        if a["e"] != b["e"]:
            return f"{prefix}Changed: {b['t']} now {w(b)}"
        return f"{prefix}New place: {b['t']} · {b.get('l') or 'none'} ({w(b)})"

    def _snapshot(self, key: str, records: dict, lo: datetime, hi: datetime) -> tuple[dict, datetime] | None:
        snaps = self.state.data["snapshots"]
        old = snaps.get(key)
        snaps[key] = {"end": _iso(hi), "events": records}
        if not old:
            return None
        return old.get("events") or {}, min(_dt(old.get("end")) or hi, hi)

    def check_changes(self, suspects: set[str]) -> None:
        cfg, g, now = self.cfg, self.g, self.now()
        ch = cfg.notify.changes
        snaps = self.state.data["snapshots"]
        if ch.calendars and not (set(ch.calendars) & suspects):
            lo, hi = self._window(ch.days_ahead)
            records = {}
            for ev in g.events_between(lo, hi):
                if ev.calendar_id in ch.calendars and ev.start > now:
                    records[f"{ev.calendar_id}|{ev.uid}|{ev.start.isoformat()}"] = {
                        "uid": ev.uid,
                        "cal": ev.calendar_id,
                        "s": ev.start.isoformat(),
                        "e": ev.end.isoformat(),
                        "t": clean_title(cfg, ev.summary),
                        "l": ev.location,
                        "ad": ev.all_day,
                    }
            prev = self._snapshot("owner:" + ",".join(sorted(ch.calendars)), records, lo, hi)
            if prev is not None:
                changes = self._diff(prev[0], records, now, prev[1], lambda r: (r["cal"], r["uid"]))
                if changes:
                    cals = {(c[2] or c[1])["cal"] for c in changes}
                    many = len(cals) > 1
                    lines = [
                        self._line(
                            k, a, b, cfg.tz, f"{cfg.calendars[(b or a)['cal']].name}: " if many else ""
                        )
                        for k, a, b in changes
                    ]
                    title = (
                        f"{len(changes)} changes on your calendars"
                        if many
                        else f"{'A change' if len(changes) == 1 else f'{len(changes)} changes'} on "
                        f"{cfg.calendars[next(iter(cals))].name}"
                    )
                    self.tell_owner("changes", title, _cap(lines), self.panel("/preview"), tags="pencil")
        for key in [k for k in snaps if k.startswith("owner:")]:
            if key != "owner:" + ",".join(sorted(ch.calendars)):
                del snaps[key]
        # The people you share with: what their audience can see.
        for aid, aud in cfg.audiences.items():
            key = f"aud:{aid}"
            subs = [s for s in self.viewer_subs(aid) if s.get("changes")] if aud.alerts.enabled else []
            if not subs:
                snaps.pop(key, None)
                continue
            if suspects:
                continue  # wait: an emptied calendar would look like everything was cancelled
            lo, hi = self._window(aud.alerts.days_ahead)
            vis = [v for v in g.view(aud, lo, hi) if v.start > now]
            records = {
                v.key: {
                    "s": v.start.isoformat(),
                    "e": v.end.isoformat(),
                    "t": v.title,
                    "l": v.location,
                    "ad": v.all_day,
                    "lv": v.level.value,
                }
                for v in vis
            }
            prev = self._snapshot(key, records, lo, hi)
            if prev is None:
                continue
            changes = self._diff(
                prev[0], records, now, prev[1], lambda r: r["t"] if r.get("lv") in ("full", "title") else None
            )
            if not changes:
                continue
            for sub in subs:
                tz = _zone(sub.get("tz"), cfg.tz)
                lines = [self._line(k, a, b, tz) for k, a, b in changes]
                n = len(changes)
                title = f"{aud.title}: {'a change' if n == 1 else f'{n} changes'}"
                self._send_sub(sub, title, _cap(lines), sub.get("page"))
        self.state.save()

    # -- reminders and the morning summary ------------------------------------------------------

    def upcoming(self, now: datetime) -> list[Event]:
        """Events from an hour ago to 26 hours ahead (worked out once an hour, or when calendars change)."""
        g = self.g
        hour = now.astimezone(self.cfg.tz).replace(minute=0, second=0, microsecond=0)
        key = (id(g.cfg), g.generation, g.cache.fingerprint(), g.school.version, hour)
        if self._upcoming is None or self._upcoming[0] != key:
            self._upcoming = (key, g.events_between(hour - timedelta(hours=1), hour + timedelta(hours=27)))
        return self._upcoming[1]

    def tick(self) -> None:
        """Run every half minute: send the reminders that are due, and the morning summary."""
        with self._lock:
            self._tick()

    def _tick(self) -> None:
        cfg, now = self.cfg, self.now()
        st = self.state.data
        last = _dt(st.get("last_tick")) or now - timedelta(seconds=60)
        last = max(last, now - LATE)
        if last >= now:
            return
        reminded: dict = st["reminded"]
        events = self.upcoming(now)
        r = cfg.notify.reminders
        if r.enabled:
            lead = timedelta(minutes=r.minutes)
            done: set[tuple] = set()
            for ev in events:
                if ev.all_day or ev.free or (r.calendars and ev.calendar_id not in r.calendars):
                    continue
                if not (last < ev.start - lead <= now) or (ev.summary, ev.start) in done:
                    continue
                key = f"o|{ev.uid}|{ev.start.isoformat()}"
                if key in reminded:
                    continue
                done.add((ev.summary, ev.start))
                reminded[key] = _iso(now)
                cal = cfg.calendars.get(ev.calendar_id)
                body = when_text(ev.start, ev.end, False, cfg.tz)
                body += f" · {ev.location}" if ev.location else ""
                body += f" · {cal.name}" if cal else ""
                self.tell_owner(
                    "reminders",
                    f"{lead_text(r.minutes)}: {clean_title(cfg, ev.summary)}",
                    body,
                    None,
                    tags="alarm_clock",
                    urgent=True,
                )
        # People you share with, about what their audience sees.
        by_aud: dict[str, list[dict]] = {}
        for s in self.subs.viewers():
            if s.get("reminders"):
                ok = self.valid_viewer(s)
                if ok is not None and ok[1].alerts.reminders:
                    by_aud.setdefault(ok[0], []).append(s)
        for aid, subs in by_aud.items():
            aud = cfg.audiences[aid]
            span_lo, span_hi = now - timedelta(hours=1), now + timedelta(hours=26)
            vis: list[VisibleEvent] = visible_events(cfg, aud, events, span_lo, span_hi)
            for sub in subs:
                lead = timedelta(minutes=int(sub.get("lead") or 30))
                tz = _zone(sub.get("tz"), cfg.tz)
                for v in vis:
                    if v.all_day or v.level is Level.BUSY:
                        continue
                    if not (last < v.start - lead <= now):
                        continue
                    key = f"{sub['id']}|{v.key}"
                    if key in reminded:
                        continue
                    reminded[key] = _iso(now)
                    body = f"{when_text(v.start, v.end, False, tz)} · {aud.title}"
                    self._send_sub(
                        sub,
                        f"{lead_text(int(sub.get('lead') or 30))}: {v.title}",
                        body,
                        sub.get("page"),
                        urgent=True,
                    )
        self._summary(now)
        cutoff = now - timedelta(days=3)
        st["reminded"] = {k: v for k, v in reminded.items() if (_dt(v) or now) > cutoff}
        st["last_tick"] = _iso(now)
        self.state.save()

    def _summary(self, now: datetime) -> None:
        cfg = self.cfg
        s = cfg.notify.summary
        if not s.enabled:
            return
        local = now.astimezone(cfg.tz)
        due = datetime.combine(local.date(), s.at, tzinfo=cfg.tz)
        if (
            not (due <= local < due + timedelta(hours=3))
            or self.state.data.get("summary_day") == local.date().isoformat()
        ):
            return
        self.state.data["summary_day"] = local.date().isoformat()
        lo = datetime.combine(local.date(), time.min, tzinfo=cfg.tz)
        hi = lo + timedelta(days=1)
        seen: set[tuple] = set()
        items: list[Event] = []
        for ev in self.g.events_between(lo, hi):
            if s.calendars and ev.calendar_id not in s.calendars:
                continue
            if (ev.summary, ev.start) in seen:
                continue
            seen.add((ev.summary, ev.start))
            items.append(ev)
        if not items and s.skip_empty:
            return
        items.sort(key=lambda e: (not e.all_day, e.start))
        lines = [
            ("All day" if ev.all_day else fmt_time(max(ev.start, lo))) + f" · {clean_title(cfg, ev.summary)}"
            for ev in items
        ]
        n = len(items)
        title = f"Today: {n} event{'s' if n != 1 else ''}" if n else "Today: nothing on your calendars"
        self.tell_owner("summary", title, _cap(lines, 14) or "Enjoy the free day.", None, tags="sunrise")

    # -- after each calendar refresh ------------------------------------------------------------

    def after_refresh(self) -> None:
        with self._lock:
            try:
                suspects = self.check_feeds()
                self.check_changes(suspects)
                self.check_expiring()
                self.prune()
            except Exception:
                log.exception("alerts check failed")


def _cap(lines: list[str], limit: int = MAX_LINES) -> str:
    if len(lines) <= limit:
        return "\n".join(lines)
    return "\n".join(lines[: limit - 1] + [f"…and {len(lines) - limit + 1} more"])


VALID_EMAIL = re.compile(r"^[^@\s<>\"',;]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}$")


def clean_email(text: str) -> str | None:
    text = (text or "").strip()
    return text if VALID_EMAIL.match(text) else None


def today_of(cfg: AppConfig) -> date:
    return datetime.now(cfg.tz).date()
