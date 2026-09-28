# Upgrading

## Before any upgrade

Back up `/config`. It holds your settings, password hash and run history;
recordings are not touched by upgrades.

```sh
docker compose stop blackvuesync
tar czf blackvuesync-config-$(date +%F).tgz -C ~/blackvuesync config
docker compose start blackvuesync
```

Read the [CHANGELOG](../../CHANGELOG.md) for the versions between yours and
the new one. Your running version is shown by:

```sh
docker exec blackvuesync python -m blackvuesync_v2 --version
```

## Docker Compose

```sh
cd ~/blackvuesync
docker compose pull
docker compose up -d
docker compose logs -f blackvuesync
```

Check for `starting web server` in the log, then open the dashboard.

`latest` follows the main branch. To stay on a release, pin a version tag
instead, e.g. `image: ghcr.io/tekgnosis-net/blackvuesync:2.8.0`, and change
the tag when you want to upgrade.

A sync that is running when the container stops is interrupted. The partial
file is kept and resumed on the next run.

## `docker run`

```sh
docker pull ghcr.io/tekgnosis-net/blackvuesync:latest
docker stop blackvuesync && docker rm blackvuesync
docker run -d --name blackvuesync ...   # same options as before
```

Keep the same `-v ...:/config` mount so the settings carry over.

## pip / uv

```sh
~/blackvuesync-venv/bin/pip install --force-reinstall --no-deps "git+https://github.com/tekgnosis-net/blackvuesync"
# or
uv tool install --reinstall "git+https://github.com/tekgnosis-net/blackvuesync"
```

The version number does not change on every commit, so
`pip install --upgrade` and `uv tool upgrade` can report that everything is
up to date and keep the old code. The commands above always fetch the latest
commit.

Restart the `serve` process afterwards.

## Settings across versions

* New settings added by an upgrade appear with their default values; nothing
  needs to be done.
* The environment variables in your compose file are **not** re-read. If a
  release notes a new variable, set the matching field in the web UI instead.
* The settings file carries a schema `version`. When the format changes, the
  file is migrated automatically on first start.

## Rolling back

1. Stop the container.
2. Restore the `/config` backup taken before the upgrade. An older version
   drops settings it does not know about the next time it saves, so restoring
   the backup is safer than reusing the newer file.
3. Pin the previous image tag and start the container.

## Upgrading to the release after 2.8.0a0

This release fixes a set of bugs found in review (see the
[CHANGELOG](../../CHANGELOG.md)). After upgrading:

* **Log in again.** Existing sessions are signed out once.
* **Check retention.** Earlier versions of the web service ignored
  **Retention → Keep recordings for**. It now applies on the next sync, so
  recordings older than that setting (default `2w`) are deleted. Clear the
  field first if you want to keep everything, or turn on **System → Dry run**
  for one run to see what would be removed.
* **Check the schedule if it names weekdays by number.** Day-of-week `0` now
  means Sunday, as in standard cron. A schedule such as `0 3 * * 1-5` used to
  run Tuesday to Saturday and now runs Monday to Friday.
* **Check include/exclude.** They must be type or type+direction codes such as
  `P` or `NF`. Other values are rejected when you save.
* **Skip metadata and retry settings** now apply to scheduled syncs.

## Migrating from the cron-era image (2.2.x and earlier)

Older images ran a cron job inside the container and had no web UI. The
current image runs a web service with its own scheduler.

What changes:

| Before | Now |
| --- | --- |
| `CRON` env var | Retired. Set the schedule under **Settings → Schedule** (default every 15 minutes). |
| `RUN_ONCE` env var | Retired. Use **Sync now** on the dashboard, or run a one-off `sync` command (below). |
| Env vars read on every start | Env vars seed `/config/settings.json` once; afterwards the file is used. |
| No ports | Web UI on port 8080. |
| Only `/recordings` volume | Also mount `/config`. |
| `DRY_RUN` env var | Use **System → Dry run** in the UI. |

Steps:

1. Stop and remove the old container. Recordings in `/recordings` stay where
   they are and are not downloaded again.
2. Add a `/config` volume and publish port 8080 (see
   [installation.md](installation.md#option-1-docker-compose-recommended)).
   Keep your existing env vars; on this first start they are copied into
   `settings.json`. `KEEP` now defaults to `2w` if unset; set `KEEP=` to a
   larger value, or clear **Retention → Keep** in the UI afterwards, if you
   relied on keeping everything.
3. Add `BLACKVUESYNC_TIMEZONE` with the same value as `TZ` so the schedule
   runs in local time.
4. Start the container and complete the [first run](installation.md#first-run).
5. Check **Settings** once; from now on change settings there, not in the
   compose file.

A one-off sync, without the web service, still works by overriding the
command:

```sh
docker run --rm -v /data/dashcam:/recordings \
    ghcr.io/tekgnosis-net/blackvuesync \
    sync 192.168.1.50 --destination /recordings --dry-run --verbose
```

### Plain cron installs

If you run `blackvuesync` from the host's crontab and installed it from PyPI,
you have the upstream package; `pip install "git+https://github.com/tekgnosis-net/blackvuesync"` replaces it with this
fork. Either way, upgrading changes nothing about the cron job: `blackvuesync <address> ...` still runs one sync
with the same flags. The web service is only started by `blackvuesync serve`.
Do not run cron syncs and `serve` against the same destination at the same
time; the lock file makes one of them skip, but the web UI will not show cron
runs.
