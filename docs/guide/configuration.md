# Configuration

The web service keeps all of its configuration in one file,
`/config/settings.json` (or the path given by `--config-path` /
`BLACKVUESYNC_CONFIG_PATH`). Edit it through **Settings** in the web UI.

## How settings are stored

* **First start:** the file does not exist, so it is created from environment
  variables (see [below](#environment-variables)). Missing variables get the
  defaults listed here.
* **Every later start:** the file is read and environment variables are
  ignored. Changing `KEEP=` in your compose file after the first start does
  nothing; change it in the UI instead.
* The file is written atomically with mode `0600`. The service refuses to
  start if the file is readable by group or others; fix with
  `chmod 600 settings.json`.
* Invalid values are rejected by the UI with a message next to the field.
  If you edit the file by hand and a value is invalid, a warning is logged at
  startup.

### When a change takes effect

| Sections | Takes effect |
| --- | --- |
| logging, metrics, viewer, auth | immediately |
| schedule, sync, retention, stats | on the next sync run |
| connection, web, system | after a restart |

Restart with `docker compose restart blackvuesync-v2` (or `docker restart
blackvuesync-v2`).

## Settings reference

### Connection

| Field | Default | Description |
| --- | --- | --- |
| `address` | *(empty)* | Dashcam IP address or hostname. Syncs fail until this is set. |
| `timeout_seconds` | `10.0` | Connection timeout. Raise it on weak Wi-Fi. |

### Schedule

| Field | Default | Description |
| --- | --- | --- |
| `cron_expression` | `*/15 * * * *` | Standard 5-field cron: minute, hour, day of month, month, day of week (0 or 7 = Sunday; names like `mon` also work). |
| `timezone` | `UTC` | IANA timezone the schedule runs in, e.g. `Europe/London`. |
| `paused` | `false` | Skip scheduled runs. **Sync now** still works. Also toggled by Pause/Resume on the dashboard. |

Examples: `*/10 * * * *` every 10 minutes; `*/5 17-23 * * *` every 5 minutes
in the evening; `0 * * * 1-5` hourly on weekdays.

### Sync

| Field | Default | Description |
| --- | --- | --- |
| `priority` | `date` | `date` oldest first, `rdate` newest first, `type` manual, then events, then normal, then parking. |
| `grouping` | `none` | `daily`, `weekly`, `monthly`, `yearly` subdirectories, or `none`. |
| `include` | *(all)* | Only download these codes. A code is a type letter, optionally followed by a direction letter: `P` all parking, `NF` normal front. |
| `exclude` | *(none)* | Skip these codes. Wins over `include`. |
| `retry_failed_after` | `1d` | Wait this long before retrying a file that failed. Units `s`, `h`, `d`, `w`. |
| `skip_metadata` | *(none)* | Don't download `t` thumbnails, `3` accelerometer, `g` GPS files. The viewer needs `g` for the map and `3` for the G-sensor chart. |
| `affinity_key` | *(empty)* | Test harness only: sent as an `X-Affinity-Key` header. Leave empty. |

Type codes: `N` normal, `E` event, `P` parking, `M` manual, `I` impact,
`O` overspeed, `A` acceleration, `T` cornering, `B` braking, `R`/`X`/`G`
geofence, `D`/`L`/`Y`/`F` driver monitoring. Direction codes: `F` front,
`R` rear, `I` interior, `O` optional.

### Retention

| Field | Default | Description |
| --- | --- | --- |
| `keep` | `2w` | Delete downloaded recordings older than this. Units `d`, `w` (no unit means days). Empty keeps everything. |
| `max_used_disk_percent` | `90` | Stop downloading when the destination disk is this full. |

Retention runs at the start of each sync. With a dry run it only logs what it
would delete.

### Logging

| Field | Default | Description |
| --- | --- | --- |
| `verbose` | `0` | `0` normal, `1` verbose, `2` debug. |
| `quiet` | `false` | Errors only. Wins over `verbose`. |
| `format` | `text` | `text` or `json` (for log shippers). |
| `file_max_bytes` | `10485760` | Size of `/config/logs/blackvuesync.log` before it rotates. |
| `file_backup_count` | `5` | Rotated log files kept. |
| `ring_buffer_capacity` | `1000` | Lines kept in memory for the Logs page. |

### Metrics

| Field | Default | Description |
| --- | --- | --- |
| `file` | *(empty)* | Write Prometheus metrics to this path after every run. |
| `pushgateway_url` | *(empty)* | Push metrics to this Pushgateway. |
| `job` | `blackvuesync` | Pushgateway job label. |
| `instance` | *(dashcam address)* | Pushgateway instance label. |
| `state_file` | *(empty)* | Where the last successful download time is remembered across restarts. Empty means `metrics-state.json` next to `settings.json`. Only used while metrics are enabled. |

### Stats

| Field | Default | Description |
| --- | --- | --- |
| `retention_days` | `365` | Days of run history kept in `/config/stats.db`. `0` keeps everything. |

### Viewer

| Field | Default | Description |
| --- | --- | --- |
| `journey_mode` | `progressive` | How map and G-sensor data for a multi-segment journey loads: `progressive` adds each segment's track as playback reaches it; `full` loads the whole journey's track when you select it. |
| `speed_unit` | `kmh` | `kmh` or `mph`. |

### Web

| Field | Default | Description |
| --- | --- | --- |
| `port` | `8080` | Port inside the container. If you change it, change the port mapping too. |
| `session_lifetime_hours` | `24` | How long a login lasts. |

### Auth

| Field | Default | Description |
| --- | --- | --- |
| `mode` | `login` | `login` password, `proxy` trust a reverse proxy header, `none` no authentication. |
| `username` | `admin` | Admin user name. |
| `trusted_proxies` | *(none)* | `proxy` mode only: proxy IPs or CIDRs, one per line. |
| `proxy_user_header` | `X-Remote-User` | `proxy` mode only: header carrying the user name. |

The password hash and session secret are also stored here but are not shown
or editable in the settings form. Use **Change password** and **Sign out all
sessions** in the Auth section.

### System

| Field | Default | Description |
| --- | --- | --- |
| `destination` | `/recordings` | Where recordings are stored. In Docker, leave it and map a volume to `/recordings`. |
| `dry_run` | `false` | Log what would be downloaded or deleted without doing it. |

## Environment variables

### Read only on first start (seed `settings.json`)

| Variable | Setting |
| --- | --- |
| `ADDRESS` | `connection.address` |
| `TIMEOUT` | `connection.timeout_seconds` |
| `BLACKVUESYNC_SCHEDULE` | `schedule.cron_expression` |
| `BLACKVUESYNC_TIMEZONE` | `schedule.timezone` |
| `PRIORITY` | `sync.priority` |
| `GROUPING` | `sync.grouping` |
| `INCLUDE`, `EXCLUDE` | `sync.include`, `sync.exclude` (comma-separated) |
| `RETRY_FAILED_AFTER` | `sync.retry_failed_after` |
| `SKIP_METADATA` | `sync.skip_metadata` (e.g. `t3g`) |
| `AFFINITY_KEY` | `sync.affinity_key` |
| `KEEP` | `retention.keep` |
| `MAX_USED_DISK` | `retention.max_used_disk_percent` |
| `VERBOSE`, `QUIET`, `LOG_FORMAT` | `logging.*` (`QUIET` accepts `1`, `true`, `yes`) |
| `METRICS_FILE`, `METRICS_PUSHGATEWAY_URL`, `METRICS_JOB`, `METRICS_INSTANCE`, `METRICS_STATE_FILE` | `metrics.*` |
| `STATS_RETENTION_DAYS` | `stats.retention_days` |
| `BLACKVUESYNC_PORT` | `web.port` |
| `BLACKVUESYNC_ADMIN_USERNAME` | `auth.username` |
| `BLACKVUESYNC_ADMIN_PASSWORD` | Hashed into `auth.password_hash` if at least 12 characters; skips the first-run page. Remove it from your compose file afterwards. |

`DRY_RUN` is not read by the web service; use **System → Dry run**. `CRON`
and `RUN_ONCE` from older images are ignored and only log a warning.

### Read on every start

| Variable | Purpose |
| --- | --- |
| `PUID`, `PGID` | Docker only. User and group the service runs as; must be able to write `/recordings` and `/config`. |
| `TZ` | Docker only. Container clock timezone, used for log timestamps and retention dates. Set it to the dashcam's timezone. |
| `BLACKVUESYNC_TRUST_PROXY` | `1` behind an HTTPS reverse proxy. See [installation](installation.md#putting-it-behind-a-reverse-proxy). |
| `BLACKVUESYNC_CONFIG_PATH` | Alternate settings file path. |

## Files under `/config`

| Path | Contents |
| --- | --- |
| `settings.json` | All settings, including the password hash and session secret. Keep it private. |
| `stats.db` | SQLite run history for the Statistics page. Safe to delete; history restarts. |
| `logs/blackvuesync.log*` | Rotating service log. |
| `metrics-state.json` | Metrics state, only when metrics are enabled. |

In the recordings directory the service also keeps `.blackvuesync.lock` (stops
two syncs running at once) and hidden partial-download dotfiles.
