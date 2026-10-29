"""The settings panel's Diagnostics section: a bug report that is safe to paste.

``diagnostics.report()`` (shared with Brandon's other apps, keep it byte-identical) lists the
version, install, OS, Python, folders and the end of the log, shortens the home folder to ~ and
blanks anything that looks like ``password=...`` or a login inside a link. Kalends' own secrets
don't look like that: a calendar feed link or a share-link slug is just a long address or word.
So after the report is made, every secret Kalends knows about is replaced with ``***`` by value:
everything in ``.env``, every feed link, slug, password hash, key and token in the settings,
the notification and video-call logins, the video-call sign-in tokens, an unfinished Teams
sign-in code and each meeting request's private status link, meeting link and meeting ID, in
each spelling it could appear in.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import __version__, conferencing, diagnostics
from .config import AppConfig, read_env_file
from .sources import normalize_url
from .updates import PROJECT_ROOT
from .web.auth import feed_token
from .web.viewer import person_viewer

MASK = "***"
# Shorter .env values are settings such as a port or "true", not secrets, and blanking them
# everywhere would garble the report. Every secret Kalends writes itself is 16+ characters.
MIN_SECRET_LENGTH = 6


def log_file(config_path: Path) -> Path:
    """Where `kalends service` (auto-start) writes its log: logs/kalends.log beside config.yaml."""
    return config_path.resolve().parent / "logs" / "kalends.log"


def _config_secrets(cfg: AppConfig) -> list[str | None]:
    """Every secret in the live settings, including ones written straight into config.yaml or
    set as real environment variables instead of in ``.env``."""
    found: list[str | None] = [cfg.server.secret_key, cfg.admin.password_hash, cfg.admin.setup_token]
    for cal in cfg.calendars.values():
        found += [cal.url, cal.share_slug]
    for aid, aud in cfg.audiences.items():
        found += [aud.slug, aud.password_hash, feed_token(cfg, aid)]
        found += [aud.requests.booking_slug, aud.requests.meeting_link, aud.widget.slug]
    for pid, person in cfg.people.items():
        found += [person.slug, person.password_hash, feed_token(cfg, person_viewer(cfg, pid))]
    n = cfg.notify
    found += [n.ntfy.topic, n.ntfy.token, n.email.password, n.discord.webhook_url]
    v = cfg.video_calls
    found += [v.zoom.account_id, v.zoom.client_id, v.zoom.client_secret]
    found += [v.google.client_id, v.google.client_secret, v.teams.client_id]
    found += [v.webex.client_id, v.webex.client_secret]
    found.append(cfg.school.calendar_url)
    return found


def _strings(value) -> Iterable[str]:
    """Every string inside a JSON value (the video-call sign-in file holds nested accounts)."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def video_tokens(config_path: Path) -> list[str]:
    """The sign-in tokens in video-accounts.json beside the config (missing or unreadable: none)."""
    try:
        data = json.loads((config_path.resolve().parent / conferencing.FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return list(_strings(data))


def private_values(requests: Iterable, video=None) -> list[str]:
    """Secrets that live outside the settings: each meeting request's private status token, its
    meeting link and the meeting's ID (for a Jitsi room the name is its only lock), and the code
    of an unfinished Teams sign-in."""
    found: list[str] = []
    for r in requests:
        found += [r.token, r.link, r.meeting_id]
    pending = getattr(video, "pending_teams", None)
    if pending:
        found.append(pending.get("device_code") or "")
    return [v for v in found if v]


def secret_values(
    cfg: AppConfig | None, env_values: Iterable[str] = (), others: Iterable[str | None] = ()
) -> set[str]:
    """Every secret Kalends knows about: ``.env`` values, the secrets in the live settings and
    ``others`` (sign-in tokens, request status links)."""
    found: list[str | None] = [*env_values, *others]
    if cfg is not None:
        found += _config_secrets(cfg)
    return {v.strip() for v in found if v and len(v.strip()) >= MIN_SECRET_LENGTH}


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


def diagnostics_text(
    cfg: AppConfig,
    config_path: Path,
    env_path: Path,
    feed_problems: int = 0,
    request_tokens: Iterable[str] = (),
) -> str:
    """The text the Diagnostics section shows and copies. Runs ``git status``: call off the event loop."""
    extra = {
        "Settings format": cfg.config_version,
        "Ports": f"public pages {cfg.server.port}, settings panel {cfg.admin.port}",
        "Calendars": f"{len(cfg.calendars)} ({feed_problems} with a problem)",
        "Audiences": len(cfg.audiences),
        "People": len(cfg.people),
        "Privacy rules": f"{len(cfg.rules)} ({sum(r.enabled for r in cfg.rules)} on)",
        "Update check": "on" if cfg.updates.check else "off",
    }
    text = diagnostics.report(
        "Kalends",
        __version__,
        str(PROJECT_ROOT),
        settings_dir=str(config_path.resolve().parent),
        log_path=str(log_file(config_path)),
        extra=extra,
    )
    others = [*video_tokens(config_path), *request_tokens]
    return scrub(text, secret_values(cfg, read_env_file(env_path).values(), others))
