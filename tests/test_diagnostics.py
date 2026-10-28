"""diagnostics: the Copy Diagnostics text and the Open Log Folder helper."""

import os

from kalends import diagnostics


def test_redact_blanks_secrets_and_home():
    home = os.path.expanduser("~")
    text = (
        f"settings in {home}/app\n"
        'password=hunter2 token: abc123 "api_key": "k-999"\n'
        "Authorization: Bearer abcdefghijklmnop\n"
        "https://user:pa55@example.com/feed.ics"
    )
    out = diagnostics.redact(text)
    for secret in ("hunter2", "abc123", "k-999", "abcdefghijklmnop", "pa55", home):
        assert secret not in out
    assert "~/app" in out
    assert "password=***" in out


def test_tail(tmp_path):
    log = tmp_path / "app.log"
    log.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
    assert diagnostics.tail(str(log), 3).splitlines() == ["line 197", "line 198", "line 199"]
    assert diagnostics.tail(str(tmp_path / "missing.log")) == ""
    assert diagnostics.tail(None) == ""


def test_git_commit_reads_refs(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs" / "heads" / "main").write_text("0123456789abcdef\n")
    assert diagnostics.git_commit(str(tmp_path)) == ("0123456", "main")

    (git / "refs" / "heads" / "main").unlink()
    (git / "packed-refs").write_text("# pack-refs\nfedcba9876543210 refs/heads/main\n")
    assert diagnostics.git_commit(str(tmp_path)) == ("fedcba9", "main")

    (git / "HEAD").write_text("aaaaaaaaaaaaaaaa\n")  # detached at a release tag
    assert diagnostics.git_commit(str(tmp_path)) == ("aaaaaaa", None)


def test_not_a_git_copy(tmp_path):
    assert diagnostics.git_commit(str(tmp_path)) == (None, None)
    assert diagnostics.local_changes(str(tmp_path)) == "unknown"


def test_report(tmp_path):
    log = tmp_path / "logs" / "app.log"
    log.parent.mkdir()
    log.write_text("INFO started\nERROR token=s3cret failed\n", encoding="utf-8")
    text = diagnostics.report(
        "Test App",
        "1.2.3",
        str(tmp_path),
        settings_dir=str(tmp_path / "Settings"),
        log_path=str(log),
        extra={"Serial port": "COM3"},
    )
    assert text.startswith("Test App 1.2.3")
    assert "folder without git" in text
    assert "Serial port: COM3" in text
    assert "ERROR token=*** failed" in text
    assert "s3cret" not in text
    assert diagnostics.log_folder(str(log)) == str(log.parent)


def test_open_folder(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(diagnostics.subprocess, "Popen", lambda cmd, **kw: calls.append(cmd))
    if hasattr(diagnostics.os, "startfile"):
        monkeypatch.setattr(diagnostics.os, "startfile", lambda p: calls.append(["startfile", p]))
    target = tmp_path / "new" / "logs"
    diagnostics.open_folder(str(target))
    assert target.is_dir()
    assert calls and calls[0][-1] == str(target)
