# CHANGELOG

<!-- --8<-- [start:changelog] -->
BlackVue Sync v2 follows [Semantic Versioning](https://semver.org/): a major
version changes when an upgrade needs action from you, a minor version adds
features, and a patch version only fixes bugs. Docker images are tagged with
the full version (`3.0.0`), the minor (`3.0`) and the major (`3`).

Versions 2.3.0 to 2.8.0a0 were developed as a fork of the upstream project at
`tekgnosis-net/blackvuesync`; their pull requests remain in that archived
repository. Versions 2.2.0 and earlier are the upstream
[BlackVue Sync](https://github.com/acolomba/blackvuesync) by Alessandro
Colomba; their numbers refer to upstream pull requests.

## 3.0.0 - unreleased

First release of **BlackVue Sync v2** as a standalone project. See
[Upgrading to 3.0.0](https://tekgnosis-net.github.io/blackvuesync-v2/guide/upgrading/#upgrading-to-300) before you
upgrade.

### Breaking changes

* The project, command, Python package and image are renamed so they no longer
  collide with the upstream `blackvuesync` package: repository
  `tekgnosis-net/blackvuesync-v2`, image `ghcr.io/tekgnosis-net/blackvuesync-v2`,
  command `blackvuesync-v2` (was `blackvuesync`), Python module
  `blackvuesync_v2` and distribution `blackvuesync-v2`. Environment variables (`BLACKVUESYNC_*`), `settings.json`, Prometheus metric
  names, the destination lock file and the recordings layout are unchanged.
* The web service now applies `retention.keep` (default `2w`). Earlier
  versions ignored it in scheduled syncs, so recordings older than the
  setting are deleted on the first sync after upgrading. Clear the setting to
  keep everything.
* Cron day-of-week numbers follow standard cron: `0` and `7` are Sunday.
  Earlier versions treated `0` as Monday, so numeric weekday schedules moved
  by one day.

### Added

* Documentation site at <https://tekgnosis-net.github.io/blackvuesync-v2/> with installation, configuration,
  command-line, upgrading and troubleshooting guides, a feature tour with
  screenshots, the HTTP API reference and these release notes.
* `scripts/screenshots.py` regenerates the documentation screenshots from a
  synthetic demo library.
* GitHub releases and semantic-version Docker tags. `scripts/release.py` prepares
  a release pull request and pushes the tag; `release.yml` publishes the release.

### Fixed

* The dashboard's **Last sync** card showed "no completed sync recorded" except
  for ten seconds after each run. It now shows the most recent run from the run
  history: how long ago, when (in the container's `TZ`), outcome, files and
  size.
* The metrics state file defaulted to `/config/metrics-state.json` even outside
  Docker and was written while metrics were disabled. It now defaults to
  `metrics-state.json` next to `settings.json` and is only used while metrics
  are enabled. Existing settings files are migrated (schema version 2); inside
  the Docker image the file stays in the same place.
* The web service now applies `retention.keep`, `sync.retry_failed_after`, `sync.skip_metadata` and `sync.affinity_key`; previously scheduled syncs ignored them, so old recordings were never deleted. An empty `keep` keeps recordings forever.
* Live progress and log streaming work under waitress; they returned 500 in production.
* A download cut short by the dashcam is kept as a partial and resumed, instead of being saved as complete.
* Fix a file-descriptor leak on every scheduled sync that exhausted the process after about 10 days.
* Security: `PATCH /api/settings/auth` can no longer overwrite the password hash or session secret; `X-Forwarded-For` is only honored with `BLACKVUESYNC_TRUST_PROXY`, so proxy auth and the login rate limiter can't be spoofed; no public fallback session key; password changes and session rotation sign out other sessions immediately; CSP drops `'unsafe-inline'`.
* Cron schedules follow standard cron: day-of-week `0`/`7` is Sunday (previously Monday), and day-of-month/day-of-week combine with OR. Invalid cron expressions and timezones are rejected, and a bad stored schedule falls back to `*/15 * * * *` UTC instead of crash-looping.
* Settings are type-checked (422 instead of 500); `keep`, `retry_failed_after` and include/exclude codes use the same parsers as the CLI.
* `BLACKVUESYNC_ADMIN_PASSWORD` now sets the admin password on first start.
* Progress counts files rather than recordings, reports already-downloaded files as skipped, and shows early failures such as an unreachable dashcam.
* Viewer: large libraries load. The sidebar lists days and loads a day's recordings when it is opened, thumbnails load lazily, and the server keeps a per-directory index instead of walking every file on each request. A 42,706-recording library previously sent 13.9 MB of JSON and 42,706 thumbnail requests at once and could crash Safari; it now shows the newest day in about 2 s (0.2 s once cached). NAS system folders (`#recycle`, `@eaDir`) are ignored. New `GET /api/viewer/days`; `GET /api/viewer/recordings` takes `?date=`.
* Viewer: recordings with `L`/`S` upload-flag filenames play; rear-only recordings play once; GPS timing no longer runs ahead of the video; `nan` speeds no longer produce invalid JSON.
* The web UI shows errors for failed actions, redirects to login when a session expires, and keeps working on pages open longer than an hour.
* Docker: `/config` is created and owned by the service user, so the container starts without a `/config` mount; waitress runs 32 threads; at most 16 live-update streams.

## 2.8.0a0

* Add recording viewer (`/viewer`): front/rear playback, GPS track on a map, G-sensor chart, and journey auto-advance. New `viewer` settings section (`journey_mode`, `speed_unit`). ([#21](https://github.com/tekgnosis-net/blackvuesync/pull/21))

## 2.7.0a0

* Add statistics page (`/stats`): per-run history in `/config/stats.db` and a disk-usage forecast. New `stats` settings section (`retention_days`). ([#20](https://github.com/tekgnosis-net/blackvuesync/pull/20))

## 2.6.0a0

* Add live log viewer (`/logs`) backed by an in-memory ring buffer and a rotating log file under `/config/logs/`. ([#19](https://github.com/tekgnosis-net/blackvuesync/pull/19))

## 2.5.0a0

* Add settings UI (`/settings`) covering all settings sections, password change, and session rotation. ([#18](https://github.com/tekgnosis-net/blackvuesync/pull/18))

## 2.4.0b0

* Add dashboard with live progress, Sync now, Stop, Pause/Resume, and storage, dashcam, next-run and recent-activity cards. ([#11](https://github.com/tekgnosis-net/blackvuesync/pull/11), [#12](https://github.com/tekgnosis-net/blackvuesync/pull/12), [#17](https://github.com/tekgnosis-net/blackvuesync/pull/17))
* Add read-only dashcam config info card. ([#12](https://github.com/tekgnosis-net/blackvuesync/pull/12))
* Resume interrupted downloads with HTTP range requests. ([#13](https://github.com/tekgnosis-net/blackvuesync/pull/13))
* Multi-stage Docker image; `uv` is no longer in the final image. ([#14](https://github.com/tekgnosis-net/blackvuesync/pull/14))
* Apply logging setting changes without a restart. ([#15](https://github.com/tekgnosis-net/blackvuesync/pull/15))

## 2.3.0

* Restructure as a package with `sync` and `serve` subcommands; `blackvuesync <address>` still ran a sync. ([#4](https://github.com/tekgnosis-net/blackvuesync/pull/4))
* Add `SettingsStore`: `/config/settings.json` (mode `0600`), seeded from env vars on first start, canonical afterwards. ([#5](https://github.com/tekgnosis-net/blackvuesync/pull/5))
* Add authentication: Argon2id passwords, first-run wizard, login rate limiting, and `login` / `none` / `proxy` modes. ([#6](https://github.com/tekgnosis-net/blackvuesync/pull/6))
* Add sync API with live progress over SSE. ([#7](https://github.com/tekgnosis-net/blackvuesync/pull/7))
* Add `serve`: Flask + waitress web service with an APScheduler-driven sync schedule. The `CRON` and `RUN_ONCE` env vars are retired; the image defaults to `serve` on port 8080. ([#8](https://github.com/tekgnosis-net/blackvuesync/pull/8))
* Add settings and auth APIs. ([#9](https://github.com/tekgnosis-net/blackvuesync/pull/9))
* Add structured JSON logs and Prometheus metrics export (upstream [#73](https://github.com/acolomba/blackvuesync/pull/73), [#74](https://github.com/acolomba/blackvuesync/pull/74)).

## 2.2.0

* Replace undocumented `--filter` with `--include` and `--exclude` options for filtering recordings by type and direction. Codes are comma-separated, direction is optional. ([#61](https://github.com/acolomba/blackvuesync/pull/61))
* Add `--retry-failed-after` option to retry failed downloads after a configurable delay. ([#58](https://github.com/acolomba/blackvuesync/pull/58))
* Add `--skip-metadata` option to skip downloading metadata files (thumbnails, accelerometer, GPS). ([#14](https://github.com/acolomba/blackvuesync/pull/14))
* Stream recording downloads in chunks to avoid buffering full files in memory.
* Close the lock file descriptor when lock acquisition fails and distinguish lock contention from other OS errors.
* Ensure lock descriptor `0` is always unlocked on exit.

## 2.1.1

* Switch to semver.

## 2.1

* Minor resource cleanup fix. ([#52](https://github.com/acolomba/blackvuesync/pull/52))

## 2.0

* Modernize for Python 3.9, now that it's available in Debian Bullseye oldoldstable, the earliest LTS-supported Debian release. Now uses type hints, f-strings; walrus operator.
* Logging uses lazy evaluation.
* Add initial Claude Code settings and AI contribution policy.
* Build Docker images for amd64, arm64, and armv7 architectures. ([#12](https://github.com/acolomba/blackvuesync/pull/12))
* Add support for 'O' (Optional) camera direction on DR770X Box Pro and similar models. (inspired by grysage/blackvuesync)
* Add support for DMS (Driver Monitoring System) recording types: D (Drowsiness), L (Distraction), Y (Seatbelt), F (Undetected).
* Publish to PyPi. Can be run with `uvx blackvuesync` without explicitly installing.
* Introduce integration tests for some features.

## 1.10 (2025-12-28)

* Add `--filter` option to filter which events are downloaded. ([#6](https://github.com/acolomba/blackvuesync/pull/6))
* Add support for the interior camera found on the DR750X-3CH. ([#7](https://github.com/acolomba/blackvuesync/pull/7))
* Download GPS data for all recording types. ([#9](https://github.com/acolomba/blackvuesync/pull/9))
* Flush logs on exit. ([#20](https://github.com/acolomba/blackvuesync/pull/20))
* Silence host/network down/unreachable and timeout in cron mode (inspired by [#23](https://github.com/acolomba/blackvuesync/pull/23)).
* Propagate exit status code to calling process. In cron mode, expected errors produce a success exit status.
* Add "rdate" priority, to download from newest to oldest.
* Upgrade alpine image to 3.23.2.

## 1.9 (2021-08-08)

* Properly removes outdated recordings with new event types and upload flags from May 2021 firmware. ([#4](https://github.com/acolomba/blackvuesync/pull/4))

## 1.8 (2021-05-24)

* Supports new event types produced by the May 2021 [BlackVue firmware update](https://blackvue.com/major-update-improved-blackvue-app-ui-dark-mode-live-event-upload-and-more/). ([#3](https://github.com/acolomba/blackvuesync/pull/3))
* The Docker image respects the KEEP option now.
* Docker compose file for a possibly quicker quickstart.
* Friendlier hardware requirement descriptions. ([#2](https://github.com/acolomba/blackvuesync/pull/2))
* More reliable removal of outdated directories when grouping by day, month or year.
* Better handling of unexpected 500 errors or remote disconnections.
* Upgraded docker image to alpine 3.13.5

## 1.7 (2019-07-14)

* Allows grouping recordings by date, with daily, weekly, monthly and yearly granularities.
* Docker image layers are more cacheable.
* Upgraded docker image to alpine 3.10.1

## 1.6 (2019-06-01)

* Logs file/recording download speed to help troubleshoot unreliable/slow Wi-Fi setups.
* Fixed a spurious error log during the first run after midnight.
* Does a better job at cleaning up temp files from interrupted downloads.
* Better handling of network errors while reading the file list.

## 1.5 (2019-03-03)

* Downloads .thm (thumbnail) files for all recordings.
* New ``--priority` switch allows downloading by either a) date or b) type (manual, event, normal, parking in that order.)
* Now downloads front and rear recordings together.

## 1.4 (2019-02-26)

* Downloads gps data for all but parking recording types, and accelerometer data for all.
* 500 errors while downloading are logged but ignored, so we don't get stuck on files we can't download.
* Tests that outdated gps/accelerometer files exist before deleting them, so it doesn't error out.

## 1.3 (2019-02-09)

* Removes gps data for outdated recordings along with the video.
* Gracefully handles low-level socket timeouts.

## 1.2 (2019-02-02)

* Removes temporary files upon successful completion.

## 1.1 (2019-02-01)

* No more sporadically getting stuck forever trying to connect to the dashcam.
* Connection timeout defaults to 10 seconds and is configurable.

## 1.0 (2019-01-30)

* initial release
<!-- --8<-- [end:changelog] -->
