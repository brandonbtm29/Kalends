"""The settings panel's Diagnostics tab: a bug report that is safe to paste.

``diagnostics.report()`` (shared with Brandon's other apps, keep it byte-identical) lists the
version, install, OS, Python, folders and the end of the log, shortens the home folder to ~ and
blanks anything that looks like ``password=...`` or a login inside a link. Kalends' own secrets
don't look like that: a calendar feed link, a share-link slug or a Discord webhook is just a long
address or word. So after the report is made, every secret Kalends knows about is replaced with
``***`` by value: everything in ``.env``, every feed link, every slug, password hash, key and
token in the settings, the video-call sign-ins and each meeting request's private link, in each
spelling it could appear in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote, urlsplit

from . import __version__, conferencing, diagnostics
from .config import AppConfig, read_env_file
from .sources import normalize_url
from .updates import PROJECT_ROOT
from .web.auth import feed_token
from .web.viewer import person_viewer

if TYPE_CHECKING:
    from .web.runtime import Runtime

MASK = "***"
# Shorter .env values are settings such as a port or "true", not secrets, and blanking them
# everywhere would garble the report. Every secret Kalends writes itself is 16+ characters.
MIN_SECRET_LENGTH = 6


def log_file(config_path: Path) -> Path:
    """Where `kalends service` (auto-start) writes its log: logs/kalends.log beside config.yaml."""
    return config_path.resolve().parent / "logs" / "kalends.log"


def secret_values(
    cfg: AppConfig | None, env_values: Iterable[str | None] = (), kept: Iterable[str | None] = ()
) -> set[str]:
    """Every secret Kalends knows about: ``.env`` values, the secrets in the live settings, and
    ``kept`` ones stored elsewhere (see :func:`kept_secrets`).

    The settings part also covers secrets written straight into config.yaml or set as real
    environment variables instead of in ``.env``.
    """
    found = set(env_values) | set(kept)
    if cfg is not None:
        found.update((cfg.server.secret_key, cfg.admin.password_hash, cfg.admin.setup_token))
        for cal in cfg.calendars.values():
            found.update((cal.url, cal.share_slug))
        for aid, aud in cfg.audiences.items():
            found.update((aud.slug, aud.password_hash, feed_token(cfg, aid), aud.widget.slug))
            found.update((aud.requests.booking_slug, aud.requests.meeting_link))
        for pid, person in cfg.people.items():
            found.update((person.slug, person.password_hash, feed_token(cfg, person_viewer(cfg, pid))))
        n = cfg.notify
        found.update((n.ntfy.topic, n.ntfy.token, n.email.password, n.discord.webhook_url))
        v = cfg.video_calls
        found.update((v.zoom.account_id, v.zoom.client_id, v.zoom.client_secret, v.teams.client_id))
        found.update((v.google.client_id, v.google.client_secret, v.webex.client_id, v.webex.client_secret))
        found.add(cfg.school.calendar_url)
    return {s.strip() for s in found if s and len(s.strip()) >= MIN_SECRET_LENGTH}


def kept_secrets(rt: Runtime) -> list[str]:
    """Secrets kept outside the settings: video-call sign-ins (video-accounts.json) and each
    meeting request's private status link and meeting link (requests.json)."""
    found: list[str] = []
    video = rt.kalends.video
    for platform in conferencing.PLATFORMS:
        found += [str(v) for k, v in video.accounts.get(platform).items() if k.endswith("token") and v]
    if video.pending_teams:  # a Teams sign-in that hasn't finished yet
        found.append(str(video.pending_teams.get("device_code") or ""))
    for r in rt.requests.all():
        found += [r.token, r.link, r.meeting_id]  # a Jitsi room name is the meeting's only lock
    return found


def _spellings(secret: str) -> set[str]:
    """The ways a secret can show up in a log line, and in the report after redact()."""
    out = {secret, unquote(secret)}
    url = normalize_url(secret)
    try:
        parts = urlsplit(url)
    except ValueError:  # e.g. "http://[x": not a link, the value itself is still blanked
        parts = None
    if parts and parts.scheme and parts.netloc:
        out.add(url)
        out.add(url.split("://", 1)[1])  # so webcal://, https:// and http:// all match
        out.add(parts.path + (f"?{parts.query}" if parts.query else ""))  # a request line has no host
    out |= {diagnostics.redact(s) for s in out}
    return {s for s in out if len(s) >= MIN_SECRET_LENGTH}


def scrub(text: str, secrets: Iterable[str]) -> str:
    """Replace every spelling of every secret in ``text`` with ``***``."""
    spellings = set().union(*(_spellings(s) for s in secrets))
    if not spellings:
        return text
    # Longest first, so a feed link is blanked whole rather than around a shorter secret inside it.
    pattern = "|".join(re.escape(s) for s in sorted(spellings, key=len, reverse=True))
    return re.sub(pattern, MASK, text, flags=re.IGNORECASE)


def _count(n: int, thing: str) -> str:
    return f"{n} {thing}" + ("" if n == 1 else "s")


def _extra(rt: Runtime) -> dict[str, object]:
    """Counts and on/off switches only: no names, addresses or links."""
    c = rt.cfg
    states = rt.kalends.cache.states
    problems = sum(1 for cid in c.calendars if (st := states.get(cid)) and st.last_error)
    local = sum(1 for cal in c.calendars.values() if cal.local)
    shared = list(c.shared_audiences.values())
    video = rt.kalends.video
    video_on = [p for p in conferencing.PLATFORMS if video.status(p) != "off"]
    n = c.notify
    return {
        "Settings format": c.config_version,
        "Ports": f"public pages {c.server.port}, settings panel {c.admin.port}",
        "Calendars": f"{len(c.calendars)} ({len(c.calendars) - local} connected, "
        f"{_count(local, 'Kalends calendar')}; {problems} with a problem)",
        "Groups": len(c.groups),
        "Audiences": f"{len(shared)}" + (" (plus My calendar)" if c.self_audience else ""),
        "People": len(c.people),
        "Privacy rules": f"{len(c.rules)} ({sum(r.enabled for r in c.rules)} on)",
        "Meeting requests": f"on for {_count(sum(a.requests.enabled for a in shared), 'audience')}, "
        f"{_count(sum(a.requests.booking for a in shared), 'booking page')}, "
        f"{rt.requests.pending_count()} waiting",
        "Website widgets": sum(a.widget.enabled for a in shared),
        "Video calls": f"{len(video_on)} on ({sum(video.status(p) == 'ready' for p in video_on)} ready)",
        "Notifications": f"{sum((n.ntfy.enabled, n.email.enabled, n.discord.enabled))} on",
        "School mode": "on" if c.school.enabled else "off",
        "Update check": "on" if c.updates.check else "off",
    }


def diagnostics_text(rt: Runtime) -> str:
    """The text the Diagnostics tab shows and copies. Runs ``git status``: call off the event loop."""
    store = rt.store
    if store is None:
        raise ValueError("diagnostics need a ConfigStore")
    text = diagnostics.report(
        "Kalends",
        __version__,
        str(PROJECT_ROOT),
        settings_dir=str(store.config_path.resolve().parent),
        log_path=str(log_file(store.config_path)),
        extra=_extra(rt),
    )
    env = read_env_file(store.env_path)
    return scrub(text, secret_values(rt.cfg, env.values(), kept_secrets(rt)))
