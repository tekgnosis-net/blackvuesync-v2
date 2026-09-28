# Installation

This guide sets up BlackVue Sync as a long-running service that downloads
recordings from a BlackVue dashcam on a schedule and serves a web UI for
monitoring, settings, logs, statistics and playback.

For upgrades see [upgrading.md](upgrading.md). For problems see
[troubleshooting.md](troubleshooting.md). Every setting is listed in
[configuration.md](configuration.md).

## Before you start

You need:

* A cloud-capable BlackVue dashcam joined to your Wi-Fi network with a
  **static IP address** or a stable DNS name. Set a DHCP reservation on your
  router so the address does not change.
* The dashcam powered for a while after the engine turns off (hardwiring kit or
  battery pack), long enough for the day's recordings to download. Plan on
  roughly 1 hour of download time per hour of two-channel recording on a good
  Wi-Fi link.
* A host that is always on and on the same LAN: a NAS, home server or
  Raspberry Pi 3+ running a 64-bit OS. Images are published for `amd64` and
  `arm64`.
* Disk space on a local filesystem (not NFS/SMB, see
  [troubleshooting](troubleshooting.md#lock-errors-on-network-shares)). Budget
  about 5 GB per hour of recording per camera.

### Check the dashcam is reachable

From the host, with the car parked in Wi-Fi range and the dashcam on:

```sh
curl http://192.168.1.50/blackvue_vod.cgi
```

Replace the address with your dashcam's. A working dashcam answers with a
list of files:

```text
v:1.00
n:/Record/20260925_081503_NF.mp4,s:1000000
n:/Record/20260925_081503_NR.mp4,s:1000000
...
```

If this fails, fix connectivity first; BlackVue Sync cannot work around it.

## Option 1: Docker Compose (recommended)

1. Create a directory for the service and two data directories:

   ```sh
   mkdir -p ~/blackvuesync-v2/config /data/dashcam
   cd ~/blackvuesync-v2
   ```

2. Find the user and group IDs that should own the recordings:

   ```sh
   id -u   # e.g. 1000
   id -g   # e.g. 1000
   ```

3. Create `docker-compose.yml`:

   ```yaml
   services:
     blackvuesync-v2:
       image: ghcr.io/tekgnosis-net/blackvuesync-v2:3
       container_name: blackvuesync-v2
       restart: unless-stopped
       ports:
         - "8080:8080"
       volumes:
         - /data/dashcam:/recordings
         - ./config:/config
       environment:
         ADDRESS: 192.168.1.50
         PUID: 1000
         PGID: 1000
         TZ: America/New_York
         BLACKVUESYNC_TIMEZONE: America/New_York
         KEEP: 2w
   ```

   The environment variables are only read the first time the container
   starts, to create `/config/settings.json`. After that, change settings in
   the web UI. See [configuration.md](configuration.md#environment-variables)
   for the full list.

4. Start it:

   ```sh
   docker compose up -d
   docker compose logs -f
   ```

   Look for `scheduler started` and `starting web server on 0.0.0.0:8080`.

## Option 2: `docker run`

```sh
docker run -d --name blackvuesync-v2 --restart unless-stopped \
    -p 8080:8080 \
    -v /data/dashcam:/recordings \
    -v /data/blackvuesync-v2-config:/config \
    -e ADDRESS=192.168.1.50 \
    -e PUID=$(id -u) -e PGID=$(id -g) \
    -e TZ=America/New_York \
    -e BLACKVUESYNC_TIMEZONE=America/New_York \
    -e KEEP=2w \
    ghcr.io/tekgnosis-net/blackvuesync-v2:3
```

Always mount `/config`. Without it the settings file, password, statistics
and logs are lost when the container is recreated.

## Option 3: Python, without Docker

Requires Python 3.9 or newer.

```sh
python3 -m venv ~/blackvuesync-v2-venv
~/blackvuesync-v2-venv/bin/pip install "git+https://github.com/tekgnosis-net/blackvuesync-v2@v3.0.0"
```

Replace `v3.0.0` with the release you want (see the
[release notes](../release-notes.md)). The command is `blackvuesync-v2`. The
`blackvuesync` package on PyPI is the original project, which has no web
service; the two can be installed side by side.

Run the web service with a config directory of your choice:

```sh
ADDRESS=192.168.1.50 \
~/blackvuesync-v2-venv/bin/blackvuesync-v2 serve \
    --config-path ~/blackvuesync-v2/config/settings.json
```

Then set **System → Destination** in the web UI to your recordings directory.
To keep it running, wrap it in a systemd unit or similar.

If you only want the command-line sync, without the web service, see
[Command line](cli.md). That mode has no web UI and uses command-line options
instead of `settings.json`.

## First run

1. Open `http://<host>:8080/`. You are redirected to the first-run page.
2. Choose an admin username (default `admin`) and a password of at least 12
   characters. You are then sent to the login page.

   If `BLACKVUESYNC_ADMIN_PASSWORD` was set when the container first started,
   the password is already set and the first-run page is skipped.

   Until a password is set, anyone who can reach port 8080 can claim the
   admin account. Complete this step right after starting the container, or
   set `BLACKVUESYNC_ADMIN_PASSWORD`.
3. Log in and open **Settings**. Check at least:
   * **Connection → Address**: the dashcam IP or hostname.
   * **Schedule**: the cron expression (default every 15 minutes) and the
     timezone it runs in.
   * **Retention → Keep recordings for**: e.g. `2w`. Leave empty to keep
     everything. **Max used disk (%)** stops downloads when the disk reaches
     that level (default 90).
   * **System → Dry run**: turn it on for a first test if you want to see what
     would be downloaded without writing files.
4. Go to the dashboard and press **Sync now**. The status card shows progress
   file by file. The **Logs** page shows the details.
5. If you used dry run, turn it off once the logs look right.

## Recordings layout

Recordings keep their dashcam filenames, e.g. `20260925_081503_NF.mp4`
(date, time, type `N` normal, direction `F` front). Each recording can come
with a thumbnail (`.thm`), accelerometer data (`.3gf`) and GPS data (`.gps`).
With **Sync → Grouping** set to `daily`, `weekly`, `monthly` or `yearly`,
files go into date-named subdirectories.

Partial downloads are stored as hidden dotfiles (`.20260925_081503_NF.mp4`)
and resumed on the next run.

## Putting it behind a reverse proxy

The service speaks plain HTTP on port 8080. For HTTPS or access from outside
your LAN, put a reverse proxy in front of it and set
`BLACKVUESYNC_TRUST_PROXY=1` in the container environment. With that set:

* the session cookie is marked `Secure` (HTTPS only), and
* `X-Forwarded-For` / `X-Forwarded-Proto` from the proxy are honored.

Do not set it if clients connect to port 8080 directly; they could then spoof
their address.

### Caddy

```caddyfile
dashcam.example.net {
    reverse_proxy 127.0.0.1:8080
}
```

### nginx

The dashboard and log pages use Server-Sent Events. Disable buffering for
them so updates arrive live:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_read_timeout 1h;
}
```

### Letting the proxy handle login

If your proxy already authenticates users (Authelia, oauth2-proxy, Authentik,
etc.), you can switch **Settings → Auth → Auth mode** to `proxy`:

* **Trusted proxies**: the IP address or CIDR of the proxy as seen by the
  container, one per line (e.g. `172.18.0.0/16` for a Docker network).
* **Proxy user header**: the header your proxy sets with the user name
  (default `X-Remote-User`).

Requests that do not come from a trusted proxy address, or lack the header,
are rejected. Make sure port 8080 is not reachable except through the proxy.

`none` mode disables authentication entirely. Use it only on a network where
everyone is trusted.

## Health checks

* `GET /healthz` returns 200 while the process is up. The Docker image uses
  it for its `HEALTHCHECK`.
* `GET /readyz` returns 200 once settings are loaded.

Both need no login.

## Monitoring

Prometheus metrics can be written to a file for the node_exporter textfile
collector or pushed to a Pushgateway. Set them under **Settings → Metrics**.
See [Prometheus metrics](cli.md#prometheus-metrics) for the metric names and
example alerts.
