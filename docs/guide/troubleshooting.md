# Troubleshooting

Start with the logs. In the web UI open **Logs**. To record more detail, set
the capture verbosity to **Debug** and the display filter to **Debug**, then
reproduce the problem. From the host:

```sh
docker compose logs --tail 200 blackvuesync-v2
```

The same log is written to `/config/logs/blackvuesync.log`.

## The container does not start

### `PermissionError: settings file ... has insecure permissions`

`settings.json` must be readable only by its owner:

```sh
chmod 600 ~/blackvuesync-v2/config/settings.json
```

### `PermissionError` / `Permission denied` on `/config`

The service runs as the user given by `PUID`/`PGID`. That user must own the
host directory mounted at `/config`:

```sh
sudo chown -R 1000:1000 ~/blackvuesync-v2/config   # use your PUID:PGID
```

### `JSONDecodeError` on startup

`settings.json` was edited by hand and is no longer valid JSON. Fix the
syntax, or restore it from a backup. As a last resort, move it away and
restart; a new file is created from the environment variables, and you set
the password again on the first-run page.

### Container keeps restarting after a schedule change

Check the log for a scheduler error. An invalid cron expression or timezone
falls back to the default schedule (every 15 minutes, UTC) with an error in
the log. Correct it under **Settings → Schedule**.

## Cannot open the web UI

* Check the port is published: `docker ps` should show `0.0.0.0:8080->8080`.
* Check the container is healthy: `docker inspect --format '{{.State.Health.Status}}' blackvuesync-v2`.
* From the host, `curl -i http://localhost:8080/healthz` should return `200`.
* If you changed **Web → Port**, update the port mapping to match and
  restart.
* Firewalls on NAS devices often block new ports; allow 8080 or use a
  reverse proxy.

## Login problems

### Locked out after failed attempts

After 10 failed logins from one address within 10 minutes, logins from that
address are blocked for 15 minutes. Wait, or restart the container (the
counter is kept in memory).

### Forgot the password

Stop the container, set `"password_hash": ""` in the `auth` section of
`/config/settings.json`, start it again, and set a new password on the
first-run page. Do this quickly: until a password is set, anyone on the
network can set it.

### Logged out right after logging in, behind a reverse proxy

With `BLACKVUESYNC_TRUST_PROXY=1` the session cookie is sent only over
HTTPS. Access the UI through `https://`, or unset the variable if you do not
use HTTPS.

### `proxy` auth mode returns 401

* The proxy's address, as the container sees it, must be listed in
  **Auth → Trusted proxies**. For a proxy in another Docker container this is
  usually the Docker network range, e.g. `172.18.0.0/16`. Check the address
  with `docker network inspect <network>`.
* The proxy must send the header named in **Proxy user header**.
* To get back in, stop the container and set `"mode": "login"` in
  `settings.json`.

### All sessions end suddenly

Someone pressed **Sign out all sessions** or changed the password. Log in
again.

## Syncs do not run

* **Dashboard shows "paused"**: press **Resume**, or turn off
  **Schedule → Pause scheduled syncs**.
* **Wrong times**: the cron expression runs in **Schedule → Timezone**, which
  defaults to UTC, not the container's `TZ`.
* **`Another instance is already running for destination`**: a previous sync
  is still going, or a separate cron job or second container uses the same
  recordings directory. Only one sync per directory can run at a time.
* **"Sync now" returns "already running"**: wait for the current run or press
  **Stop**.

## Dashcam problems

### `Dashcam unavailable` / `Timeout communicating with dashcam`

The dashcam is off, out of Wi-Fi range, or on another address. This is
normal when the car is away. The web service then logs a single line such as
`dashcam 192.168.1.50 not reachable; next attempt at the scheduled time`, and
the dashboard's **Last sync** card shows a grey **dashcam not reachable**
badge instead of a red **failed** one. On the Statistics page such runs are
counted under **Dashcam offline**, not as failures. A refused connection is
different: it
means another device answered on that address, so it is reported as an error.

If the car is parked in range:

* Run `curl http://<dashcam>/blackvue_vod.cgi` from the host.
* Check **Connection → Address** matches the dashcam's current IP. Give the
  dashcam a DHCP reservation.
* Increase **Connection → Timeout** on weak Wi-Fi.
* Make sure the dashcam stays powered after the engine stops (parking mode
  or battery settings).

### `Dashcam disconnected without a response`

Usually weak Wi-Fi or the dashcam going to sleep mid-transfer. The partial
file is kept and resumed on the next run.

### Downloads are slow or never catch up

The log shows the download speed of each file. Two-channel recording at high quality
produces about 20 Mbit/s; the Wi-Fi link must be faster than that for the
backlog to shrink. Options:

* Move the access point closer to the car.
* Set **Sync → Priority** to `type` so manual and event clips come first,
  or `rdate` for newest first.
* Use **Sync → Exclude** to skip, e.g., `NR` (normal rear) recordings.
* Use **Sync → Skip metadata** to skip thumbnails.

### Some files fail every time

A file that fails is retried after **Sync → Retry failed after** (default one
day). A persistent 500 error from the dashcam usually means the file is
corrupt on the SD card; it is skipped until the retry window passes.

## Disk and retention

### `Not enough disk space left. Max used disk space percentage allowed : 90%`

Downloads stop when the destination disk passes **Retention → Max used disk**.
Free space, lower **Retention → Keep recordings for**, or raise the limit.
The **Statistics** page shows a disk-usage forecast.

### Old recordings are not deleted

* **Retention → Keep recordings for** must be set (e.g. `2w`). Empty keeps
  everything.
* Retention runs at the start of each sync, so at least one sync has to run.
* In dry-run mode deletions are only logged.
* Recording dates come from the filenames, i.e. the dashcam's clock. If the
  dashcam clock is wrong, retention is too.

### Dates and times are off by an hour

BlackVue dashcams do not follow daylight saving time. Adjust the dashcam
clock when the clocks change, and set `TZ` to the dashcam's timezone.

## Lock errors on network shares

### `Could not acquire lock on destination`

The lock file uses `fcntl` locks, which do not work reliably on NFS or SMB
mounts. Store recordings on a local disk of the host running the container.
If the recordings must end up on a NAS, run the container on the NAS itself.

## Web UI problems

### Dashboard or Logs page never updates live

Live updates use Server-Sent Events. Behind nginx, add
`proxy_buffering off;` (see [installation](installation.md#nginx)). Some
browser extensions and corporate proxies also buffer event streams.

### "Too many streams" / pages slow with many tabs open

Each open dashboard or logs tab holds one live connection, and the number of
connections is capped. Close unused tabs.

### "Save failed" on the Settings page

The message next to the field says which value is invalid. If every save
fails, reload the page; your login may have expired.

### Viewer shows no map or G-sensor chart

The map needs `.gps` files and the chart needs `.3gf` files. Check that
**Sync → Skip metadata** does not include `g` or `3`. Parking recordings
often have no GPS fix. The map tiles are loaded from OpenStreetMap, so the
browser needs internet access; without it the track still draws on a blank
background.

### Viewer list takes a few seconds on first open

After the service starts, the first visit to the viewer reads every recording
folder once to build its index; on a NAS with tens of thousands of recordings
that takes a few seconds. Later visits only re-read folders that changed and
open almost instantly. The list shows days; open a day to see its recordings.

### Viewer does not play a video

Browsers play the H.264 video in BlackVue `.mp4` files. If a file does not
play, check it plays in VLC; if not, the download was likely cut short and
will be fetched again on the next run.

## Starting over

To reset all settings but keep recordings:

```sh
docker compose down
mv ~/blackvuesync-v2/config ~/blackvuesync-v2/config.old
mkdir ~/blackvuesync-v2/config
docker compose up -d
```

The environment variables seed a fresh `settings.json`, and the first-run
page asks for a new password.

## Reporting a bug

Open an issue at <https://github.com/tekgnosis-net/blackvuesync-v2/issues> with:

* the version (`docker exec blackvuesync-v2 python -m blackvuesync_v2 --version`),
* the dashcam model and firmware (shown on the dashboard's dashcam card),
* the relevant log lines, captured with verbosity set to **Debug** on the
  Logs page.

Remove the dashcam address and any usernames from the logs if you consider
them private. Never post `settings.json`; it contains your password hash and
session secret.
