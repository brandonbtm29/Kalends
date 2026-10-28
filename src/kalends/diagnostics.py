"""Bug-report helpers behind the "Open Log Folder" and "Copy Diagnostics" buttons.

Shared by Brandon's apps; keep every copy byte-identical (like crash_log.py and
settings_location.py). Standard library only, no Qt, so it can be tested on its own.

    text = diagnostics.report("Jebao Pump Sequencer", __version__, APP_DIR,
                              settings_dir=SETTINGS_DIR, log_path=crash_log.log_path())
    QApplication.clipboard().setText(text)
    diagnostics.open_folder(diagnostics.log_folder(crash_log.log_path()))

The report lists the app version and git commit, the operating system, Python and Qt
versions, where the app, its settings, data and log are, and the end of the log. The home
folder is shortened to "~" and anything that looks like a password, token or key is
blanked, so the text is safe to paste into a GitHub issue or an email.
"""

import os
import platform
import re
import subprocess
import sys
import time

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flashes up
_SECRET = re.compile(
    r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret|"
    r"authorization|cookie|session[_-]?id)(\"?\s*[=:]\s*\"?)([^\s\",;&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_URL_LOGIN = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")


def redact(text):
    """Shorten the home folder to ~ and blank anything that looks like a secret."""
    home = os.path.expanduser("~")
    if home and home not in ("~", os.sep):
        for variant in {home, home.replace("\\", "/"), home.replace("/", "\\")}:
            text = text.replace(variant, "~")
    text = _BEARER.sub(lambda m: f"{m.group(1)} ***", text)
    text = _SECRET.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    return _URL_LOGIN.sub(r"\1***@", text)


def tail(path, lines=50, max_bytes=256_000):
    """The last ``lines`` lines of a text file ("" if it can't be read)."""
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _git_dir(app_dir):
    path = os.path.join(app_dir, ".git")
    if os.path.isfile(path):  # worktree or submodule: ".git" is a file pointing elsewhere
        try:
            with open(path, encoding="utf-8") as f:
                line = f.read().strip()
        except OSError:
            return None
        if line.startswith("gitdir:"):
            target = line.split(":", 1)[1].strip()
            return target if os.path.isabs(target) else os.path.normpath(os.path.join(app_dir, target))
        return None
    return path if os.path.isdir(path) else None


def git_commit(app_dir):
    """(short commit, branch) of a git copy, read straight from .git; (None, None) if not one.

    Doesn't run git, so it is fast, never shows a console window and never leaves lock files.
    """
    gdir = _git_dir(app_dir)
    if not gdir:
        return None, None
    try:
        with open(os.path.join(gdir, "HEAD"), encoding="utf-8") as f:
            head = f.read().strip()
    except OSError:
        return None, None
    if not head.startswith("ref:"):
        return head[:7] or None, None  # detached HEAD (e.g. checked out at a release tag)
    ref = head.split(":", 1)[1].strip()
    branch = ref.rsplit("/", 1)[-1] if ref.startswith("refs/heads/") else ref
    commit_dirs = [gdir]
    try:  # worktrees keep their refs in the main repository ("commondir")
        with open(os.path.join(gdir, "commondir"), encoding="utf-8") as f:
            common = f.read().strip()
        commit_dirs.append(common if os.path.isabs(common) else os.path.normpath(os.path.join(gdir, common)))
    except OSError:
        pass
    for d in commit_dirs:
        try:
            with open(os.path.join(d, *ref.split("/")), encoding="utf-8") as f:
                return f.read().strip()[:7], branch
        except OSError:
            pass
        try:
            with open(os.path.join(d, "packed-refs"), encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) == 2 and parts[1] == ref:
                        return parts[0][:7], branch
        except OSError:
            pass
    return None, branch


def local_changes(app_dir, timeout=5):
    """'no', 'yes (N files)', or 'unknown': have the app's own tracked files been edited?"""
    if not _git_dir(app_dir):
        return "unknown"
    try:
        result = subprocess.run(  # noqa: S603 - fixed command, no shell
            ["git", "--no-optional-locks", "status", "--porcelain", "--untracked-files=no"],  # noqa: S607
            cwd=app_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            creationflags=_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    changed = [line for line in result.stdout.splitlines() if line.strip()]
    return f"yes ({len(changed)} files)" if changed else "no"


def _os_name():
    if sys.platform == "darwin":
        release = platform.mac_ver()[0]
        return f"macOS {release} ({platform.machine()})" if release else platform.platform()
    if sys.platform == "win32":
        return f"Windows {platform.release()} ({platform.version()}, {platform.machine()})"
    return platform.platform()


def _qt_versions():
    core = sys.modules.get("PyQt6.QtCore")  # only if the app already loaded Qt
    if core is None:
        return None
    return f"Qt {core.QT_VERSION_STR}, PyQt {core.PYQT_VERSION_STR}"


def log_folder(log_path):
    """The folder holding ``log_path`` (or None)."""
    return os.path.dirname(log_path) if log_path else None


def report(
    app_name,
    version,
    app_dir,
    *,
    settings_dir=None,
    data_dir=None,
    log_path=None,
    extra=None,
    log_lines=50,
):
    """Plain-text diagnostics for a bug report, already redacted. ``extra`` is a dict of more lines."""
    lines = [f"{app_name} {version}".strip()]
    if getattr(sys, "frozen", False):
        lines.append("Install: standalone build")
    else:
        commit, branch = git_commit(app_dir)
        if commit or branch:
            where = f" ({branch})" if branch else ""
            changes = local_changes(app_dir)
            lines.append(f"Install: git copy, commit {commit or '?'}{where}, local changes: {changes}")
        else:
            lines.append("Install: folder without git (ZIP download?)")
    lines.append(f"OS: {_os_name()}")
    qt = _qt_versions()
    lines.append(f"Python: {platform.python_version()} ({sys.executable})" + (f" · {qt}" if qt else ""))
    lines.append(f"App folder: {app_dir}")
    if settings_dir:
        lines.append(f"Settings folder: {settings_dir}")
    if data_dir:
        lines.append(f"Data folder: {data_dir}")
    lines.append(f"Log file: {log_path or 'none'}")
    for label, value in (extra or {}).items():
        lines.append(f"{label}: {value}")
    lines.append(f"Report made: {time.strftime('%Y-%m-%d %H:%M:%S %z')}")
    log_text = tail(log_path, log_lines)
    if log_text:
        lines += ["", f"Last {log_lines} log lines:", "```", log_text, "```"]
    return redact("\n".join(lines))


def open_folder(path):
    """Show a folder in Explorer / Finder / the file manager (creating it if needed).

    Raises OSError with a readable message if it can't be opened.
    """
    if not path:
        raise OSError("There is no folder to open yet.")
    os.makedirs(path, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 - opens a folder the app itself chose
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path], stdin=subprocess.DEVNULL)  # noqa: S603, S607
    else:
        subprocess.Popen(["xdg-open", path], stdin=subprocess.DEVNULL)  # noqa: S603, S607
