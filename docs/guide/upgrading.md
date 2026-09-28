# Upgrading

## Before any upgrade

Back up `/config`. It holds your settings, password hash and run history;
recordings are not touched by upgrades.

```sh
docker compose stop blackvuesync-v2
tar czf blackvuesync-v2-config-$(date +%F).tgz -C ~/blackvuesync-v2 config
docker compose start blackvuesync-v2
```

Read the [release notes](../release-notes.md) for the versions between yours
and the new one. A new **major** version (3 → 4) always lists what you need to
check. Your running version is shown in the web interface's footer, or by:

```sh
docker exec blackvuesync-v2 python -m blackvuesync_v2 --version
```

## Choosing an image tag

| Tag | Gets |
| --- | --- |
| `:3` | Every 3.x release: new features and fixes, never a breaking change. **Recommended.** |
| `:3.0` | 3.0.x bug-fix releases only. |
| `:3.0.0` | Exactly that release. |
| `:latest` | Every commit to the main branch, including unreleased changes. |

## Docker Compose

```sh
cd ~/blackvuesync-v2
docker compose pull
docker compose up -d
docker compose logs -f blackvuesync-v2
```

Check for `starting web server` in the log, then open the dashboard. To move
to a new major version, change the tag in `docker-compose.yml` first.

A sync that is running when the container stops is interrupted. The partial
file is kept and resumed on the next run.

## `docker run`

```sh
docker pull ghcr.io/tekgnosis-net/blackvuesync-v2:3
docker stop blackvuesync-v2 && docker rm blackvuesync-v2
docker run -d --name blackvuesync-v2 ...   # same options as before
```

Keep the same `-v ...:/config` mount so the settings carry over.

## pip / uv

Install the release tag you want:

```sh
~/blackvuesync-v2-venv/bin/pip install "git+https://github.com/tekgnosis-net/blackvuesync-v2@v3.0.0"
# or
uv tool install --reinstall "git+https://github.com/tekgnosis-net/blackvuesync-v2@v3.0.0"
```

Each release has its own version number, so installing a newer tag replaces
the old one. Installing from the main branch (no `@tag`) needs
`pip install --force-reinstall --no-deps …`, because the version number only
changes at releases.

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

## Upgrading to 3.0.0

3.0.0 is the first release of BlackVue Sync v2 as its own project. It renames
the image and the command, and it fixes several bugs that change what a
scheduled sync does.

### From `ghcr.io/tekgnosis-net/blackvuesync` (versions 2.3 to 2.8)

The earlier image name no longer receives updates.

1. Back up `/config` (above).
2. In `docker-compose.yml`, change the image:

    ```yaml
    image: ghcr.io/tekgnosis-net/blackvuesync-v2:3
    ```

    Keep the volumes and environment as they are; `settings.json`, the stats
    database and the logs are used unchanged. Renaming the service or
    container to `blackvuesync-v2` is optional.

3. `docker compose pull && docker compose up -d`.
4. Log in again: existing sessions are signed out once.
5. Check these settings, whose behaviour changed:
    * **Retention → Keep recordings for.** Earlier versions of the web service
      ignored it; it now applies on the next sync, so recordings older than
      the setting (default `2w`) are deleted. Clear the field to keep
      everything, or turn on **System → Dry run** for one run to see what
      would be removed.
    * **Schedule**, if it names weekdays by number. Day-of-week `0` now means
      Sunday, as in standard cron. `0 3 * * 1-5` used to run Tuesday to
      Saturday and now runs Monday to Friday.
    * **Sync → Include / Exclude.** They must be type or type+direction codes
      such as `P` or `NF`; other values are rejected when you save.
    * **Sync → Skip metadata** and **Retry failed after** now apply to
      scheduled syncs.

If you use the command line, the command is now `blackvuesync-v2` and the
module is `blackvuesync_v2` (`python -m blackvuesync_v2`). The options are
unchanged.

### From the original BlackVue Sync (2.2.x and earlier)

The original images ran a cron job inside the container and had no web
interface. v2 runs a web service with its own scheduler.

| Before | Now |
| --- | --- |
| `CRON` env var | Retired. Set the schedule under **Settings → Schedule** (default every 15 minutes). |
| `RUN_ONCE` env var | Retired. Use **Sync now** on the dashboard, or run a one-off `sync` command (below). |
| Env vars read on every start | Env vars seed `/config/settings.json` once; afterwards the file is used. |
| No ports | Web interface on port 8080. |
| Only `/recordings` volume | Also mount `/config`. |
| `DRY_RUN` env var | Use **System → Dry run** in the web interface. |

Steps:

1. Stop and remove the old container. Recordings in `/recordings` stay where
   they are and are not downloaded again: v2 uses the same file names and
   folders.
2. Use the image `ghcr.io/tekgnosis-net/blackvuesync-v2:3`, add a `/config`
   volume and publish port 8080 (see
   [Installation](installation.md#option-1-docker-compose-recommended)).
   Keep your existing env vars; on this first start they are copied into
   `settings.json`. `KEEP` defaults to `2w` if unset; set it to a larger
   value, or clear **Retention → Keep** afterwards, if you relied on keeping
   everything.
3. Add `BLACKVUESYNC_TIMEZONE` with the same value as `TZ` so the schedule
   runs in local time.
4. Start the container and complete the [first run](installation.md#first-run).
5. Check **Settings** once; from now on change settings there, not in the
   compose file.

A one-off sync, without the web service, still works by overriding the
command:

```sh
docker run --rm -v /data/dashcam:/recordings \
    ghcr.io/tekgnosis-net/blackvuesync-v2:3 \
    sync 192.168.1.50 --destination /recordings --dry-run --verbose
```

#### Cron jobs on the host

If you run the original `blackvuesync` from a crontab, install v2 (see
[Command line](cli.md#installing-the-command)) and replace `blackvuesync`
with `blackvuesync-v2` in the cron line; the options are the same. The
original package can stay installed, since the command names differ.

Do not run cron syncs and the web service against the same destination: the
shared lock file keeps them from colliding, but the web interface only shows
its own runs.
