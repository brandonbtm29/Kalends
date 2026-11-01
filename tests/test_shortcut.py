"""The Kalends shortcut: a button that opens the settings panel, offered by the installers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kalends import autostart, cli

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def scripts(tmp_path, monkeypatch):
    d = tmp_path / "venv" / "Scripts"
    d.mkdir(parents=True)
    for exe in ("kalends-open.exe", "kalends-open", "kalends-service.exe"):
        (d / exe).write_text("")
    monkeypatch.setattr(autostart, "_scripts_dir", lambda: d)
    return d


@pytest.fixture
def config(tmp_path):
    folder = tmp_path / "My Kalends"
    folder.mkdir()
    cfg = folder / "config.yaml"
    cfg.write_text("x")
    return cfg


@pytest.fixture
def windows(tmp_path, monkeypatch, scripts):
    desktop = tmp_path / "OneDrive" / "Desktop"
    made = []

    def shortcut(lnk, target, args, workdir, description, icon=""):
        lnk.parent.mkdir(parents=True, exist_ok=True)
        lnk.write_text(f"{target} {args}")
        made.append({"lnk": lnk, "target": target, "args": args, "workdir": workdir, "icon": icon})

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))  # keep the Start menu entry inside tmp_path
    monkeypatch.setattr(autostart, "_win_desktop_dir", lambda: desktop)
    monkeypatch.setattr(autostart, "make_windows_shortcut", shortcut)
    return desktop, made


def test_windows_desktop_shortcut_opens_the_panel(windows, config, scripts):
    desktop, made = windows
    st = autostart.shortcut_status()
    assert st.supported and not st.enabled and st.detail == "On your desktop"

    st = autostart.add_shortcut(config)
    assert st.enabled and Path(st.where) == desktop / "Kalends.lnk"
    (m,) = made
    assert m["target"] == str(scripts / "kalends-open.exe")
    assert m["args"] == f'-c "{config.resolve()}"'  # quoted: the folder name has a space
    assert m["workdir"] == str(config.resolve().parent)
    assert m["icon"].endswith("kalends.ico") and Path(m["icon"]).exists()

    assert not autostart.remove_shortcut().enabled
    assert not (desktop / "Kalends.lnk").exists()
    assert not autostart.remove_shortcut().enabled  # nothing to remove is fine


def test_start_menu_entry_uses_the_icon_too(windows, config, scripts):
    _, made = windows
    lnk = autostart.add_start_menu_entry(config)
    assert lnk is not None and lnk.name == "Kalends settings.lnk"
    assert str(lnk).startswith(str(config.parent.parent))  # never written outside the test folder
    assert made[0]["target"] == str(scripts / "kalends-open.exe") and made[0]["icon"].endswith("kalends.ico")


def test_falls_back_to_python_when_kalends_open_is_missing(windows, config, scripts):
    _, made = windows
    (scripts / "kalends-open.exe").unlink()
    autostart.add_shortcut(config)
    assert made[0]["target"] == sys.executable
    assert made[0]["args"].startswith("-m kalends.service --open -c ")


def test_mac_app_runs_kalends_open(tmp_path, monkeypatch, config, scripts):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    st = autostart.add_shortcut(config)
    app = tmp_path / "Applications" / "Kalends.app"
    assert st.enabled and Path(st.where) == app and "Applications" in st.detail
    plist = (app / "Contents" / "Info.plist").read_text(encoding="utf-8")
    assert "<string>Kalends</string>" in plist and autostart.MAC_APP_ID in plist
    launcher = (app / "Contents" / "MacOS" / "Kalends").read_text(encoding="utf-8")
    assert launcher.startswith("#!/bin/sh\nexec ")
    assert str(scripts / "kalends-open") in launcher and f"'{config.resolve()}'" in launcher

    autostart.add_shortcut(config)  # running it again just refreshes it
    assert not autostart.remove_shortcut().enabled and not app.exists()


def test_mac_never_touches_someone_elses_app(tmp_path, monkeypatch, config, scripts):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    other = tmp_path / "Applications" / "Kalends.app" / "Contents"
    other.mkdir(parents=True)
    (other / "Info.plist").write_text("<string>com.example.other</string>")
    with pytest.raises(FileExistsError):
        autostart.add_shortcut(config)
    autostart.remove_shortcut()
    assert (other / "Info.plist").read_text() == "<string>com.example.other</string>"


def test_linux_apps_menu_entry(tmp_path, monkeypatch, config, scripts):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    st = autostart.add_shortcut(config)
    entry = tmp_path / "data" / "applications" / "kalends.desktop"
    assert st.enabled and Path(st.where) == entry and st.detail == "In your apps menu"
    text = entry.read_text(encoding="utf-8")
    assert text.startswith("[Desktop Entry]\n") and "Terminal=false\n" in text
    q = autostart._desktop_quote
    cfg = q(str(config.resolve()))
    assert cfg.startswith('"')  # the folder name has a space
    assert f"Exec={q(str(scripts / 'kalends-open'))} -c {cfg}\n" in text
    assert not autostart.remove_shortcut().enabled and not entry.exists()


def test_desktop_entry_quoting():
    q = autostart._desktop_quote
    assert q("/opt/kalends/bin/kalends-open") == "/opt/kalends/bin/kalends-open"
    assert q("/home/me/My Kalends/config.yaml") == '"/home/me/My Kalends/config.yaml"'
    assert q("/home/me/100%/c.yaml") == "/home/me/100%%/c.yaml"
    assert q('/a "b"/$x') == '"/a \\\\"b\\\\"/\\\\$x"'
    assert q("/a\\b c") == '"/a\\\\\\\\b c"'


def test_unsupported_system(monkeypatch, config):
    monkeypatch.setattr(sys, "platform", "sunos5")
    st = autostart.shortcut_status()
    assert not st.supported and not autostart.add_shortcut(config).enabled


def test_cli_turns_it_on_and_off(windows, config, capsys):
    desktop, _ = windows
    cli.main(["-c", str(config), "shortcut", "on"])
    assert capsys.readouterr().out.startswith("On: On your desktop (")
    cli.main(["-c", str(config), "shortcut", "status"])
    assert capsys.readouterr().out.startswith("On: ")
    cli.main(["-c", str(config), "shortcut", "off"])
    assert capsys.readouterr().out.startswith("Off: ")
    assert not (desktop / "Kalends.lnk").exists()


def test_cli_reports_a_failure_instead_of_a_traceback(windows, config, monkeypatch, capsys):
    def boom(*a, **k):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(autostart, "make_windows_shortcut", boom)
    with pytest.raises(SystemExit) as exc:
        cli.main(["-c", str(config), "shortcut", "on"])
    assert exc.value.code == 1 and "Access is denied" in capsys.readouterr().err


def test_installers_ask_and_keep_an_existing_shortcut_current():
    ps1 = (ROOT / "scripts" / "windows" / "install.ps1").read_text(encoding="utf-8")
    assert "[switch]$Shortcut, [switch]$NoShortcut" in ps1
    assert "shortcut status" in ps1 and "Add a Kalends shortcut to your desktop? [Y/n]" in ps1
    assert "[Console]::IsInputRedirected" in ps1  # never waits for an answer nobody can give
    sh = (ROOT / "scripts" / "unix" / "install.sh").read_text(encoding="utf-8")
    assert "KALENDS_SHORTCUT" in sh and "shortcut status" in sh and "[ -t 0 ]" in sh
    assert "shortcut off" in (ROOT / "scripts" / "windows" / "uninstall.ps1").read_text(encoding="utf-8")
