# Kalends (code)

Public repo (MIT). Kalends merges several ICS calendar feeds (Google, Outlook, iCloud, its own local calendars) into one week view and shares it with each audience at a chosen detail level (`full`, `title`, `label`, `busy`, `hidden`), with optional passwords. One Python process: FastAPI serves the public pages and a settings panel; no database. It was called Glimpse until 0.11 (the `glimpse` package and scripts are kept so old installs keep working; new code imports `kalends`). Read `README.md` for what users see and `CONTRIBUTING.md` for the rules below in full.

## Privacy is the point
- `src/kalends/policy.py` is the privacy boundary: renderers (`render/` HTML, Excel, ICS) and the web views only ever get `VisibleEvent` objects from it. Never let a renderer read raw events.
- Never widen what a level reveals without a config option that defaults to the safer behaviour, and add a test that proves the hidden text appears in none of the HTML, Excel and ICS output.
- Availability (`availability.py`) looks at every calendar but must never reveal anything about an event beyond "busy".
- Never commit real calendar data: `config.yaml`, `.env`, `state.json`, `requests.json`, `local-calendars/`, `video-accounts.json`, `school-state.json`, `backups/`, `logs/` and caches are gitignored (and `config.yaml` / `.env` are blocked by the pre-commit hook). Test fixtures and demo calendars (`tests/fixtures/`, `examples/demo/`) are made up. Feed URLs and link slugs work like passwords: never put them in code, tests, logs, issues or commit messages.

## Layout
- `src/kalends/`: `cli.py` (`kalends serve | init | open | service | check | validate | preview | links | ...`), `config.py` (YAML with `${VAR}` secrets from `.env`; `CONFIG_VERSION`), `migrations.py` (one step per config format change), `store.py` (the settings panel writes `config.yaml` / `.env` through it), `sources.py` (feed download with ETag / Last-Modified, last good copy kept), `events.py` (iCalendar parsing and recurrence), `core.py` (the cached "what does audience X see this week" query), `policy.py`, `availability.py` / `bookings.py` / `requests_store.py` (meeting requests), `school.py` / `school_page.py` (school terms), `localcal.py` (Kalends' own calendars, `.ics` files in `local-calendars/`), `notify.py`, `conferencing.py`, `updates.py` / `update_helper.py` (one-click update from GitHub releases), `server.py` (the two web servers in one process), `service.py` / `autostart.py` (background mode, start at sign-in, shortcuts), `support.py` / `diagnostics.py` (bug reports safe to paste; `diagnostics.py` is shared with other apps, keep it byte-identical), `web/` (`app.py`, `viewer.py`, `admin.py`, `auth.py`, Jinja templates), `render/`.
- `scripts/` holds the install scripts behind `Install Kalends.cmd` / `.command`.

## Run / test
```bash
pip install -e ".[dev]"            # Python 3.11+
pip install pre-commit && pre-commit install
pytest                             # tests build their config from tests/fixtures (see tests/conftest.py)
ruff check src tests && ruff format src tests
kalends serve -c config.yaml       # local run; `kalends init` makes a config to start from
```
CI runs the tests on Linux and Windows with Python 3.11 to 3.13, ruff, and a gitleaks scan of the whole history.

## Conventions
- Line length 110; ruff rules E, F, I, B, UP, S. Keep the code working on Windows, macOS and Linux.
- Don't start the server or the background service without saying so.
- Commits are SSH-signed (configured globally); don't disable signing.

## Releases
1. Move the `[Unreleased]` notes in `CHANGELOG.md` under a new version heading and set the same version in `src/kalends/__init__.py` (the only place it lives).
2. If the shape of `config.yaml` changed, bump `CONFIG_VERSION` in `config.py` and add a step to `migrations.py`, with a test.
3. Commit, tag `vX.Y.Z`, push the tag, and publish a GitHub release whose notes are that version's changelog section. Installs only see published releases; never retag a published version or force-push `main`.
