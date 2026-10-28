"""The settings panel's Diagnostics section: a bug report that is safe to paste.

``diagnostics.report()`` (shared with Brandon's other apps, keep it byte-identical) lists the
version, install, OS, Python, folders and the end of the log, shortens the home folder to ~ and
blanks anything that looks like ``password=...`` or a login inside a link. Glimpse's own secrets
don't look like that: a calendar feed link or a share-link slug is just a long address or word.
So after the report is made, every secret Glimpse knows about is replaced with ``***`` by value:
everything in ``.env``, every feed link, and every slug, password hash, key and token in the
settings, in each spelling it could appear in.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import __version__, diagnostics
from .config import AppConfig, read_env_file
from .sources import normalize_url
from .updates import PROJECT_ROOT
from .web.auth import feed_token
from .web.viewer import person_viewer

MASK = "***"
# Shorter .env values are settings such as a port or "true", not secrets, and blanking them
# everywhere would garble the report. Every secret Glimpse writes itself is 16+ characters.
MIN_SECRET_LENGTH = 6


def log_file(config_path: Path) -> Path:
    """Where `glimpse service` (auto-start) writes its log: logs/glimpse.log beside config.yaml."""
    return config_path.resolve().parent / "logs" / "glimpse.log"


def secret_values(cfg: AppConfig | None, env_values: Iterable[str] = ()) -> set[str]:
    """Every secret Glimpse knows about: ``.env`` values plus the secrets in the live settings.

    The settings part also covers secrets written straight into config.yaml or set as real
    environment variables instead of in ``.env``.
    """
    found = set(env_values)
    if cfg is not None:
        found.update((cfg.server.secret_key, cfg.admin.password_hash, cfg.admin.setup_token))
        found.update(cal.url for cal in cfg.calendars.values())
        for aid, aud in cfg.audiences.items():
            found.update((aud.slug, aud.password_hash, feed_token(cfg, aid)))
        for pid, person in cfg.people.items():
            found.update((person.slug, person.password_hash, feed_token(cfg, person_viewer(cfg, pid))))
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


def diagnostics_text(cfg: AppConfig, config_path: Path, env_path: Path, feed_problems: int = 0) -> str:
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
        "Glimpse",
        __version__,
        str(PROJECT_ROOT),
        settings_dir=str(config_path.resolve().parent),
        log_path=str(log_file(config_path)),
        extra=extra,
    )
    return scrub(text, secret_values(cfg, read_env_file(env_path).values()))
