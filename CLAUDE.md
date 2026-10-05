# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

BlackVue Sync v2 synchronizes recordings from BlackVue dashcams to a local directory over HTTP. It has two runtimes: a `sync` CLI whose core (`sync.py`, `metrics.py`) uses only the standard library, and a `serve` web service (Flask) with an internal scheduler, dashboard, settings UI, log viewer, statistics and a recording viewer. Both ship in the Docker image, which defaults to `serve`.

Repository: <https://github.com/tekgnosis-net/blackvuesync-v2> (standalone, not a GitHub fork). Documentation site: <https://tekgnosis-net.github.io/blackvuesync-v2/>.

It started as a fork of the original BlackVue Sync by Alessandro Colomba (<https://github.com/acolomba/blackvuesync>) and became a separate project at 3.0.0. The fork phase (PRs #1-#22) lives in the archived repo `tekgnosis-net/blackvuesync`; CHANGELOG entries for 2.3.0-2.8.0a0 link there, entries for 2.2.0 and earlier link to upstream PRs.

Naming: command `blackvuesync-v2`, module `blackvuesync_v2` (`python -m blackvuesync_v2`), distribution `blackvuesync-v2`, image `ghcr.io/tekgnosis-net/blackvuesync-v2`, UI name "BlackVue Sync v2". Deliberately kept from upstream so existing setups keep working: `BLACKVUESYNC_*` env vars, `blackvuesync_*` Prometheus metric names and the Pushgateway job default `blackvuesync`, and the on-disk names `.blackvuesync.lock` (shared with an upstream cron job on the same destination) and `.blackvuesync.metrics-state.json` (pinned by a test).

## Claude Code

- Prefer using the LSP plugin over textual search.
- When a sandbox operation fails, stop to ask the user. Avoid disabling the sandbox.
- Create plans under the `docs/plans/` directory.

## Development Setup

The project uses `pyproject.toml` for dependency management. Development dependencies (black, Flask, pre-commit, pytest) are defined as optional dependencies.

### Setup

Create a virtual environment and install development dependencies:

```bash
# create virtual environment
python3 -m venv venv

# activate it
source venv/bin/activate

# install package in editable mode with dev dependencies
pip install -e ".[dev]"

# install pre-commit hooks
pre-commit install

# install commit-msg hook for gitlint
pre-commit install --hook-type commit-msg
```

The `-e` flag installs in editable mode, so changes under `blackvuesync_v2/` take effect immediately without reinstalling. Add `docs` to the extras (`pip install -e ".[dev,docs]"`) to build the documentation site.

Pre-commit hooks will automatically run on `git commit` to check code quality, format code, and scan for secrets. The hooks include Black, shellcheck, yamllint, trufflehog, and others.

## Guidelines

### Comments

Python docstrings and inline code comments in Python, YAML, shell, etc. are lowercase. The word "TODO" remains all-caps. Entities such as file names etc. preserve their casing.

Comments must be in the third-person, e.g. "installs", not "install", because they are descriptive. Avoid the imperative.

Keep comments concise, and non-obvious. Avoid documenting what everybody is expected to know.

### Python

Prefer Python idiomatic ("pythonic") style.

Always use type annotations.

### Code Formatting

Code formatting is handled automatically by pre-commit hooks (Black for Python, yamlfmt for YAML).

### Git

- Git commit messages must be longer than 5 characters, and each line must be less than 80.
- You can expect pre-commit hooks to fail when attempting to commit. Fix the errors.
- NEVER use `--no-verify` to skip the hooks.

## Architecture

### Package Design

The application is a Python package under `blackvuesync_v2/`. Core modules:

- `sync.py` -- filename regex, dashcam HTTP client, download/resume, retention,
  locking. The primary sync logic; kept self-contained for portability.
- `metrics.py` -- `SyncMetrics` dataclass and Prometheus text emission.
- `settings.py` -- `Settings` frozen-dataclass tree, `SettingsStore` with atomic
  JSON persistence, per-section validators, env-var bootstrap, and schema
  migration. See "Settings" section below.
- `__main__.py` -- CLI entry point with two subcommands: `sync` (one-shot,
  cron-era flags) and `serve` (long-running web service with the internal
  scheduler). When the first argument is not a subcommand or flag (the legacy
  `blackvuesync-v2 <address> ...` form), `sync` is inserted.

### Settings

`SettingsStore` persists configuration to `/config/settings.json` (override via
`BLACKVUESYNC_CONFIG_PATH` env var). Env vars are **seed-only**: they populate
the file on first run; subsequent runs read the file and ignore env vars. The
file has `0600` permissions and `SettingsStore` refuses to load if the mode is
wider than that.

Settings are organized into twelve frozen-dataclass sections (schema
`version` 2):

| Section | TIER | Key fields |
| --- | --- | --- |
| connection | restart | address, timeout_seconds |
| schedule | next_tick | cron_expression, timezone, paused |
| sync | next_tick | priority, grouping, include, exclude, retry_failed_after, skip_metadata, affinity_key |
| retention | next_tick | keep, max_used_disk_percent |
| logging | immediate | verbose, quiet, format, file_max_bytes, file_backup_count, ring_buffer_capacity |
| metrics | immediate | file, pushgateway_url, job, instance, state_file |
| stats | next_tick | retention_days |
| viewer | immediate | journey_mode, speed_unit, continuous_play |
| camera | immediate | read_timeout_seconds |
| web | restart | port, session_lifetime_hours |
| auth | immediate | mode, username, password_hash, session_secret, trusted_proxies, proxy_user_header |
| system | restart | destination, dry_run |

`TIER` (`immediate` / `next_tick` / `restart`) indicates how quickly a change
propagates. All mutations go through `SettingsStore.update()`, which validates,
saves atomically, and fires change-listener callbacks. The settings UI
(`server/settings_form.py`) declares which fields each section exposes; the
auth secrets are deliberately not form fields.

Env vars read on first-run bootstrap: `ADDRESS`, `TIMEOUT`,
`BLACKVUESYNC_SCHEDULE`, `BLACKVUESYNC_TIMEZONE`, `PRIORITY`, `GROUPING`,
`INCLUDE`, `EXCLUDE`, `RETRY_FAILED_AFTER`, `SKIP_METADATA`, `AFFINITY_KEY`,
`KEEP`, `MAX_USED_DISK`, `VERBOSE`, `QUIET`, `LOG_FORMAT`, `METRICS_*`,
`STATS_RETENTION_DAYS`, `BLACKVUESYNC_PORT`, `BLACKVUESYNC_ADMIN_USERNAME`,
`BLACKVUESYNC_ADMIN_PASSWORD` (hashed into `auth.password_hash` when at least
12 characters; never logged). `CRON` and `RUN_ONCE` are retired and only produce a warning. `DRY_RUN` and
`TZ` are not seeded into the file; `system.dry_run` and `schedule.timezone`
must be set through the file or the settings UI.

Other process-level env vars (read on every start, not persisted):
`BLACKVUESYNC_CONFIG_PATH` and `BLACKVUESYNC_TRUST_PROXY`. The latter marks
the session cookie `Secure` and is the only switch that enables `ProxyFix`
(one hop of `X-Forwarded-*`); without it `request.remote_addr` is the socket
peer.

Validation notes: every field is type-checked against its annotation before
value checks (wrong types are a 422 from the API, and fall back to the default
with a warning when loading a hand-edited file). `store.update()` re-validates
only the sections that changed. Cron expressions are range-checked by a stdlib
parser and follow standard cron semantics (day-of-week 0/7 = Sunday; when both
day-of-month and day-of-week are restricted either may match);
`server/scheduler.py` translates them for APScheduler via
`settings.cron_trigger_fields()`. `retention.keep`, `sync.retry_failed_after`
and include/exclude reuse the parsers in `sync.py`; an empty `keep` means keep
forever. An empty `connection.address` is valid; a sync run then fails with a
clear message. The store generates `auth.session_secret` when the file lacks
one and refuses to save an empty secret; `password_hash` / `session_secret`
cannot be set through `PATCH /api/settings/auth`.

Settings schema version is 2: the v1 -> v2 migration turns the literal
`/config/metrics-state.json` default into `""`, which `sync_runner` resolves to
`metrics-state.json` next to `settings.json` (callers pass `store.path.parent`
as `state_dir`); the state file is only read or written while metrics are
enabled.

Serve mode also keeps these files next to `settings.json`: `stats.db` (SQLite
per-run metrics, see `server/stats_store.py`) and `logs/blackvuesync.log`
(rotating, sized by `logging.file_max_bytes` / `file_backup_count`).

### Core Flow

1. **Lock acquisition**: Uses file locking (`fcntl`) to prevent concurrent runs on the same destination
2. **Destination preparation**: Creates directories, removes outdated recordings based on retention policy
3. **Dashcam communication**: HTTP requests to `blackvue_vod.cgi` endpoint to list recordings
4. **Recording parsing**: Filename-based extraction of metadata (date, type, direction)
5. **Download with resume**: Uses temporary dotfiles (`.filename.mp4`) for partial downloads
6. **Cleanup**: Removes temp files and empty grouping directories

### Recording Types

The filename regex (`filename_re`) parses BlackVue recording filenames to extract:

- **Timestamp**: `YYYYMMDD_HHMMSS`
- **Type**: N=Normal, E=Event, P=Parking, M=Manual, I=Impact, O=Overspeed, A=Acceleration, T=Cornering, B=Braking, R/X/G=Geofence, D/L/Y/F=DMS
- **Direction**: F=Front, R=Rear, I=Interior, O=Optional
- **Upload flag**: L=Live, S=Substream (optional)

Each recording consists of multiple files: `.mp4` (video), `.thm` (thumbnail), `.3gf` (accelerometer), `.gps` (GPS data).

### Grouping

Recordings can be organized into date-based directories (`--grouping`):

- `daily`: YYYY-MM-DD
- `weekly`: YYYY-MM-DD (Monday of week)
- `monthly`: YYYY-MM
- `yearly`: YYYY

Grouping speeds up loading in BlackVue Viewer and keeps directories manageable.

### Server Package

The web server lives under `blackvuesync_v2/server/`. It is a standard Flask
application, structured as follows:

- `__init__.py` -- `create_app(settings_store, ...)` factory. Configures
  Flask-WTF CSRF protection (tokens do not expire), ProxyFix middleware (only
  with `BLACKVUESYNC_TRUST_PROXY`), session cookie settings, and
  attaches `settings_store`, `progress_publisher`, `stats_store` (in-memory
  SQLite when none is passed) and related collaborators to the app instance.
  Registers the eighteen blueprints listed below and adds the
  `add_security_headers` after-request hook that injects CSP, X-Frame-Options,
  HSTS, and related headers on every response. CSP `script-src` is `'self'`
  only; templates must not use inline scripts or event handlers (the vendored
  Alpine is the CSP build). A settings listener updates `app.secret_key` when
  `auth.session_secret` rotates, so rotation signs everyone out immediately.
- `auth.py` -- Argon2id password hashing helpers (`hash_password`,
  `verify_password`, `needs_rehash`) and the `login_required` decorator.
  Maintains an in-memory sliding-window rate limiter (10 failures per 600 s →
  15-minute lockout) guarded by a `threading.Lock`, with stale entries pruned
  and the table size capped. Auth mode is read fresh from
  `current_app.settings_store` on every request, so a mode change takes effect
  immediately without a restart. In `login` mode the session carries `pwv`
  (a short hash of the password hash); a password change invalidates older
  sessions. Unauthenticated `/api/*` requests get 401 JSON
  (`AUTH_REQUIRED`), htmx requests get 401 with `HX-Redirect`, pages get a 302
  to `/login`. `proxy` mode checks the socket peer (not `X-Forwarded-For`)
  against `auth.trusted_proxies` (IPs or CIDRs).
- `routes/health.py` -- `GET /healthz` (always 200) and `GET /readyz` (200 when
  settings store is loaded; the app is built after the store loads, so the
  503 branch is unreachable in practice).
- `routes/auth.py` -- `GET|POST /login`, `POST /logout`, `GET|POST /first-run`.
  A `before_app_request` hook redirects every non-exempt path to `/first-run`
  while `auth.password_hash == ""` (sticky first-run flow).
- `routes/ui.py` -- page routes `/` (dashboard), `/settings`, `/logs`,
  `/stats`, `/viewer`; all protected by `@login_required`. The dashboard
  pre-renders its cards server-side so the first paint is populated.
- `routes/api_sync.py` -- JSON API routes at `/api/sync/*`; see "Sync API"
  subsection below.
- `routes/api_settings.py` -- `GET /api/settings`, `PATCH /api/settings/<section>`.
- `routes/api_auth.py` -- `GET /api/auth/me`, `POST /api/auth/password`,
  `DELETE /api/auth/sessions` (rotates `session_secret`).
- `routes/api_health.py` -- `/api/health/storage` and `/api/health/dashcam`.
- `routes/api_dashcam.py` -- `/api/dashcam/info`; read-only parse of the
  dashcam's `version.bin` and `config.ini`.
- `routes/api_camera.py` -- `GET /api/camera/config` (live read of the
  camera's `config.ini` with snapshot fallback and change diff) and
  `GET /api/camera/secret` (one decrypted password on request).
- `routes/hx_camera.py` -- `GET /hx/camera/panes`, the read-only Camera panes.
- `routes/api_recordings.py` -- `/api/recordings/recent`.
- `routes/api_schedule.py` -- `POST /api/schedule/pause|resume`.
- `routes/api_logs.py` -- `/api/logs/recent` snapshot and `/api/logs/stream` SSE.
- `routes/api_stats.py` -- `/api/stats/series` (summary, series, disk forecast).
- `routes/api_viewer.py` -- `/api/viewer/days`, day-scoped
  `/api/viewer/recordings?date=`, and per-recording `journey`, `gps`,
  `gsensor`. Never returns the whole library in one response.
- `routes/media.py` -- `/media/<path>` serves `.mp4`/`.thm` from the destination
  with HTTP Range; guarded by an extension allow-list, `safe_join`, and a
  realpath-containment check.
- `routes/hx_sync.py` -- HTMX fragment routes at `/hx/sync/*`; renders
  `_partials/sync_status_card.html` and `_partials/last_run_card.html`.
- `routes/hx_dashboard.py` -- HTMX dashboard cards at `/hx/*-card`.
- `progress.py` -- `FileProgress` / `SyncProgress` frozen dataclasses and
  `ProgressPublisher`; see "Progress Publisher" subsection below.
- `sync_runner.py` -- spawns `run_sync` on a daemon thread guarded by a
  module-level `threading.Lock`; surfaces a 409 when a sync is already running.
  The cooperative stop flag behind `POST /api/sync/stop` lives in `sync.py`
  (checked between download chunks) and is cleared at the start of each run.
  `sync.DashcamUnavailableError` (a `UserWarning` subclass, so the CLI is
  unchanged) marks the car-away state (unreachable/down host, timeouts, a
  dropped connection); `_do_sync` catches it specifically and logs one INFO
  line with no traceback, still recording the run failure. The last-sync card
  shows "dashcam not reachable" for runs where `RunRow.unreachable` is true.
- `scheduler.py` -- APScheduler `BackgroundScheduler` that fires the sync from
  `schedule.cron_expression` in `schedule.timezone`; honors `schedule.paused`.
- `log_buffer.py` -- ring-buffer logging handler behind the `/logs` page; unlike
  the progress publisher it delivers every line (batches, not latest-wins).
- `stats_store.py` -- SQLite store of one row per sync run, pruned by
  `stats.retention_days`. `RunRow.unreachable` is the single "car away" rule
  (failed, and every non-zero failure reason is in `UNREACHABLE_REASONS` =
  network/timeout), derived at read time so no migration is needed; the
  last-sync card and `/api/stats/series` (`offline`, `reachable_runs`,
  success rate over reachable runs, `null` when none) both use it.
- `forecast.py` -- least-squares disk-usage projection for the stats page.
- `viewer_index.py` -- enumerates downloaded recordings and computes journey
  chains of contiguous segments (same type by default; every type with
  `viewer.continuous_play`, because BlackVue writes an E/P segment in place of
  the N one for that minute). The viewer's sidebar highlight follows the
  playing segment. `RecordingIndex` (shared per destination +
  grouping via `recording_index()`) caches each directory's listing keyed by
  its mtime and re-lists only changed directories; listings younger than the
  2 s racy-mtime window are re-read on the next lookup. Skips `.`, `@` and `#`
  directories (Synology `@eaDir`, `#recycle`). `list_recordings()` stays the
  uncached walk. Scale reference: 42,706 recordings (~256k files) render in
  ~2 s cold / ~0.2 s warm.
- `camera_crypto.py` -- BlackVue Wi-Fi password encoding (AES-128-CBC, public
  key); the only module that imports `cryptography`.
- `camera_config.py` -- reads `config.ini` / `version.bin` without rewriting
  them, keeps the 0700/0600 snapshot under `<settings dir>/camera/` and diffs
  successive reads.
- `camera_schema.py` -- labels and display rules for the DR900X Plus keys;
  unknown keys go under "Other", and password-like names are always masked.
- `gps.py` / `gsensor.py` -- stdlib parsers for `.gps` (NMEA) and `.3gf`
  (big-endian binary) sidecars; formats in `docs/reference/blackvue-file-formats.md`.
- `settings_form.py` -- field descriptors that drive the settings page.
- `sse.py` -- shared Server-Sent Events response helper.

Front end: Jinja templates under `server/templates/`, with Alpine.js, htmx,
Chart.js and Leaflet vendored under `server/static/js/` (see `VENDORED.md`).
No build step; page scripts are plain files in `static/js/`.

Templates get `app_version` as a Jinja global; `base.html` shows it in the
header and footer only when `g.current_user` is set (every `login_required`
page, including auth mode `none`), never on the login or first-run pages.
Chart.js auto-colours a chart only when no dataset sets a colour, so a chart
that colours one series must colour them all (see `stats.js`).

Colours come from the tokens in `static/css/tokens.css`, which also declares
`color-scheme: light dark` so native controls follow the system theme. Use the
`-text` variants (`--color-success-text`, `--color-accent-text`, ...) for text
and `--color-accent-fill` behind white text; the base status colours are for
fills, bars and borders. Elements that set a background must also set a text
colour. `test/e2e/test_contrast.py` fails any text below 4.5:1 in light or
dark mode.

**Auth modes** (set via `settings.json` `auth.mode`):

| Mode | Behavior |
| --- | --- |
| `login` | Password auth enforced; session cookie issued on success. |
| `none` | All routes accessible without credentials (trusted-LAN use). |
| `proxy` | Reverse proxy is expected to authenticate; request is trusted. |

`login_required` sets `g.current_user` (`"anonymous"` in `none` mode) and
`g.auth_mode`. `base.html` shows the nav and version on every page where
`g.current_user` is set, and the Sign out button only in `login` mode.

**Argon2 parameters are locked**: `time_cost=3, memory_cost=65536,
parallelism=4, hash_len=32, salt_len=16`. Do not change without a migration
plan.

### Progress Publisher

`ProgressPublisher` (in `server/progress.py`) owns the thread-safe sync
progress state. It is instantiated once per server process in `cmd_serve` and
attached to the Flask app as `app.progress_publisher`.

**Writer API** (called from the sync thread):

| Method | Description |
| --- | --- |
| `begin_job(files_total, job_id=None) -> str` | starts a job; returns `job_id` (uuid4 hex when not supplied) |
| `start_file(filename, artifact, total_bytes)` | marks start of a file download |
| `set_total(files_total)` | sets the file total once the dashcam listing is known |
| `update_bytes(downloaded, total_bytes=0)` | progress tick; throttled to 5 Hz for subscribers |
| `finish_file(success, reason=None)` | closes a file; bumps aggregate counts |
| `skip_file()` | counts a file that needed no transfer (`files_skipped`) and removes it from `files_total` |
| `end_job(success)` | closes the job; retained for 10 s, then resets to idle (a stale reset timer never idles a newer job) |

`sync()` begins the job before listing the dashcam, so early failures
(dashcam unreachable, lock held) show up as `failed`. Totals count files, not
recordings.

**Reader API** (called from Flask handlers):

| Method | Description |
| --- | --- |
| `snapshot() -> SyncProgress` | returns current frozen state; safe from any thread |
| `subscribe() -> Iterator[SyncProgress]` | yields state changes; 30-second heartbeat timeout |

State transitions: `idle → running → complete/failed → idle`.
`SyncProgress` and `FileProgress` are frozen dataclasses; mutations use
`dataclasses.replace`. The CLI sync path passes `publisher=None`, and
`sync.py` imports `server.progress` only under `TYPE_CHECKING`, so it stays
free of Flask imports. `_NoopPublisher` is a no-op stand-in used by tests.

### Sync API

Sync-related endpoints are split between JSON API and HTMX fragments:

**JSON API** (`/api/sync/*`, all `@login_required`):

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/sync/progress` | current snapshot as JSON |
| `GET` | `/api/sync/progress/stream` | SSE stream of progress events |
| `POST` | `/api/sync/now` | trigger an on-demand sync |
| `POST` | `/api/sync/stop` | request cooperative stop; 202, or 404 when idle |
| `GET` | `/api/sync/last` | current non-idle snapshot; 204 when idle, including 10 s after a run ends |

`POST /api/sync/now` is CSRF-protected globally by Flask-WTF. It returns
202 + `{"job_id": ...}` or 409 + `{"code": "SYNC_ALREADY_RUNNING", ...}`.

The SSE stream emits `event: progress\ndata: <json>\n\n` frames, throttled
to 5 Hz. When no state change occurs for 30 seconds the generator emits
`": keepalive"` instead of a redundant data frame. Set `X-Accel-Buffering: no`
to prevent nginx-family proxy buffering. `server/sse.py` must not set
hop-by-hop headers such as `Transfer-Encoding`: waitress rejects them (500).
At most 16 SSE streams are open at once across endpoints (503
`TOO_MANY_STREAMS` beyond); waitress runs 32 threads. The limiter is
process-wide and a stream frees its slot only when its next write fails (up
to 30 s after the client leaves), so `test/e2e/conftest.py` resets it for
every `live_server`; a reset bumps a generation so late releases from older
streams never free a newer stream's slot.

**HTMX Fragments** (`/hx/sync/*`, all `@login_required`):

| Method | Path | Template |
| --- | --- | --- |
| `GET` | `/hx/sync/status-card` | `_partials/sync_status_card.html` |
| `GET` | `/hx/sync/last-run-card` | `_partials/last_run_card.html` |

The full endpoint reference, including settings, auth, health, logs, stats and
viewer APIs, is in `docs/api.md`. Update it alongside any route change.

### Logging

Two logger hierarchies:

- `logger`: Root logger, respects verbosity and quiet flags
- `cron_logger`: Remains active in cron mode for Normal/Manual recordings and errors

## Testing

### Test Structure

- `test/blackvuesync_test.py` -- Pytest unit tests for parsing, grouping, filtering
- `test/test_settings.py` -- unit tests for SettingsStore (100% coverage target)
- `test/test_auth.py` -- unit tests for Argon2 helpers and rate-limiter
- `test/test_routes_auth.py` -- Flask test-client tests for /login, /logout,
  /first-run (timing, rate-limit, CSRF)
- `test/test_routes_health.py` -- tests for /healthz and /readyz
- `test/test_routes_ui.py` -- tests for authenticated UI placeholder routes
- `test/test_security_headers.py` -- tests for CSP and other security headers
- `test/test_progress.py` -- unit tests for `FileProgress`, `SyncProgress`,
  `ProgressPublisher` state machine, throttle, retention, concurrency
- `test/test_sync_callback.py` -- tests for `download_file` `on_chunk` callback
- `test/test_sync_runner.py` -- tests for `trigger_sync` locking and daemon thread
- `test/test_dashcam_unreachable.py` -- car-away exception, one-line log, card label
- `test/test_routes_api_sync.py` -- tests for `/api/sync/*` endpoints and SSE
- `test/test_routes_hx_sync.py` -- tests for `/hx/sync/*` htmx fragment endpoints
- `test/test_routes_*.py` -- one file per blueprint (`api_auth`, `api_dashcam`,
  `api_health`, `api_logs`, `api_recordings`, `api_schedule`, `api_settings`,
  `api_stats`, `api_viewer`, `hx_dashboard`, `media`)
- `test/test_sync_resume.py`, `test/test_sync_stop_flag.py`,
  `test/test_clean_destination.py` -- download resume, cooperative stop, temp
  cleanup
- `test/test_scheduler.py`, `test/test_scheduler_pause.py` -- APScheduler wiring
- `test/test_log_buffer.py`, `test/test_logging_on_change.py`,
  `test/test_main_serve_logging.py` -- live logging
- `test/test_sync_fixes.py` -- truncated downloads, lock fd, progress counts,
  serve-mode settings
- `test/test_sse.py`, `test/test_server_hardening.py` -- SSE under waitress,
  stream cap, proxy trust, session revocation, CSRF, 401 handling
- `test/test_main_entry.py` -- CLI entry does not create stray settings files
- `test/test_stats_store.py`, `test/test_forecast.py` -- stats persistence and
  disk forecast
- `test/test_gps.py`, `test/test_gsensor.py`, `test/test_viewer_index.py` --
  viewer parsers, index and per-directory cache
- `test/test_settings_form.py`, `test/test_settings_page.py`,
  `test/test_dashboard_render.py`, `test/test_dashboard_sse_handoff.py` --
  page rendering
- `test/test_camera_*.py`, `test/test_routes_api_camera.py`,
  `test/test_routes_hx_camera.py`, `test/e2e/test_camera_settings.py` --
  camera crypto, config, schema, routes and browser tests; the fixtures in
  `test/fixtures/camera/` are sanitized (never a real camera file)
- `test/e2e/` -- Playwright browser tests for the dashboard, settings, logs,
  stats and viewer pages, frontend error handling, and a WCAG AA text-contrast
  audit of every page in light and dark mode (`test_contrast.py`). Deselected by default
  (`addopts = -m 'not e2e'`); run with `pytest test/e2e -m e2e`
- `features/` -- Behave BDD integration tests against a mock BlackVue dashcam

### Running Tests

```bash
# all unit tests (sync core only)
pytest test/blackvuesync_test.py -v

# full pytest suite, excluding browser tests
pytest test --ignore=test/e2e

# playwright browser tests (deselected by default)
pytest test/e2e -m e2e

# single unit test (by node id or keyword expression)
pytest test/blackvuesync_test.py::test_name -v
pytest test/blackvuesync_test.py -k "retention and weekly" -v

# all integration tests (Behave BDD against an in-process mock dashcam)
behave

# a single feature or scenario
behave features/sync_basic.feature
behave -n "scenario name substring"

# integration tests against the Docker image instead of a subprocess
behave -D implementation=docker

# combined unit+integration coverage (writes coverage_report/index.html)
./coverage.sh
```

`features/CLAUDE.md` documents the BDD harness (mock dashcam, step library, userdata
flags). Read it before changing anything under `features/`.

## Documentation and Releases

### Documentation site

User documentation is a Material for MkDocs site (`mkdocs.yml`, sources in
`docs/`, `docs/plans/` excluded) published to GitHub Pages by
`.github/workflows/pages.yml`: every pull request runs
`mkdocs build --strict` (broken links fail), pushes to `main` deploy.
Preview with `mkdocs serve` after `pip install -e ".[docs]"`.

- Guides live in `docs/guide/`; the README is a short landing page that links
  to the site. Update the matching guide in the same change as the behaviour.
- `docs/release-notes.md` and `docs/contributing.md` include `CHANGELOG.md`
  and `CONTRIBUTING.md` via `pymdownx.snippets`; edit those root files, not
  the stubs. The changelog include uses the `changelog` section markers.
- Links from `CHANGELOG.md` into the docs use absolute site URLs, because the
  file is also rendered on GitHub and in release notes.
- Material 9.7 is in maintenance mode (fixes through May 2027) and requires
  `mkdocs<2`; its successor Zensical reads the same `mkdocs.yml`.

### Screenshots

`python scripts/screenshots.py` regenerates `docs/assets/screenshots/*.png`
from a synthetic demo: ffmpeg test-pattern clips, a GPS track along an
OpenStreetMap route across the Sydney Harbour Bridge, invented run history
and a demo dashcam HTTP server (vod list, recordings, `Config/version.bin`,
`config.ini`, HEAD). It runs the real `serve` in a temp dir and drives it with
Playwright (`bypass_csp`, since the app CSP forbids eval). Requires ffmpeg and
Playwright Chromium. Never use real recordings or locations in screenshots.
Regenerate after UI changes and after a version bump (the footer shows the
version).

### Versioning

Semantic versioning (plain `MAJOR.MINOR.PATCH`, no pre-release suffixes),
first standalone release 3.0.0. The version lives in
`blackvuesync_v2/__init__.py` and `pyproject.toml`; `test_release_script.py`
fails if they differ or the CHANGELOG lacks a section for it. Releases go
through `scripts/release.py` (a script, not release-please, so no PAT secret
is needed):

1. `python scripts/release.py prepare X.Y.Z` on an up-to-date `main` bumps
   both files, turns `## Unreleased` into `## X.Y.Z - date`, commits on
   `release/vX.Y.Z` and opens a PR.
2. After the PR merges, `python scripts/release.py tag X.Y.Z` verifies
   `origin/main` and pushes an annotated tag with the maintainer's own
   credentials (a `GITHUB_TOKEN`-created tag would not trigger workflows).
3. The tag runs `release.yml` (GitHub release from the CHANGELOG section via
   `release.py notes`) and `docker-build.yml` (images `X.Y.Z`, `X.Y`, `X`).

Collect changes under `## Unreleased` at the top of the CHANGELOG between
releases. `latest` follows `main`. PyPI publishing (`ci.yml`
`publish` job) only runs when the repository variable `PYPI_PUBLISH` is
`true` and a PyPI trusted publisher exists for `blackvuesync-v2`.

## Important Constraints

### Python Version

Requires Python 3.9+ for modern type hints (`str | None`, walrus operator `:=`).

### External Dependencies

`sync.py` and `metrics.py` use only the Python standard library -- this
constraint must be maintained for portability (the cron-based sync path must
work without pip-installed packages).

The web server (`blackvuesync_v2/server/`) depends on Flask, Flask-WTF, waitress,
argon2-cffi, and APScheduler. These are listed as runtime dependencies in `pyproject.toml`
and installed via `pip install -e ".[dev]"` in the development setup.

### Backwards Compatibility

Recording filename patterns must remain compatible with existing BlackVue firmware. The filename regex is based on official BlackVue documentation.

### File Locking

Lock files are stored in the destination directory (`.blackvuesync.lock`). The destination must be on a local filesystem (not NFS) for `fcntl.lockf()` to work correctly.

## Docker-Specific Notes

The Docker image (`Dockerfile`):

- Uses Alpine Linux for minimal size
- Runs as `dashcam` user (UID/GID set via `PUID`/`PGID` env vars)
- Sync is scheduler-driven from inside the long-running web service; cadence
  comes from `settings.schedule.cron_expression` (default `*/15 * * * *`)
- `entrypoint.sh` remaps the dashcam user via `setuid.sh`, then execs
  `python -m blackvuesync_v2` with the CMD passed by Docker; defaults to `serve`
- Multi-stage build: runtime pip deps (Flask, Flask-WTF, waitress,
  argon2-cffi, APScheduler) are installed into `/opt/venv` in a builder stage via
  `uv`; the final stage copies only the venv, so `uv` is not in the image
- `EXPOSE 8080` documents the web server port; map it in `docker-compose.yml`
- `HEALTHCHECK` polls `GET /healthz` via Python `urllib.request` (no curl needed)
- `/config` volume: mount a host directory here; `settings.json` is stored inside
- `run.sh` is a local smoke-test that overrides the CMD with `sync --dry-run` to
  exercise the image against a real dashcam without standing up the web server
