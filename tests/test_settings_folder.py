"""Settings folder: Kalends' own folder by default, or a folder chosen in the panel (with a warning)."""

import json
import os
import re

import pytest
from test_admin import admin, rt, sign_in  # noqa: F401  (fixtures used by the panel tests)

from kalends import cli, service, settings_folder, update_helper, updates


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(settings_folder.ENV_VAR, raising=False)
    monkeypatch.setattr(settings_folder, "_started_with", None)


@pytest.fixture
def app_dir(tmp_path):
    d = tmp_path / "Kalends"
    d.mkdir()
    return d


def fill(folder, marker: str) -> None:
    """Every settings file and folder Kalends keeps, each holding ``marker``."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in ("config.yaml", ".env", *settings_folder.FILES):
        (folder / name).write_text(marker, encoding="utf-8")
    for name in settings_folder.FOLDERS:
        (folder / name).mkdir(exist_ok=True)
        (folder / name / "one.ics").write_text(marker, encoding="utf-8")


def pointer(app_dir) -> dict:
    return json.loads((app_dir / settings_folder.POINTER).read_text(encoding="utf-8"))


# --- where the settings are -------------------------------------------------------------------


def test_default_is_kalends_own_folder(app_dir):
    cfg = app_dir / "config.yaml"
    assert settings_folder.resolve_config(cfg) == cfg
    assert settings_folder.is_default(cfg)
    assert settings_folder.app_config() == cfg.resolve()


def test_pointer_moves_the_settings(app_dir, tmp_path):
    synced = tmp_path / "Sync" / "Kalends" / "Settings"
    (app_dir / settings_folder.POINTER).write_text(json.dumps({"settings_dir": str(synced)}))
    cfg = app_dir / "config.yaml"
    assert settings_folder.resolve_config(cfg) == synced / "config.yaml"
    assert settings_folder.app_config() == cfg.resolve()  # auto-start keeps Kalends' folder path


def test_a_config_in_the_settings_folder_resolves_to_itself(tmp_path):
    synced = tmp_path / "Sync" / "Settings"
    synced.mkdir(parents=True)
    assert settings_folder.resolve_config(synced / "config.yaml") == synced / "config.yaml"


def test_unreadable_pointer_means_the_default(app_dir):
    (app_dir / settings_folder.POINTER).write_text("{not json")
    assert settings_folder.is_default(app_dir / "config.yaml")


def test_env_var_overrides_pointer(app_dir, tmp_path, monkeypatch):
    (app_dir / settings_folder.POINTER).write_text(json.dumps({"settings_dir": str(tmp_path / "a")}))
    monkeypatch.setenv(settings_folder.ENV_VAR, str(tmp_path / "b"))
    assert settings_folder.resolve_config(app_dir / "config.yaml") == tmp_path / "b" / "config.yaml"
    with pytest.raises(ValueError):
        settings_folder.change(app_dir / "config.yaml", str(tmp_path / "c"))


# --- changing it ------------------------------------------------------------------------------


def test_move_copies_every_settings_file_and_never_overwrites(app_dir, tmp_path):
    cfg = app_dir / "config.yaml"
    fill(app_dir, "mine")
    synced = tmp_path / "Sync"
    synced.mkdir()
    (synced / "state.json").write_text("synced")

    moved = settings_folder.change(cfg, str(synced))
    assert moved.folder == synced
    assert moved.kept == ["state.json"]
    expected = {"config.yaml", ".env", *settings_folder.FILES, *settings_folder.FOLDERS} - {"state.json"}
    assert set(moved.copied) == expected and not moved.replaced and moved.backup is None
    assert (synced / "state.json").read_text() == "synced"  # the synced copy wins
    assert (synced / "local-calendars" / "one.ics").read_text() == "mine"
    assert (synced / "cache" / "one.ics").read_text() == "mine"
    assert (app_dir / "config.yaml").read_text() == "mine"  # nothing deleted
    assert not list(synced.glob(".*.copying"))
    if os.name != "nt":
        assert (synced / ".env").stat().st_mode & 0o777 == 0o600
    assert settings_folder.settings_dir(cfg) == synced


def test_back_to_kalends_folder_uses_the_settings_in_use(app_dir, tmp_path):
    cfg = app_dir / "config.yaml"
    fill(app_dir, "before the move")
    synced = tmp_path / "Sync"
    settings_folder.change(cfg, str(synced))
    fill(synced, "edited since")  # Kalends ran from the synced folder for a while

    back = settings_folder.change(cfg, "")
    assert back.folder == app_dir.resolve() and not back.kept
    assert set(back.replaced) == {"config.yaml", ".env", *settings_folder.FILES, *settings_folder.FOLDERS}
    assert (app_dir / "config.yaml").read_text() == "edited since"
    assert (app_dir / "local-calendars" / "one.ics").read_text() == "edited since"
    assert (back.backup / "config.yaml").read_text() == "before the move"  # the old copies are kept
    assert (back.backup / "local-calendars" / "one.ics").read_text() == "before the move"
    assert not (back.backup / "cache").exists()  # only copies of the calendars: not worth keeping
    assert (synced / "config.yaml").read_text() == "edited since"  # nothing deleted
    assert pointer(app_dir) == {"settings_dir": ""}
    assert settings_folder.is_default(cfg)


def test_a_failed_copy_leaves_the_folder_as_it_was(app_dir, tmp_path, monkeypatch):
    cfg = app_dir / "config.yaml"
    fill(app_dir, "mine")
    calls = []

    def flaky(src, dst, **kw):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(src, dst, **kw)

    original = settings_folder.shutil.copy2
    monkeypatch.setattr(settings_folder.shutil, "copy2", flaky)
    with pytest.raises(OSError):
        settings_folder.change(cfg, str(tmp_path / "Sync"))
    assert not (app_dir / settings_folder.POINTER).exists()  # still Kalends' own folder
    assert settings_folder.is_default(cfg)
    assert not (tmp_path / "Sync" / ".env").exists()  # no half-written file under the real name


def test_change_stores_home_paths_portably(app_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    settings_folder.change(app_dir / "config.yaml", str(tmp_path / "SyncThing" / "Kalends" / "Settings"))
    assert pointer(app_dir) == {"settings_dir": "~/SyncThing/Kalends/Settings"}


def test_backups_cover_every_settings_file(config_path):
    for name in settings_folder.FILES:
        config_path.with_name(name).write_text("{}")
    dest = updates.backup_settings(config_path, config_path.with_name(".env"))
    assert {p.name for p in dest.iterdir()} >= {"config.yaml", ".env", *settings_folder.FILES}


# --- command line and background start -------------------------------------------------------


def test_cli_follows_the_pointer(app_dir, config_path, capsys):
    (app_dir / settings_folder.POINTER).write_text(json.dumps({"settings_dir": str(config_path.parent)}))
    cli.main(["-c", str(app_dir / "config.yaml"), "validate"])  # app_dir has no config.yaml of its own
    assert "config ok" in capsys.readouterr().out.lower()


def test_cli_settings_folder_shows_and_changes_it(app_dir, tmp_path, capsys):
    cfg = app_dir / "config.yaml"
    fill(app_dir, "mine")
    cli.main(["-c", str(cfg), "settings-folder"])
    assert "the default" in capsys.readouterr().out
    target = tmp_path / "Sync" / "Settings"
    cli.main(["-c", str(cfg), "settings-folder", str(target)])
    out = capsys.readouterr().out
    assert f"now {target}" in out and "copied" in out and "Restart Kalends" in out
    assert (target / ".env").exists()
    cli.main(["-c", str(cfg), "settings-folder"])
    assert str(target) in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["-c", str(cfg), "settings-folder", "relative/dir"])
    cli.main(["-c", str(cfg), "settings-folder", "--default"])
    assert "replaced" in capsys.readouterr().out and settings_folder.is_default(cfg)


def test_autostart_and_shortcut_use_kalends_own_config(app_dir, config_path, monkeypatch, capsys):
    from kalends import autostart

    (app_dir / settings_folder.POINTER).write_text(json.dumps({"settings_dir": str(config_path.parent)}))
    seen = []
    monkeypatch.setattr(
        autostart, "enable", lambda c: seen.append(c) or autostart.AutostartStatus(True, True)
    )
    monkeypatch.setattr(autostart, "add_start_menu_entry", lambda c: seen.append(c))
    monkeypatch.setattr(
        autostart, "add_shortcut", lambda c: seen.append(c) or autostart.ShortcutStatus(True, True)
    )
    cli.main(["-c", str(app_dir / "config.yaml"), "autostart", "on"])
    cli.main(["-c", str(app_dir / "config.yaml"), "shortcut", "on"])
    assert seen == [(app_dir / "config.yaml").resolve()] * 3


def test_background_start_rereads_the_pointer(app_dir, config_path, monkeypatch):
    (app_dir / settings_folder.POINTER).write_text(json.dumps({"settings_dir": str(config_path.parent)}))
    resolved = settings_folder.resolve_config(app_dir / "config.yaml")
    assert resolved == config_path
    spawned = {}
    monkeypatch.setattr(service.subprocess, "Popen", lambda args, **kw: spawned.update(args=args, **kw))
    service._spawn_background(resolved)
    assert spawned["args"][-1] == str((app_dir / "config.yaml").resolve())
    assert spawned["cwd"] == str(app_dir.resolve())


# --- settings panel ---------------------------------------------------------------------------


@pytest.fixture
def started_from(config_path, monkeypatch):
    """The panel's Kalends was started with this config path (normally Kalends' own folder)."""
    monkeypatch.setattr(settings_folder, "_started_with", config_path.resolve())
    return config_path


def test_setup_page_shows_folder_and_warning(admin, started_from):  # noqa: F811
    sign_in(admin)
    html = admin.get("/setup").text
    assert "Settings folder" in html and str(started_from.parent.resolve()) in html
    assert "(the default)" in html and ".env holds secrets" in html and "one computer at a time" in html
    assert "Restart Kalends now" not in html


def test_move_needs_full_path_and_confirmation(admin, started_from, tmp_path):  # noqa: F811
    csrf = sign_in(admin)
    r = admin.post("/setup/folder", data={"csrf": csrf, "action": "move", "folder": str(tmp_path / "s")})
    assert r.status_code == 400 and "read the warning" in r.text
    r = admin.post(
        "/setup/folder", data={"csrf": csrf, "action": "move", "folder": "relative/dir", "understand": "on"}
    )
    assert r.status_code == 400 and "full path" in r.text
    r = admin.post("/setup/folder", data={"csrf": csrf, "action": "move", "folder": ""})
    assert r.status_code == 400 and "Type the folder" in r.text
    assert not (started_from.parent / settings_folder.POINTER).exists()


def test_move_restarts_into_the_new_folder(admin, rt, started_from, tmp_path, monkeypatch):  # noqa: F811
    csrf = sign_in(admin)
    calls = []
    monkeypatch.setattr(updates, "start_restart", lambda *a: calls.append(a))
    target = tmp_path / "Sync" / "Kalends" / "Settings"
    r = admin.post(
        "/setup/folder", data={"csrf": csrf, "action": "move", "folder": str(target), "understand": "on"}
    )
    assert r.status_code == 200 and "Restarting Kalends" in r.text
    assert re.search(r"Copied .*config\.yaml", r.text) and "Nothing was deleted" in r.text
    assert (target / "config.yaml").exists() and (target / ".env").exists()
    in_use, app_cfg, cmd, health = calls[0]
    assert in_use == rt.store.config_path and app_cfg == started_from.resolve()
    assert cmd[-1] == str(started_from.resolve()) and health.endswith("/healthz")

    # This process still runs from the old folder: the page offers the restart again.
    html = admin.get("/setup").text
    assert str(target) in html and "Restart Kalends now" in html
    r = admin.post("/setup/folder/restart", data={"csrf": csrf})
    assert r.status_code == 200 and len(calls) == 2


def test_moving_to_the_folder_in_use_changes_nothing(admin, started_from, monkeypatch):  # noqa: F811
    csrf = sign_in(admin)
    monkeypatch.setattr(updates, "start_restart", lambda *a: pytest.fail("restarted for nothing"))
    r = admin.post(
        "/setup/folder",
        data={"csrf": csrf, "action": "move", "folder": str(started_from.parent), "understand": "on"},
    )
    assert r.status_code == 303 and r.headers["location"] == "/setup?ok=folder_same#folder"
    assert "nothing changed" in admin.get(r.headers["location"]).text
    assert json.loads((started_from.parent / settings_folder.POINTER).read_text()) == {"settings_dir": ""}


def test_update_restarts_with_kalends_own_config(admin, rt, started_from, tmp_path, monkeypatch):  # noqa: F811
    """After a move, an update must restart Kalends the way auto-start does, not from the moved path."""
    csrf = sign_in(admin)
    rt.updates = updates.UpdateChecker(
        fetch=lambda repo: updates.Release("9.0.0", "v9.0.0", "v9.0.0", "", "u", "2026-01-01")
    )
    admin.post("/setup/updates/check", data={"csrf": csrf})
    monkeypatch.setattr(
        updates, "update_readiness", lambda root, tag=None, fetch=True: updates.Readiness(True, kind="git")
    )
    started = {}
    monkeypatch.setattr(
        updates, "start_update", lambda root, cfg, tag, cmd, health, python=None: started.update(cmd=cmd)
    )
    r = admin.post("/setup/updates/apply", data={"csrf": csrf})
    assert r.status_code == 200 and started["cmd"][-1] == str(started_from.resolve())


def test_helper_restart_only_mode(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(update_helper.time, "sleep", lambda s: None)
    monkeypatch.setattr(update_helper, "stop", lambda pid, wait=20: calls.append(("stop", pid)))
    monkeypatch.setattr(update_helper, "start", lambda cmd, cwd: calls.append(("start", cmd, cwd)) or 1)
    monkeypatch.setattr(update_helper, "healthy", lambda url, wait: True)
    cfg = tmp_path / "config.yaml"
    rc = update_helper.main(
        ["--restart-only", "--config", str(cfg), "--pid", "42", "--start", '["kalends-service", "-c", "x"]']
    )
    assert rc == 0
    assert calls == [("stop", 42), ("start", ["kalends-service", "-c", "x"], tmp_path)]


def test_helper_still_needs_a_tag_to_update(monkeypatch):
    monkeypatch.setattr(update_helper.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit):
        update_helper.main(["--config", "c.yaml"])
