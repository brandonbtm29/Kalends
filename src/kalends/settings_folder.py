"""Where Kalends keeps its settings: ``config.yaml``, ``.env`` and the files beside them.

By default they sit beside the config path Kalends is started with, which is Kalends' own folder
(the installer, auto-start and the shortcuts all use ``<Kalends folder>/config.yaml``).

A pointer file, ``settings_location.json`` in that folder (gitignored, one per computer), can send
them somewhere else, such as a SyncThing folder: ``{"settings_dir": "~/SyncThing/Kalends/Settings"}``.
A relative path is relative to Kalends' folder. ``KALENDS_SETTINGS_DIR`` overrides the pointer. The
settings panel changes it under Settings > This computer > Settings folder; so does
``kalends settings-folder``. The pointer's name and format match the ``settings_location.py`` shared
by Brandon's lab apps; this module is Kalends' own (a web app, no Qt) and is not one of the shared,
byte-identical files.

Everything Kalends finds through the config path follows it: ``state.json``, meeting requests, video
sign-ins, school and alert state, Kalends calendars, the feed cache, logs and update backups. The
pointer is read each time Kalends starts, so auto-start and the shortcuts keep Kalends' own config
path and pick up a change after a restart.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

POINTER = "settings_location.json"
ENV_VAR = "KALENDS_SETTINGS_DIR"
# Files kept beside config.yaml (besides config.yaml and .env). updates.backup_settings saves these too.
FILES = (
    "state.json",
    "requests.json",
    "video-accounts.json",
    "school-state.json",
    "alerts.json",
    "alerts-state.json",
)
# Folders that move with them: Kalends' own calendars, and the feed cache (so the last good copy of
# each calendar is there from the first minute).
FOLDERS = ("local-calendars", "cache")

_started_with: Path | None = None


def read_pointer(app_dir: Path) -> str:
    try:
        data = json.loads((app_dir / POINTER).read_text(encoding="utf-8"))
        return str(data.get("settings_dir") or "").strip()
    except (OSError, ValueError, AttributeError):
        return ""


def expand(value: str, app_dir: Path) -> Path:
    path = Path(os.path.expanduser(value))
    return Path(os.path.normpath(path if path.is_absolute() else app_dir / path))


def settings_dir(app_config: Path) -> Path:
    """Folder holding the settings for Kalends started with ``app_config``."""
    app_dir = Path(app_config).resolve().parent
    value = os.environ.get(ENV_VAR) or read_pointer(app_dir)
    return expand(value, app_dir) if value else app_dir


def is_default(app_config: Path) -> bool:
    return settings_dir(app_config) == Path(app_config).resolve().parent


def resolve_config(config: Path) -> Path:
    """The config.yaml to use for a ``-c`` path given on the command line.

    Also remembers ``config`` as the path Kalends was started with (see :func:`app_config`).
    """
    global _started_with
    config = Path(config)
    _started_with = config.resolve()
    if is_default(config):
        return config
    return settings_dir(config) / config.name


def app_config() -> Path:
    """The config path Kalends was started with: what auto-start, shortcuts and restarts use."""
    if _started_with is not None:
        return _started_with
    from .updates import PROJECT_ROOT

    return PROJECT_ROOT / "config.yaml"


def portable(path: Path) -> str:
    """``~/...`` under the home folder (works on every computer), else the absolute path."""
    path = Path(os.path.normpath(path.resolve()))
    try:
        return "~/" + path.relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(path)


@dataclass
class Moved:
    folder: Path
    copied: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    backup: Path | None = None


def _copy(src: Path, dst: Path) -> None:
    """Copy a file or folder so a failure never leaves a half-written one at ``dst``."""
    tmp = dst.with_name(f".{dst.name}.copying")
    if tmp.is_dir():
        shutil.rmtree(tmp)
    if src.is_dir():
        shutil.copytree(src, tmp)
    else:
        shutil.copy2(src, tmp)
    os.replace(tmp, dst)


def change(app_config: Path, new_folder: str | Path | None) -> Moved:
    """Point Kalends at ``new_folder`` (empty = back to Kalends' own folder).

    Settings missing in the new folder are copied from the current one, and nothing is deleted.
    Moving to another folder keeps the settings already there (so a second computer picks up
    synced settings instead of overwriting them). Moving back to Kalends' own folder is the
    opposite: the copies there are the ones from before the move, so the settings in use now
    replace them, after the old ones are saved to ``backups/``. Takes effect when Kalends restarts.
    """
    if os.environ.get(ENV_VAR):
        raise ValueError(f"The {ENV_VAR} environment variable sets the folder, so it can't be changed here.")
    app_config = Path(app_config).resolve()
    app_dir = app_config.parent
    old = settings_dir(app_config)
    if new_folder and str(new_folder).strip():
        target = expand(str(new_folder).strip(), app_dir)
    else:
        target = app_dir
    value = "" if target == app_dir else portable(target)
    overwrite = target == app_dir and old != app_dir
    target.mkdir(parents=True, exist_ok=True)
    moved = Moved(target)
    names = (app_config.name, ".env", *FILES, *FOLDERS)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    for name in names:
        src, dst = old / name, target / name
        if not src.exists() or src.resolve() == dst.resolve():
            continue
        if dst.exists() and not overwrite:
            moved.kept.append(name)
            continue
        if dst.exists():  # back to Kalends' own folder: save the old copy, then use the current one
            if name != "cache":  # the cache is only copies of the calendars: not worth keeping
                moved.backup = target / "backups" / f"before-settings-folder-{stamp}"
                moved.backup.mkdir(parents=True, exist_ok=True)
                _copy(dst, moved.backup / name)
            if dst.is_dir():
                shutil.rmtree(dst)
            moved.replaced.append(name)
        else:
            moved.copied.append(name)
        _copy(src, dst)
    env = target / ".env"
    if env.exists() and os.name != "nt":
        env.chmod(0o600)  # it holds the admin password hash, the server secret and the calendar links
    if moved.backup is not None and os.name != "nt":
        os.chmod(moved.backup, 0o700)
    # Written last: if a copy fails, Kalends keeps using the folder it had.
    (app_dir / POINTER).write_text(json.dumps({"settings_dir": value}, indent=2) + "\n", encoding="utf-8")
    return moved
