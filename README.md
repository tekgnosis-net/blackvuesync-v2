# BlackVue Sync

[![CI](https://github.com/tekgnosis-net/blackvuesync/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/tekgnosis-net/blackvuesync/actions/workflows/ci.yml)
[![Build Docker image](https://github.com/tekgnosis-net/blackvuesync/actions/workflows/docker-build.yml/badge.svg?branch=main)](https://github.com/tekgnosis-net/blackvuesync/actions/workflows/docker-build.yml)

Synchronizes recordings from a BlackVue dashcam with a local directory over a LAN.

BlackVue dashcams expose an HTTP server that can be used to download all recordings. This project downloads only recordings that are not already downloaded, optionally limiting downloads in a local directory to a date range.

A typical setup would be a periodic cron job or a Docker container running on a local server.

## About this fork

BlackVue Sync was created by [Alessandro Colomba](https://github.com/acolomba)
and is developed at [acolomba/blackvuesync](https://github.com/acolomba/blackvuesync).
This repository is a fork of that project. Many thanks to Alessandro for
writing and maintaining BlackVue Sync since 2018, and to the upstream
contributors. The sync engine this fork runs on is their work: filename
parsing, the download loop, retention, locking, cron mode, structured logs and
Prometheus metrics.

The fork branched from upstream in May 2026 (upstream commit `7871a77`) and
has diverged since. Upstream changes made after that point are not included
here.

### What this fork adds

These ship from version 2.3.0 onward; see the [CHANGELOG](CHANGELOG.md).

* **Web service** (`blackvuesync serve`, the Docker default): runs syncs on a
  cron schedule inside one long-running process instead of an external cron
  job.
* **Authentication**: password login (Argon2id) with a first-run setup page,
  login handled by a reverse proxy, or none for trusted networks.
* **Dashboard**: live per-file progress, Sync now, Stop, and Pause/Resume of
  the schedule, plus storage, dashcam and recent-activity cards.
* **Settings editor**: all configuration lives in `/config/settings.json` and
  is edited in the browser. Environment variables only seed it on first start.
* **Log viewer**: live tail with level filtering and adjustable verbosity.
* **Statistics**: per-run history (bytes, files, duration, success rate) and a
  disk-usage forecast.
* **Recording viewer**: front and rear playback with the GPS track on a map, a
  G-sensor chart, and auto-advance through a journey.
* **Dashcam info**: read-only firmware and configuration details.
* **Byte-level download resume** with HTTP range requests: an interrupted file
  continues where it stopped instead of starting over.
* **Docker packaging**: `/config` volume, health check, and a multi-stage
  image for amd64 and arm64.
* **User guides** for installation, configuration, upgrading and
  troubleshooting.

The command line is unchanged: `blackvuesync <address> ...` behaves as it does
upstream, and recordings are stored with the same names and layout, so an
existing download directory can be used as is. Moving from the upstream Docker
image is covered in
[Upgrading](docs/guide/upgrading.md#migrating-from-the-cron-era-image-22x-and-earlier).

Report problems with this fork (web UI, `serve`, the
`ghcr.io/tekgnosis-net/blackvuesync` image) at
[tekgnosis-net/blackvuesync issues](https://github.com/tekgnosis-net/blackvuesync/issues),
not upstream.

## Documentation

* [Installation](docs/guide/installation.md): Docker, Compose and pip setup,
  first run, reverse proxies.
* [Configuration](docs/guide/configuration.md): every setting and
  environment variable.
* [Upgrading](docs/guide/upgrading.md): updates, rollback, and migrating from
  the cron-era image.
* [Troubleshooting](docs/guide/troubleshooting.md): common errors and fixes.
* [HTTP API](docs/api.md): endpoints used by the web UI.

## Features

* **Portable runtimes:**
  * A `blackvuesync sync` command whose sync core uses only the Python standard library. It can run [manually](#manual-usage) or [periodically](#unattended-usage).
  * A [docker image](#docker) that runs a long-running web service with an internal scheduler and a [web UI](#web-ui). Supports amd64 (Intel) and arm64 (Apple Silicon, Raspberry Pi 3+ on 64-bit OS).
* **Web UI**: Dashboard with live progress and sync/stop/pause controls, settings editor, live log viewer, statistics with a disk-usage forecast, and a recording viewer with GPS map and G-sensor chart.
* **Smart**: Only downloads recordings that haven't already been downloaded.
* **Resilient**: If a download interrupts for whatever reason, the script resumes where it left off the next time it runs. This is especially useful for possibly unreliable Wi-Fi connections from a garage.
* **Hands-off**: Optionally retains recordings for a set amount of time. Outdated recordings are automatically removed.
* **Cron-friendly**: Only one process is allowed to run at any given time for a specific download destination.
* **Safe**: Stops executing if the destination disk is almost full.
* **Friendly error reporting**: Communicates a range of known error conditions with sensible verbosity.

## Prerequisites

### Software

* [Python](https://www.python.org/) 3.9+ or [Docker](https://docs.docker.com/).
* Sufficient disk space on a file system local to the script. Plan for about 5GB/hr per camera.
* [BlackVue Viewer](https://blackvue.com/kr/download/) or a media player to view the recordings.

### Hardware

A cloud-enabled [BlackVue](https://www.blackvue.com/) dashcam must be connected via Wi-Fi to the local network with a *static* IP address.

The dashcam must be kept powered for some time after the vehicle is turned off. BlackVue offers [hardwiring kits](https://blackvue.com/product-tag/battery/) and [batteries](https://blackvue.com/product-tag/battery/).

The camera should stay active for a period sufficiently long for recordings to be downloaded. Consult the dashcam manual for the bit rate for your chosen image quality, and compare it with the download speed reported by BlackVue Sync.

Example with a DR750S-2CH recording with two cameras at the highest quality setting and a good but conservative download speed:

```calca
# dashcam bitrates
dashcam_bitrate_front = 12Mbps
dashcam_bitrate_back = 10Mbps
dashcam_bitrate = dashcam_bitrate_front + dashcam_bitrate_back

download_speed = 20Mbps

# hours on the timer for every hour of recording
ratio = dashcam_bitrate / download_speed => 1.1
```

### Verifying Connectivity

For illustration purposes, all examples assume that the camera is reachable at the `dashcam.example.net` address. A static numeric IP address works just as well.

A quick way to verify that the dashcam is online is by using `curl`.

```sh
$ curl http://dashcam.example.net/blackvue_vod.cgi
v:1.00
n:/Record/20181026_135003_PF.mp4,s:1000000
n:/Record/20181026_140658_PF.mp4,s:1000000
n:/Record/20181026_140953_PF.mp4,s:1000000
...
$
```

Another way is by browsing to: `http://dashcam.example.net/blackvue_vod.cgi`.

## Usage

### Installation

BlackVue Sync can be obtained in a number of ways:

* **[uv](https://docs.astral.sh/uv/)**: Run with `uvx --from git+https://github.com/tekgnosis-net/blackvuesync blackvuesync <args>`, or install with `uv tool install git+https://github.com/tekgnosis-net/blackvuesync` and run with `blackvuesync <args>`.
* **[Pip](https://pypi.org/project/pip/):** Install with `pip install "git+https://github.com/tekgnosis-net/blackvuesync"` and run with `blackvuesync <args>`.
* **From source:** Clone the repository and run `python3 -m blackvuesync_v2 <args>`.
* **GHCR:** The [Docker image](https://github.com/tekgnosis-net/blackvuesync/pkgs/container/blackvuesync) can be pulled with `docker pull ghcr.io/tekgnosis-net/blackvuesync`.

The interactive instructions assume a uv or Pip installation.

The `blackvuesync` package on PyPI (`pip install blackvuesync`,
`uvx blackvuesync`) is the upstream project, version 2.2.0. It does not include
this fork's web service; install from the Git URL above to get it.

### Manual Usage

`blackvuesync <address> ...` is shorthand for `blackvuesync sync <address> ...`.
`blackvuesync serve` starts the web service instead; see [Web UI](#web-ui).

The dashcam address is the only required parameter. The `--dry-run` option makes it so that the script communicates what it would do without actually doing anything. Example:

```sh
blackvuesync dashcam.example.net --dry-run
```

It's also possible to specify a destination directory other than the current directory with `--destination`:

```sh
blackvuesync dashcam.example.net --destination /data/dashcam --dry-run
```

A retention period can be indicated with the `--keep` option. Recordings prior to the retention period will be removed from the destination directory. Accepted units are `d` for days and `w` for weeks. If no unit is indicated, days are assumed.

```sh
blackvuesync dashcam.example.net --destination /data/dashcam --keep 2w --dry-run
```

A typical invocation would be:

```sh
blackvuesync dashcam.example.net --destination /data/dashcam --keep 2w
```

Other options:

* `--grouping`: Groups downloaded recordings in directories according to different schemes. Grouping speeds up loading recordings in the BlackVue Viewer app. The supported groupings are:
  * `daily`:  By day, e.g. 2018-10-26;
  * `weekly`: By week, with the directory indicating the date of that week's monday, e.g. 2018-10-22;
  * `monthly`: By month, e.g. 2018-10;
  * `yearly`: By year, e.g. 2018;
  * `none`: No grouping, the default.
* `--priority`: Downloads recordings with different priorities: `date` downloads oldest to newest; `rdate` downloads newest to oldest; `type` downloads manual, event (all types), normal and (non-event) parking recordings in that order. Defaults to `date`.
* `--max-used-disk`: Downloads stop once the specified used disk percentage threshold is reached. Defaults to `90` (i.e. 90%.)
* `--timeout`: Sets a timeout for establishing a connection to the dashcam, in seconds. Defaults to `10.0` seconds.
* `--retry-failed-after`: Sets the minimum elapsed time before retrying a failed download. Accepted units are `s` for seconds, `h` for hours, `d` for days and `w` for weeks. If no unit is indicated, days are assumed. Defaults to `1d`.
* `--skip-metadata`: Skips downloading metadata file types. Takes a string of characters: `t` for thumbnail (`.thm`), `3` for accelerometer (`.3gf`), `g` for GPS (`.gps`). For example, `--skip-metadata t3g` skips all metadata files, downloading only the `.mp4` video recordings.
* `--include`: Downloads only recordings matching the given codes. Each code is a recording type letter optionally followed by a camera direction letter, comma-separated. For example, `--include P,NF` downloads all Parking recordings and Normal Front recordings. See the table below for valid codes.
* `--exclude`: Excludes recordings matching the given codes, same format as `--include`. Takes priority over `--include`. For example, `--include N,E --exclude NR` downloads all Normal and Event recordings except Normal Rear.
* `--quiet`: Quiets down output messages, except for unexpected errors. Takes precedence over `--verbose`.
* `--verbose`: Increases verbosity. Can be specified multiple times to indicate additional verbosity.
* `--log-format`: Sets log output format. Supported values are `text` and `json`; defaults to `text`.
* `--metrics-file`: Writes Prometheus text format metrics to the given path.
* `--metrics-pushgateway-url`: Pushes Prometheus text format metrics to the given Pushgateway URL.
* `--metrics-job`: Sets the Pushgateway job grouping value. Defaults to `blackvuesync`.
* `--metrics-instance`: Sets the Pushgateway instance grouping value. Defaults to the dashcam address.
* `--metrics-state-file`: Persists cross-run metrics state at the given path. Defaults to `.blackvuesync.metrics-state.json` under the destination when metrics are enabled.

#### Recording type and direction codes

Recording type codes:

| Code | Type |
| ---- | ---- |
| N | Normal |
| E | Event |
| P | Parking |
| M | Manual |
| I | Impact |
| O | Overspeed |
| A | Acceleration |
| T | Cornering |
| B | Braking |
| R | Geofence (R) |
| X | Geofence (X) |
| G | Geofence (G) |
| D | DMS (D) |
| L | DMS (L) |
| Y | DMS (Y) |
| F | DMS (F) |

Direction codes:

| Code | Direction |
| ---- | --------- |
| F | Front |
| R | Rear |
| I | Interior |
| O | Optional |

### Unattended Usage

#### Plain cron

The script can run periodically by setting up a [cron](https://en.wikipedia.org/wiki/Cron) job on UNIX systems.

Simple example with crontab for a hypothetical `media` user:

```crontab
*/15 * * * * /home/media/bin/blackvuesync.py dashcam.example.net --keep 2w --destination /data/dashcam --cron
```

The `--cron` option changes the logging level with the assumption that the output may be emailed. When this option is enabled, the script only produces logs when it downloads recordings and when it encounters unexpected errors. One would typically see an email only after driving or when something goes wrong.

Note that if the dashcam is unreachable for whatever reason, in `--cron` mode no output is generated, since this is an expected condition whenever the dashcam is away from the local network.

If cron jobs overlap, the script recognizes that another instance is currently running via a lock file on the destination directory. For the lock to work correctly, the destination directory must be on a local filesystem relative to the script.

#### Prometheus metrics

BlackVueSync can emit Prometheus text format metrics at the end of each run.
For a host or Docker setup, write a metrics file that can be collected by
node_exporter's textfile collector:

```sh
blackvuesync dashcam.example.net --destination /data/dashcam --cron --metrics-file /var/lib/node_exporter/textfile_collector/blackvuesync.prom
```

For Kubernetes CronJob-style deployments, push the same metrics payload to a
Pushgateway:

```sh
blackvuesync dashcam.example.net --destination /data/dashcam --cron --metrics-pushgateway-url http://pushgateway.monitoring.svc:9091
```

Metrics are opt-in. When enabled, BlackVueSync persists the last successful file
pull timestamp in `.blackvuesync.metrics-state.json` under the destination
unless `--metrics-state-file` is set. Metrics delivery failures are logged as
warnings and do not replace the sync exit result.
Run-level failures such as dashcam index timeouts are exposed through
`blackvuesync_last_run_failure{reason="..."}`. Per-file failures are exposed
through `blackvuesync_file_download_failures_last_run{reason="..."}`.

Useful alert expressions include:

```promql
blackvuesync_last_run_success == 0
blackvuesync_last_run_failure{reason=~"network|timeout"} == 1
time() - blackvuesync_last_run_timestamp_seconds > 3600
time() - blackvuesync_last_successful_file_pull_timestamp_seconds > 86400
sum(max_over_time(blackvuesync_file_download_failures_last_run[1h])) > 0
```

#### NAS

Many NAS systems allow running commands periodically at set intervals.

##### openmediavault

The [openmediavault](http://www.openmediavault.org/) NAS solution allows running [scheduled jobs](https://openmediavault.readthedocs.io/en/latest/administration/general/cron.html) with support for mail notifications.

Example:

![openmediavault Scheduled Job](https://raw.githubusercontent.com/tekgnosis-net/blackvuesync/main/docs/images/cron-example-openmediavault.png)

#### Web UI

BlackVue Sync ships a built-in web interface accessible at
`http://host:8080/` when running under Docker (or via `blackvuesync serve`).

Pages:

* **Dashboard** (`/`): sync status with live per-file progress, Sync now, Stop,
  and Pause/Resume of the schedule, plus storage, dashcam, next-run and recent
  activity cards.
* **Settings** (`/settings`): edits every section of `settings.json`, changes
  the admin password, and rotates sessions (signs everyone out).
* **Logs** (`/logs`): live tail of the service log with level filtering.
* **Statistics** (`/stats`): per-run history (bytes, files, duration, success
  rate) over 24h/7d/30d/all, and a disk-usage forecast. Run history is stored
  in `/config/stats.db`.
* **Viewer** (`/viewer`): plays downloaded front and rear recordings together,
  with the GPS track on a map, a G-sensor chart, and auto-advance through
  contiguous segments.

##### First-Run Wizard

On the very first visit (or any time `auth.password_hash` is empty), the
browser is redirected to `/first-run`. Enter a password of at least 12
characters to complete setup. The hash (Argon2id) is stored in
`/config/settings.json` and the redirect disappears. Setting
`BLACKVUESYNC_ADMIN_PASSWORD` (12+ characters) before the first start sets the
password up front and skips this page. Until a password is set, anyone who
can reach the port can claim the admin account.

##### Auth Modes

Three authentication modes are available (Settings page, Auth section, or
`auth.mode` in `settings.json`):

| Mode | Behavior |
| --- | --- |
| `login` | Password authentication (default). Session cookie valid for the configured lifetime. |
| `none` | No authentication required. Suitable for trusted LAN access where no admin password is desired. |
| `proxy` | A reverse proxy handles authentication. BlackVue Sync trusts the user named in `auth.proxy_user_header` (default `X-Remote-User`) on requests whose TCP peer address is in `auth.trusted_proxies` (IPs or CIDRs). |

A mode change takes effect on the next request without a restart.

##### Reverse Proxy (Caddy Example)

```caddyfile
blackvuesync.example.net {
    reverse_proxy localhost:8080
}
```

Set `BLACKVUESYNC_TRUST_PROXY=1` in the container environment (or process
environment) when deploying behind an HTTPS reverse proxy. This marks the
session cookie `Secure` (HTTPS only) and makes the service honor the proxy's
`X-Forwarded-For` / `X-Forwarded-Proto` headers. Leave it unset when clients
connect to port 8080 directly, since they could then spoof those headers.

See [docs/guide/installation.md](docs/guide/installation.md#putting-it-behind-a-reverse-proxy)
for nginx and proxy-auth examples.

##### Recovery

If you lose access to the admin password, set `auth.password_hash` to `""`
in `/config/settings.json` and restart the container (or the `serve` process).
The first-run wizard will prompt for a new password.

#### Docker

##### Overview

The [ghcr.io/tekgnosis-net/blackvuesync](https://github.com/tekgnosis-net/blackvuesync/pkgs/container/blackvuesync) docker image runs the long-running web service that schedules sync operations internally.

Sync is now scheduler-driven inside the long-running web service. The `CRON` and `RUN_ONCE` environment variables of the cron-era image have been retired. To trigger an on-demand sync, use **Sync now** on the dashboard (or POST to `/api/sync/now`). To change the schedule, edit **Schedule** on the Settings page, or `schedule.cron_expression` in `settings.json` (default `*/15 * * * *`, evaluated in `schedule.timezone`, default `UTC`).

##### Quick Start

It's a good idea to do a single, interactive dry run first with verbose logging.
The image's default CMD is `serve`, which starts the long-running web service.
Overriding the CMD with `sync ...` runs one sync attempt and exits, which is
ideal for smoke-testing:

```sh
docker run -it --rm \
    -e ADDRESS=dashcam.example.net \
    -v $PWD:/recordings \
    --name blackvuesync \
    ghcr.io/tekgnosis-net/blackvuesync \
    sync --dry-run --verbose dashcam.example.net --destination /recordings
```

Once that works, a typical invocation would be similar to:

```sh
docker run -d --restart unless-stopped \
    -p 8080:8080 \
    -v /data/dashcam:/recordings \
    -v /data/blackvuesync-config:/config \
    -e ADDRESS=dashcam.example.net \
    -e PUID=$(id -u) \
    -e PGID=$(id -g) \
    -e TZ="America/New_York" \
    -e BLACKVUESYNC_TIMEZONE="America/New_York" \
    -e KEEP=2w \
    --name blackvuesync \
ghcr.io/tekgnosis-net/blackvuesync
```

Then open `http://<host>:8080/` and set the admin password in the first-run
wizard.

##### Reverse Proxy

The Flask service inside the container listens on HTTP port 8080; HTTPS
should terminate at a reverse proxy. A minimal [Caddy](https://caddyserver.com/)
configuration:

```caddyfile
blackvuesync.example.com {
    encode zstd gzip
    reverse_proxy localhost:8080
}
```

Set `BLACKVUESYNC_TRUST_PROXY=1` in the container environment so the session
cookie is marked `Secure` (see the [Web UI Reverse Proxy](#reverse-proxy-caddy-example)
section above for details).

##### Docker Compose

[Docker Compose](https://docs.docker.com/compose/) may offer an easier, more repeatable and extensible option for running a BlackVueSync Docker container.

After downloading the Docker [Compose file](https://raw.githubusercontent.com/tekgnosis-net/blackvuesync/main/docker-compose.yml) and editing its values as desired, BlackVueSync can be started with:

```sh
docker-compose up -d
```

OR, depending on the Docker version:

```sh
docker compose up -d
```

##### Settings File

On first start, the container creates `/config/settings.json` (mode `0600`)
from the environment variables listed below. From that point on, the file is
canonical: environment variables are ignored on subsequent starts. To change
a setting after first run, either:

* Use the Settings page in the web UI. Most changes apply immediately or on
  the next scheduled sync; connection, web and system changes need a restart.
* Edit `/config/settings.json` directly and restart the container.
* Delete `/config/settings.json` and restart; the container re-bootstraps from
  the current environment variables.

The `/config` volume is required for the settings file to persist across
container restarts. Mount it alongside `/recordings`:

```sh
docker run -d --restart unless-stopped \
    -v /data/dashcam:/recordings \
    -v /data/config:/config \
    -e ADDRESS=dashcam.example.net \
    ...
```

##### Recovery

If you lose access to the admin password, edit `auth.password_hash` to `""` in `/config/settings.json` and
restart the container. The first-run wizard will prompt for a new password.

##### Reference

These options are required for the docker image to operate correctly:

* The `/recordings` volume mapped to the desired destination of the downloaded recordings.
* The `ADDRESS` parameter set to the dashcam address.
* The `PUID` and `PGID` parameters set to the desired destination directory's user id and group id.
* The `TZ` parameter set to the same [timezone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones) as the dashcam. Note that BlackVue dashcams do not respect Daylight Savings Time, so their clock needs to be adjusted periodically.

Recommended:

* The `/config` volume, which holds `settings.json`, `stats.db` and `logs/`.
* Port `8080` published for the web UI.

Web service parameters (first start only, like all parameters below):

* `BLACKVUESYNC_SCHEDULE`: Cron expression for scheduled syncs. (Default: `*/15 * * * *`.)
* `BLACKVUESYNC_TIMEZONE`: Timezone the schedule is evaluated in. `TZ` is not used for this. (Default: `UTC`.)
* `BLACKVUESYNC_PORT`: Web UI port inside the container. (Default: `8080`.)
* `BLACKVUESYNC_ADMIN_USERNAME`: Admin username. (Default: `admin`.)
* `BLACKVUESYNC_ADMIN_PASSWORD`: Admin password, at least 12 characters. Hashed into `settings.json` on first start so the first-run page is skipped; remove it from the environment afterwards. (Default: empty, meaning the first-run page asks for one.)
* `STATS_RETENTION_DAYS`: Days of per-run statistics to keep; `0` keeps all. (Default: `365`.)

Read on every start (not stored in `settings.json`):

* `BLACKVUESYNC_TRUST_PROXY`: Set to `1` behind an HTTPS reverse proxy: marks the session cookie `Secure` and honors `X-Forwarded-*` headers from one proxy hop.
* `BLACKVUESYNC_CONFIG_PATH`: Alternate location of `settings.json`. (Default: `/config/settings.json`.)

Sync parameters:

* `GROUPING`: Groups downloaded recordings in directories, `daily`, `weekly`, `monthly`, `yearly` and `none` are supported. (Default: `none`.)
* `KEEP`: Sets the retention period of downloaded recordings. Recordings prior to the retention period will be removed from the destination. Accepted units are `d` for days and `w` for weeks. If no unit is indicated, days are assumed. (Default: `2w` when unset or empty. To keep recordings forever, clear **Retention → Keep recordings for** in the web UI.)
* `PRIORITY`: Sets the priority to download recordings. Pick `date` to download from oldest to newest; pick `rdate` to download from newset to oldest; pick `type` to download manual, event (all types), normal and (non-event) parking recordings in that order. Defaults to `date`.
* `MAX_USED_DISK`: If set to a percentage value, stops downloading if the amount of used disk space exceeds the indicated percentage value.  (Default: `90`, i.e. 90%.)
* `TIMEOUT`: If set to a float value, sets the timeout in seconds for connecting to the dashcam. (Default: `10.0` seconds.)
* `RETRY_FAILED_AFTER`: If set, sets the minimum elapsed time before retrying a failed download. Accepted units are `s` for seconds, `h` for hours, `d` for days and `w` for weeks. If no unit is indicated, days are assumed. (Default: `1d`.)
* `VERBOSE`: If set to a number greater than zero, increases logging verbosity. (Default: `0`.)
* `SKIP_METADATA`: If set, skips downloading the indicated metadata file types. Takes a string of characters: `t` for thumbnail (`.thm`), `3` for accelerometer (`.3gf`), `g` for GPS (`.gps`). For example, `t3g` skips all metadata files. (Default: empty.)
* `INCLUDE`: If set, downloads only recordings matching the given codes. Each code is a recording type letter optionally followed by a camera direction letter, comma-separated. For example, `P,NF` downloads all Parking recordings and Normal Front recordings. (Default: empty, meaning all recordings are downloaded.)
* `EXCLUDE`: If set, excludes recordings matching the given codes, same format as `INCLUDE`. Takes priority over `INCLUDE`. For example, setting `INCLUDE=N` and `EXCLUDE=NR` downloads all Normal recordings except Normal Rear. (Default: empty.)
* `QUIET`: If set to `1`, `true` or `yes`, quiets down logs: only unexpected errors will be logged. (Default: empty.)
* `LOG_FORMAT`: If set, changes log output format. Supported values are `text` and `json`. (Default: empty, meaning `text`.)
* `METRICS_FILE`: If set, writes Prometheus text format metrics to this path. (Default: empty.)
* `METRICS_PUSHGATEWAY_URL`: If set, pushes Prometheus text format metrics to this Pushgateway URL. (Default: empty.)
* `METRICS_JOB`: Sets the Pushgateway job grouping value. (Default: `blackvuesync`.)
* `METRICS_INSTANCE`: Sets the Pushgateway instance grouping value. (Default: empty, meaning the dashcam address.)
* `METRICS_STATE_FILE`: If set, stores cross-run metrics state at this path. (Default: `/config/metrics-state.json`.)
* `AFFINITY_KEY`: Test harness only; leave unset. (Default: empty.)

`DRY_RUN` is not read by the web service. Enable dry run with the **Dry run**
toggle on the Settings page (System section), or run a one-off
`sync --dry-run` as shown in [Quick Start](#quick-start).

## License

This project is licensed under the MIT License - see the [COPYING](COPYING) file for details

Copyright 2018-2026 [Alessandro Colomba](https://github.com/acolomba)

Fork additions copyright 2026 [tekgnosis-net](https://github.com/tekgnosis-net),
under the same MIT license.
