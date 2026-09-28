# HTTP API Reference

This document describes the HTTP endpoints exposed by `blackvuesync-v2 serve`
(and the Docker container on port 8080).

## Conventions

- **Authentication.** Endpoints marked as requiring authentication follow
  `auth.mode`. When a request is not authenticated:
  - `/api/*` returns `401` with
    `{"error": "authentication required", "code": "AUTH_REQUIRED", "details": {}}`;
  - htmx requests (`HX-Request: true`) return `401` with an
    `HX-Redirect: /login?next=...` header;
  - pages redirect (`302`) to `/login?next=...`.
- **Sessions** are signed cookies. They end when the password changes (other
  than the session that made the change), when sessions are rotated, or after
  `web.session_lifetime_hours`.
- **CSRF.** `POST`, `PATCH` and `DELETE` need the `X-CSRFToken` header (or a
  `csrf_token` form field). The token comes from the page's
  `<meta name="csrf-token">` and does not expire.
- **Errors** use `{"error": "...", "code": "...", "details": {...}}`.
- **Server-Sent Events.** At most 16 streams (`/api/sync/progress/stream` and
  `/api/logs/stream` combined) are open at once. Beyond that the server
  returns `503` with code `TOO_MANY_STREAMS` and `Retry-After: 5`.
- **Proxies.** `X-Forwarded-*` headers are honored (one hop) only when
  `BLACKVUESYNC_TRUST_PROXY` is set.

---

## Health Endpoints

These endpoints are exempt from authentication and from the first-run redirect.

### `GET /healthz`

Liveness probe. Returns `200 OK` immediately.

```json
{"status": "ok"}
```

### `GET /readyz`

Readiness probe. Returns `200 OK` once the settings store has loaded.
The `503 Service Unavailable` branch exists for a missing settings store; in
practice the app is only constructed after the store loads, so it always
returns 200.

```json
{"status": "ready", "settings_loaded": true}
```

```json
{"status": "starting", "settings_loaded": false}
```

---

## Auth Endpoints

### `GET /first-run`

Displays the first-run setup wizard. Redirected to automatically when
`auth.password_hash` is empty.

### `POST /first-run`

Submits the initial password. Requires the `X-CSRFToken` header (or
`csrf_token` form field). Password must be at least 12 characters.

| Field | Required | Description |
| --- | --- | --- |
| `username` | no | Admin username; defaults to `admin` |
| `password` | yes | Initial admin password (min 12 chars) |
| `confirm` | yes | Confirmation; must match `password` |

On success: redirects to `/login`. If a password is already set, both `GET`
and `POST` redirect to `/login`.

On error: re-renders the form with a validation message and status `400`.

### `GET /login`

Renders the login form. Redirected to when a protected page is accessed
without a valid session (auth mode `login` only).

### `POST /login`

Authenticates the user. Requires the `X-CSRFToken` header.

| Field | Required | Description |
| --- | --- | --- |
| `username` | yes | Admin username (from `auth.username`) |
| `password` | yes | Admin password |
| `next` | no | Relative path to return to; values with a scheme or host are replaced by `/` |

- On success: sets a session cookie and redirects to `next`, or to `/`.
- On failure: re-renders the form with a generic error after a minimum
  delay of 1.5 seconds (uniform-timing defence).
- After 10 failures from the same IP within 10 minutes: requests are
  rejected for 15 minutes.

### `POST /logout`

Clears the session and redirects to `/login`. Requires an active session
(or auth mode `none`/`proxy`).

---

## UI Endpoints

All UI endpoints require authentication (subject to `auth.mode`).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/` | Dashboard: sync status, live progress, controls, cards |
| `GET` | `/settings` | Settings editor |
| `GET` | `/logs` | Live log viewer |
| `GET` | `/stats` | Run statistics and disk forecast |
| `GET` | `/viewer` | In-browser recording viewer |

---

## Sync API Endpoints

All endpoints below require authentication (subject to `auth.mode`).
CSRF protection applies to all `POST` requests (Flask-WTF global protection).

When a download is interrupted, the partial file is preserved and the next
sync resumes it via an HTTP range request (`Range: bytes=N-`). If the dashcam
responds with `200` instead of `206`, the sync falls back to a full
re-download.

### `GET /api/sync/progress`

Returns the current sync progress snapshot as JSON.

**Response (200 OK):**

```json
{
  "job_id": "a1b2c3d4e5f6...",
  "started_at_wall": 1747603200.0,
  "state": "running",
  "current_file": {
    "filename": "20230101_120000_NF.mp4",
    "recording_base": "20230101_120000_NF",
    "artifact": "mp4",
    "direction": "F",
    "total_bytes": 52428800,
    "downloaded_bytes": 10485760,
    "bytes_per_second": 2097152.0,
    "eta_seconds": 20.0,
    "state": "downloading",
    "failure_reason": null
  },
  "files_total": 12,
  "files_completed": 3,
  "files_failed": 0,
  "files_skipped": 40,
  "bytes_downloaded_total": 157286400
}
```

When no sync has run, `state` is `"idle"` and most fields are zero.

File counts are per file (video, thumbnail, accelerometer and GPS files each
count once; types excluded by `sync.skip_metadata` are not counted). A job
starts with `files_total` 0 while the dashcam is listed, so listing failures
still end the job as `"failed"`. Files that need no transfer (already
downloaded, or blocked by a recent failure marker) are counted in
`files_skipped` and removed from `files_total`, so `files_completed +
files_failed` reaches `files_total` at the end of a successful run.

### `GET /api/sync/progress/stream`

Server-Sent Events (SSE) stream of progress updates.

**Response headers:**

```http
Content-Type: text/event-stream
Cache-Control: no-store
X-Accel-Buffering: no
```

**Event format:**

```text
event: progress
data: {"state": "running", "files_completed": 3, ...}

```

Events are throttled to 5 Hz. When no state change occurs for 30 seconds,
a keepalive comment is emitted to keep the connection alive:

```text
: keepalive

```

**Example (curl):**

```bash
curl -N -H "Cookie: bvs_session=<token>" \
  http://localhost:8080/api/sync/progress/stream
```

### `POST /api/sync/now`

Triggers an on-demand sync. Requires the `X-CSRFToken` header.

**Response (202 Accepted) -- sync started:**

```json
{"job_id": "a1b2c3d4e5f6..."}
```

**Response (409 Conflict) -- sync already running:**

```json
{
  "error": "sync already running",
  "code": "SYNC_ALREADY_RUNNING",
  "details": {"current_job_id": "a1b2c3d4e5f6..."}
}
```

**Example (curl):**

```bash
curl -X POST \
  -H "Cookie: bvs_session=<token>" \
  -H "X-CSRFToken: <token>" \
  http://localhost:8080/api/sync/now
```

### `GET /api/sync/last`

Returns the current sync snapshot when it is not idle: a running job, or a
completed/failed job during its 10-second retention window.

- **204 No Content** -- the publisher is idle: no sync has run, or more than
  10 seconds have passed since the last one ended. Completed-run history is
  available from `/api/stats/series`.
- **200 OK** -- returns the same JSON body as `/api/sync/progress`.

---

## HTMX Fragment Endpoints

These endpoints return HTML fragments intended for use with HTMX polling
(`hx-get`, `hx-trigger="every 5s"`). Both require authentication.

### `GET /hx/sync/status-card`

Returns the `sync-status-card` HTML partial showing current sync state,
progress bar, and current file information.

### `GET /hx/sync/last-run-card`

Returns the `last-run-card` HTML partial showing the most recently completed
sync run (files synced, bytes downloaded, completion state).

---

## Settings API Endpoints

All endpoints below require authentication (subject to `auth.mode`).
CSRF protection applies to all `PATCH` requests (Flask-WTF global protection).

### `GET /api/settings`

Returns the full settings object as JSON. Every section carries a `_tier`
annotation (`immediate`, `next_tick`, or `restart`) indicating how quickly
a change to that section propagates. Secret fields (currently
`auth.password_hash` and `auth.session_secret`) are always replaced with the
sentinel string `"***"` regardless of whether they are empty. This prevents
the redacted snapshot from leaking the first-run state (`password_hash == ""`)
and lets clients safely round-trip the response back through a `PATCH`.

**Response (200 OK), truncated example:**

```json
{
  "version": 1,
  "connection": {
    "address": "192.168.0.1",
    "timeout_seconds": 10.0,
    "_tier": "restart"
  },
  "auth": {
    "mode": "login",
    "username": "admin",
    "password_hash": "***",
    "session_secret": "***",
    "trusted_proxies": [],
    "_tier": "immediate"
  },
  "sync": {
    "priority": "date",
    "grouping": "none",
    "include": [],
    "exclude": [],
    "retry_failed_after": "1d",
    "skip_metadata": [],
    "_tier": "next_tick"
  }
}
```

### `PATCH /api/settings/<section>`

Updates a single settings section partially. The request body is a JSON
object containing only the fields to change; missing fields are left
unchanged. Fields whose value is the redaction sentinel `"***"` are stripped
before applying, so a client may post back the full GET response without
overwriting secrets. Any other value for `auth.password_hash` or
`auth.session_secret` is rejected with `422`; use `POST /api/auth/password`
and `DELETE /api/auth/sessions`. Each field is type-checked against the
settings schema (for example `web.port` must be an integer, `schedule.paused`
a boolean, list fields arrays of strings); a wrong type is a `422`, never a
`500`. JSON arrays are coerced to tuples for the
`sync.include`, `sync.exclude`, `sync.skip_metadata`, and
`auth.trusted_proxies` fields so the in-memory dataclass remains tuple-typed
(JSON has no tuple).

**Request body example (`PATCH /api/settings/sync`):**

```json
{
  "priority": "rdate",
  "include": ["P", "NF"]
}
```

**Response (200 OK):**

```json
{"section": "sync", "tier": "next_tick", "applied": true}
```

**Error responses:**

- `400 Bad Request` -- `INVALID_BODY` when the request body is not a JSON object.
- `404 Not Found` -- `SECTION_NOT_FOUND` when `<section>` is not a known
  settings section.
- `422 Unprocessable Entity` -- `SETTINGS_INVALID` when the payload contains
  an unknown field or fails section-level validation. The `details.field_errors`
  array enumerates the failing paths and messages.

```json
{
  "error": "settings validation failed",
  "code": "SETTINGS_INVALID",
  "details": {
    "field_errors": [
      {"path": "sync", "message": "sync.priority must be one of ['date', 'rdate', 'type'], got 'bogus'"}
    ]
  }
}
```

---

## Auth API Endpoints

All endpoints below require authentication (subject to `auth.mode`).
CSRF protection applies to all `POST` and `DELETE` requests.

### `GET /api/auth/me`

Returns the current authenticated user and the active auth mode. The mode is
read fresh from the settings store on every request, so a mode change in
`/config/settings.json` takes effect immediately without a restart.

**Response (200 OK):**

```json
{"username": "admin", "mode": "login"}
```

### `POST /api/auth/password`

Changes the current user's password. Requires the current password as well
as the new password (minimum 12 characters). Failures consume the same
rate-limit bucket as `POST /login` (10 failures from the same IP within
600 seconds triggers a 15-minute lockout). Both fields must be strings
(`422 VALIDATION_ERROR` otherwise). On success every other session is signed
out; the caller's session is re-issued and stays valid.

**Request body:**

```json
{"current_password": "<old>", "new_password": "<new>"}
```

**Response (200 OK):**

```json
{"applied": true}
```

**Error responses:**

- `400 Bad Request` -- `INVALID_BODY` when the request body is not a JSON object.
- `401 Unauthorized` -- `INVALID_CURRENT_PASSWORD` when the current password
  does not match the stored hash; also increments the rate-limit bucket.
- `422 Unprocessable Entity` -- `WEAK_PASSWORD` when the new password is
  shorter than 12 characters.
- `429 Too Many Requests` -- `RATE_LIMITED` when the IP has exceeded the
  shared `/login` failure threshold.

### `DELETE /api/auth/sessions`

Rotates the session secret (`auth.session_secret`) to a fresh random value.
The running app switches to the new secret immediately, so every existing
session, including the caller's, is signed out without a restart.

**Response (200 OK):**

```json
{"rotated": true, "restart_required": false}
```

---

## Health API Endpoints

### `GET /api/health/storage`

Returns storage usage at the destination directory. Uses `shutil.disk_usage`
so the `used_percent` value matches what the sync engine sees for its
`max_used_disk_percent` threshold check (root-reserved blocks count as free).

```json
{
  "available": true,
  "destination": "/recordings",
  "total_bytes": 137438953472,
  "free_bytes": 50725394432,
  "used_bytes": 86713559040,
  "used_percent": 63.1,
  "recording_count": 481
}
```

When the destination does not exist on disk:

```json
{"available": false, "reason": "destination not configured"}
```

### `GET /api/health/dashcam`

HEAD-probes `http://<settings.connection.address>/blackvue_vod.cgi` with a
2-second timeout. The fixed timeout intentionally diverges from
`connection.timeout_seconds` so a slow dashcam does not block the dashboard.

Success:

```json
{"reachable": true, "address": "192.168.1.50", "latency_ms": 38.0}
```

Failure (`URLError` wrapping a timeout is classified as `reason: "timeout"`
for ui consistency with `socket.timeout`):

```json
{"reachable": false, "address": "192.168.1.50", "reason": "timeout"}
```

When no address is configured:

```json
{"reachable": false, "reason": "no address configured"}
```

---

## Dashcam API Endpoints

### `GET /api/dashcam/info`

Read-only inspection of the dashcam's on-camera configuration. Fetches
`http://<address>/Config/version.bin` and `http://<address>/Config/config.ini`
(BlackVue firmware is HTTP-only), parses them defensively, and returns
structured JSON. Changing settings is deliberately out of scope (a future
sub-project); this endpoint never writes to the camera.

Available (firmware may be null if version.bin was unreachable while
config.ini succeeded -- partial availability still reports available: true):

```json
{
  "available": true,
  "address": "192.168.1.50",
  "firmware": "DR900X-2.013",
  "config": {"Tab1": {"Resolution": "4K"}, "Tab3": {"Voice": "ON"}},
  "setting_count": 2
}
```

Unreachable or no address configured:

```json
{"available": false, "reason": "dashcam unreachable"}
```

```json
{"available": false, "reason": "no address configured"}
```

---

## Recordings API Endpoints

### `GET /api/recordings/recent`

Returns the N most recently modified BlackVue recordings at the destination
(matched via `filename_re.fullmatch`). Default `limit` is 5; clamped to
`[1, 50]` via query param `?limit=N`.

```json
{
  "recordings": [
    {
      "filename": "20231015_120000_NF.mp4",
      "mtime": 1697371200.0,
      "path": "/recordings/20231015_120000_NF.mp4"
    }
  ],
  "total": 1
}
```

---

## Schedule API Endpoints

### `POST /api/schedule/pause`

Sets `settings.schedule.paused = true`. The next scheduled sync is skipped
(the scheduler logs `scheduled sync skipped: schedule is paused`). Manual
`POST /api/sync/now` is unaffected -- operators can still trigger ad-hoc
syncs.

```json
{"paused": true}
```

### `POST /api/schedule/resume`

Sets `settings.schedule.paused = false`. Idempotent: returns 200 even when
the schedule was already running.

```json
{"paused": false}
```

---

## Logs API Endpoints

All endpoints below require authentication (subject to `auth.mode`).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/logs/recent` | JSON snapshot: `{lines, file_path, capacity, verbosity}` |
| `GET` | `/api/logs/stream` | SSE stream of new log lines (`event: logs`) |

### `GET /api/logs/recent`

Returns the current in-memory log buffer snapshot as JSON.

**Response (200 OK):**

```json
{
  "boot_id": "3f2a...",
  "lines": [
    {
      "seq": 1,
      "ts": "2026-01-01T12:00:00.000Z",
      "level": "INFO",
      "level_no": 20,
      "logger": "blackvuesync",
      "message": "sync started"
    }
  ],
  "file_path": "/recordings/blackvuesync.log",
  "capacity": 500,
  "verbosity": "normal"
}
```

`boot_id` identifies the server process. `seq` restarts at 1 in each process,
so a client that sees a new `boot_id` (or a lower `seq`) resets its position.

`file_path` is `""` (empty string) when no rotating file handler is active. `verbosity` reflects
the current `logging.verbose` / `logging.quiet` setting: `"quiet"` when quiet,
otherwise `"normal"` (verbose 0), `"verbose"` (1), or `"debug"` (2 or more).

### `GET /api/logs/stream`

Server-Sent Events (SSE) stream of new log lines.

**Response headers:**

```http
Content-Type: text/event-stream
Cache-Control: no-store
X-Accel-Buffering: no
```

**Event format:**

```text
event: logs
data: {"boot_id": "3f2a...", "lines": [...]}

```

The first frame contains the full current buffer snapshot (same lines as
`/api/logs/recent`). Subsequent frames contain only the new lines since the
previous emit. When no new lines arrive for 30 seconds a keepalive comment
is emitted:

```text
: keepalive

```

Each line object carries the same fields as in `/api/logs/recent`:
`{seq, ts, level, level_no, logger, message}`.

The viewer changes capture verbosity through the existing
`PATCH /api/settings/logging`; there is no dedicated verbosity endpoint.

---

## Statistics API Endpoints

All endpoints below require authentication (subject to `auth.mode`).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/stats/series?range=24h\|7d\|30d\|all` | JSON `{range, summary, series, forecast}` |

`summary` = `{runs, bytes, avg_duration_seconds, success_rate}`;
`series.points[]` = `{ts, bytes, files, duration, disk, success, dry_run, failures{reason}}`
(dry-run rows are excluded from the `bytes` total);
`forecast` = `{projected[{ts, disk}], limits{max_used_disk_percent, keep_steady_state}}`
(disk / limit values are 0..1 ratios). Per-run rows are captured in serve mode and
stored in the SQLite stats DB (`/config/stats.db`); `stats.retention_days` prunes
old rows.

---

## Viewer API

Login required. See `docs/reference/blackvue-file-formats.md` for the underlying
file formats. `viewer.journey_mode` / `viewer.speed_unit` settings tune the page.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/viewer/days` | `{"days": [{date, count}]}`, newest day first |
| GET | `/api/viewer/recordings?date=YYYY-MM-DD` | one day's recordings, newest first; without `date`, the newest day |
| GET | `/api/viewer/recordings/<base>_<type>/journey` | forward chain of contiguous segments |
| GET | `/api/viewer/recordings/<base>_<type>/gps` | `{"points": [{t, lat, lon, speed}]}` |
| GET | `/api/viewer/recordings/<base>_<type>/gsensor` | `{"samples": [{t, x, y, z}]}` |
| GET | `/media/<path>` | path-safe `.mp4`/`.thm` serving (HTTP Range) |

`/recordings` returns `{"days": [{"date", "recordings": [...]}]}` holding at
most one day, so a library of tens of thousands of recordings is never sent in
one response. An unknown date returns `{"days": []}`; a malformed one returns
`422` with code `INVALID_DATE`. The viewer page lists `/days` and loads a
day's recordings when it is opened.

All viewer endpoints read a shared in-memory index that is refreshed per
directory by modification time: a request costs one `stat` per directory and
re-lists only directories whose contents changed. Hidden directories and NAS
system directories (`@eaDir`, `#recycle`, `#snapshot`) are skipped.

---

## Settings UI (Sub-Project #3)

The `/settings` page added in Sub-Project #3 drives the existing
`GET /api/settings` and `PATCH /api/settings/<section>` endpoints for
all eleven sections (connection, schedule, sync, retention, logging,
metrics, stats, viewer, web, auth, system; `stats` and `viewer` arrived with
Sub-Projects #5 and #6). It also drives `POST /api/auth/password`
(change-password dialog) and `DELETE /api/auth/sessions` (rotate
sessions button). No new API endpoints were added in Sub-Project #3.

---

## Dashboard Controls (Phase 2C)

The dashboard UI added in Phase 2C drives Sync-now, Stop (modal-confirmed), and
Pause/Resume directly against the existing `/api/sync/*` and `/api/schedule/*`
endpoints listed in this document. No new endpoints were added in 2C. The
`dashboard.js` Alpine component opens an SSE subscription to
`/api/sync/progress/stream` on page load and reflects `SyncProgress.state` onto
`body[data-state]`; CSS drives the active/idle layout swap.

---

## Sync API Endpoints (additions)

### `POST /api/sync/stop`

Requests cooperative stop of the active sync. The download chunk loop in
`sync.py` checks the stop flag between chunks and raises
`UserWarning("sync stopped by user")` on its next boundary, which the
existing exception classifier routes through the normal `failed` exit path.
The partial `.filename.mp4` dotfile survives; the next sync resumes it
naturally.

Returns 202 when a sync was running:

```json
{"job_id": "deadbeef...", "stopping": true}
```

Returns 404 when no sync is active:

```json
{"error": "no sync is running", "code": "SYNC_NOT_RUNNING", "details": {}}
```

---

## HTMX Fragment Endpoints (additions)

Dashboard card fragments. Each renders the matching `_partials/*.html`
template, which includes an `hx-trigger` attribute so the card self-polls
once the dashboard embeds it.

- `GET /hx/storage-card` -- renders `_partials/storage_card.html` with the
  same data as `/api/health/storage`
- `GET /hx/dashcam-card` -- renders `_partials/dashcam_card.html` with the
  same data as `/api/health/dashcam`
- `GET /hx/next-scheduled-card` -- renders
  `_partials/next_scheduled_card.html` with the next cron fire time, paused
  flag, cron expression, and timezone
- `GET /hx/recent-activity-card` -- renders
  `_partials/recent_activity_card.html` with the same data as
  `/api/recordings/recent` (default `limit=5`)
- `GET /hx/dashcam-info-card` -- renders `_partials/dashcam_info_card.html`.
  Loads once on page load and refreshes every 60s (config is near-static and
  the fetch is two files, so it polls slower than the 5s cards).
