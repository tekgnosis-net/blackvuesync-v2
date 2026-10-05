#!/usr/bin/env python3
"""generates the documentation screenshots from a synthetic demo library.

starts a demo dashcam and a real `blackvuesync-v2 serve` against generated
data (test-pattern footage, a gps track along a public road, invented run
history -- no real recordings or locations), drives the web ui with playwright
and writes png files to docs/assets/screenshots/.

requires ffmpeg on PATH and the playwright chromium browser
(`python -m playwright install chromium`).

usage: python scripts/screenshots.py [--out DIR] [--keep]
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import http.server
import json
import math
import os
import shutil
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

# pylint: disable=wrong-import-position
from blackvuesync_v2.server.auth import hash_password  # noqa: E402
from blackvuesync_v2.server.stats_store import StatsStore  # noqa: E402
from blackvuesync_v2.settings import SettingsStore  # noqa: E402

DEMO_TZ = "Australia/Sydney"
DEMO_USER = "admin"
DEMO_PASSWORD = "demo-password-1234"  # nosec: throwaway demo instance only
SEGMENT_SECONDS = 60
SPEED_MPS = 12.5  # ~45 km/h
VIEWPORT = {"width": 1440, "height": 900}

# driving route across the sydney harbour bridge, simplified to 4 m. route
# geometry derived from openstreetmap data (c) openstreetmap contributors,
# odbl, via the osrm demo router.
ROUTE = [
    (-33.86602, 151.20680),
    (-33.86515, 151.20689),
    (-33.86510, 151.20684),
    (-33.86507, 151.20575),
    (-33.86513, 151.20485),
    (-33.86425, 151.20475),
    (-33.86330, 151.20431),
    (-33.86288, 151.20434),
    (-33.86100, 151.20538),
    (-33.85991, 151.20575),
    (-33.85903, 151.20643),
    (-33.84971, 151.21222),
    (-33.84890, 151.21253),
    (-33.84826, 151.21263),
    (-33.84737, 151.21254),
    (-33.84660, 151.21230),
    (-33.84274, 151.21073),
    (-33.84239, 151.21051),
    (-33.84167, 151.20985),
    (-33.84137, 151.20930),
    (-33.84071, 151.20850),
    (-33.83890, 151.20686),
    (-33.83693, 151.20722),
    (-33.83287, 151.20809),
    (-33.83279, 151.20755),
]

CAMERA_FIXTURES = REPO / "test" / "fixtures" / "camera"
CONFIG_INI = (CAMERA_FIXTURES / "dr900x-plus-config.ini").read_text(encoding="utf-8")
VERSION_BIN = (CAMERA_FIXTURES / "dr900x-plus-version.bin").read_bytes()


@dataclasses.dataclass(frozen=True)
class Media:
    """generated demo media shared by every recording."""

    front: Path
    rear: Path
    thumb: Path


def free_port() -> int:
    """returns an unused localhost tcp port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def ffmpeg(*args: str) -> None:
    """runs ffmpeg quietly and fails loudly."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True
    )


def make_media(root: Path) -> Media:
    """renders one front and one rear test-pattern clip and a thumbnail."""
    root.mkdir(parents=True, exist_ok=True)
    clips = {}
    for label, look in (("front", "null"), ("rear", "hflip,hue=h=150:s=0.6")):
        path = root / f"{label}.mp4"
        text = f"DEMO FOOTAGE  {label.upper()} CAMERA"
        ffmpeg(
            "-f", "lavfi",
            "-i", f"testsrc2=size=960x540:rate=15:duration={SEGMENT_SECONDS}",
            "-vf",
            f"{look},drawbox=y=ih-60:w=iw:h=60:color=black@0.55:t=fill,"
            f"drawtext=font=Sans:text='{text}':fontcolor=white:fontsize=26:"
            "x=24:y=h-44",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(path),
        )  # fmt: skip
        clips[label] = path
    thumb = root / "thumb.jpg"
    ffmpeg(
        "-ss", "2", "-i", str(clips["front"]), "-frames:v", "1",
        "-vf", "scale=320:-1", "-f", "mjpeg", str(thumb),
    )  # fmt: skip
    return Media(front=clips["front"], rear=clips["rear"], thumb=thumb)


def _metres(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = 111_320.0
    return math.hypot(
        (a[0] - b[0]) * k, (a[1] - b[1]) * k * math.cos(math.radians(a[0]))
    )


def route_position(distance: float) -> tuple[float, float]:
    """returns the (lat, lon) `distance` metres along the route (clamped)."""
    for a, b in zip(ROUTE, ROUTE[1:]):
        leg = _metres(a, b)
        if distance <= leg:
            f = distance / leg if leg else 0.0
            return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f
        distance -= leg
    return ROUTE[-1]


def route_length() -> float:
    return sum(_metres(a, b) for a, b in zip(ROUTE, ROUTE[1:]))


def _nmea_coord(value: float, lat: bool) -> tuple[str, str]:
    hemi = ("N" if value >= 0 else "S") if lat else ("E" if value >= 0 else "W")
    value = abs(value)
    degrees = int(value)
    minutes = (value - degrees) * 60
    width = 2 if lat else 3
    return f"{degrees:0{width}d}{minutes:08.5f}", hemi


def gps_text(start: datetime.datetime, offset_m: float, moving: bool) -> str:
    """builds a .gps sidecar: one rmc sentence per second along the route."""
    lines = []
    epoch = start.astimezone(datetime.timezone.utc)
    for second in range(SEGMENT_SECONDS):
        ts = epoch + datetime.timedelta(seconds=second)
        speed = SPEED_MPS * (0.8 + 0.2 * math.sin(second / 9)) if moving else 0.0
        lat, lon = route_position(offset_m + (SPEED_MPS * second if moving else 0))
        lat_s, ns = _nmea_coord(lat, True)
        lon_s, ew = _nmea_coord(lon, False)
        knots = speed / 0.514444
        lines.append(
            f"[{int(ts.timestamp() * 1000)}]$GNRMC,{ts:%H%M%S}.00,A,{lat_s},{ns},"
            f"{lon_s},{ew},{knots:.3f},,{ts:%d%m%y},,,A,V*00\r\n"
        )
    return "".join(lines)


def gsensor_bytes(spike: bool) -> bytes:
    """builds a .3gf sidecar: 10 hz big-endian samples in 1/128 g."""
    out = bytearray()
    for i in range(SEGMENT_SECONDS * 10):
        x = int(10 * math.sin(i / 7))
        y = int(6 * math.sin(i / 11))
        z = 128 + int(4 * math.sin(i / 3))
        if spike and 250 <= i < 262:
            x, y = 180 - (i - 250) * 12, -90
        out += struct.pack(">Ihhh", i * 100, x, y, z)
    return bytes(out)


@dataclasses.dataclass(frozen=True)
class Planned:
    """one recording instant of the demo library."""

    start: datetime.datetime
    rtype: str
    offset_m: float
    moving: bool = True
    spike: bool = False

    @property
    def base(self) -> str:
        return f"{self.start:%Y%m%d_%H%M%S}"


def plan_library(today: datetime.date, tz: Any) -> list[Planned]:
    """lays out three days of journeys plus today's not-yet-synced drive."""
    plan: list[Planned] = []
    per_segment = SPEED_MPS * SEGMENT_SECONDS
    journey_len = max(1, int(route_length() // per_segment))

    def journey(day: datetime.date, hh: int, mm: int) -> None:
        start = datetime.datetime(day.year, day.month, day.day, hh, mm, 5, tzinfo=tz)
        for i in range(journey_len):
            plan.append(
                Planned(
                    start + datetime.timedelta(seconds=SEGMENT_SECONDS * i),
                    "N",
                    per_segment * i,
                )
            )

    for back, (hh, mm) in ((3, (8, 12)), (2, (7, 55)), (1, (17, 40))):
        day = today - datetime.timedelta(days=back)
        journey(day, hh, mm)
        plan.append(
            Planned(
                datetime.datetime(day.year, day.month, day.day, 12, 31, 40, tzinfo=tz),
                "P",
                0.0,
                moving=False,
            )
        )
    event_day = today - datetime.timedelta(days=1)
    plan.append(
        Planned(
            datetime.datetime(
                event_day.year, event_day.month, event_day.day, 17, 43, 10, tzinfo=tz
            ),
            "E",
            per_segment * 2.5,
            spike=True,
        )
    )
    journey(today, 8, 5)
    return plan


def sidecars(rec: Planned) -> dict[str, bytes]:
    return {
        f"{rec.base}_{rec.rtype}.gps": gps_text(
            rec.start, rec.offset_m, rec.moving
        ).encode(),
        f"{rec.base}_{rec.rtype}.3gf": gsensor_bytes(rec.spike),
    }


def write_library(dest: Path, media: Media, recs: list[Planned]) -> None:
    """writes recordings into daily directories, hard-linking the media."""
    for rec in recs:
        day_dir = dest / f"{rec.start:%Y-%m-%d}"
        day_dir.mkdir(parents=True, exist_ok=True)
        for direction, clip in (("F", media.front), ("R", media.rear)):
            stem = f"{rec.base}_{rec.rtype}{direction}"
            os.link(clip, day_dir / f"{stem}.mp4")
            os.link(media.thumb, day_dir / f"{stem}.thm")
        for name, data in sidecars(rec).items():
            (day_dir / name).write_bytes(data)


class DemoDashcam(http.server.ThreadingHTTPServer):
    """serves a blackvue-style http api for the not-yet-synced recordings."""

    def __init__(self, port: int, media: Media, recs: list[Planned]) -> None:
        self.files: dict[str, bytes] = {}
        for rec in recs:
            for direction, clip in (("F", media.front), ("R", media.rear)):
                stem = f"{rec.base}_{rec.rtype}{direction}"
                self.files[f"{stem}.mp4"] = clip.read_bytes()
                self.files[f"{stem}.thm"] = media.thumb.read_bytes()
            self.files.update(sidecars(rec))
        self.bytes_per_second = 900_000  # slow enough to capture progress
        super().__init__(("127.0.0.1", port), _DashcamHandler)


class _DashcamHandler(http.server.BaseHTTPRequestHandler):
    server: DemoDashcam

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        del format, args

    def _send(self, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200 if self.path.startswith("/blackvue_vod.cgi") else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/blackvue_vod.cgi":
            names = sorted(n for n in self.server.files if n.endswith(".mp4"))
            listing = "v:1.00\r\n" + "".join(
                f"n:/Record/{n},s:1000000\r\n" for n in names
            )
            self._send(listing.encode())
        elif path == "/Config/version.bin":
            self._send(VERSION_BIN)
        elif path == "/Config/config.ini":
            self._send(CONFIG_INI.encode())
        elif path.startswith("/Record/") and path[8:] in self.server.files:
            self._stream(self.server.files[path[8:]])
        else:
            self.send_error(404)

    def _stream(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        chunk = self.server.bytes_per_second // 10
        try:
            for i in range(0, len(body), chunk):
                self.wfile.write(body[i : i + chunk])
                time.sleep(0.1)
        except (BrokenPipeError, ConnectionResetError):
            pass


def seed_stats(db_path: Path, now: float, disk_now: float) -> None:
    """writes two weeks of plausible sync runs every 15 minutes.

    disk usage climbs to `disk_now` (the demo destination's real ratio) so the
    run recorded by the live sync continues the line without a jump.
    """
    # pylint: disable-next=import-outside-toplevel
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DEMO_TZ)
    StatsStore(str(db_path))  # creates the schema
    rows = []
    start = now - 14 * 86400
    ts = start - (start % 900)
    first_ts = ts
    while ts < now - 900:
        local = datetime.datetime.fromtimestamp(ts, tz)
        local_hour = local.hour
        minutes = local.hour * 60 + local.minute
        weekday = local.weekday() < 5
        # the car is away at work on weekdays and out late morning at weekends;
        # each drive downloads once it is back home.
        if weekday:
            away = 8 * 60 + 30 <= minutes < 17 * 60 + 30
            after_drive = local.hour in (18, 19) and local.minute == 0
        else:
            away = 11 * 60 <= minutes < 13 * 60 + 30
            after_drive = local.hour == 14 and local.minute == 0
        files = 24 if after_drive else 0
        size = files * 42_000_000
        progress = (ts - first_ts) / (now - first_ts)
        # daily sawtooth: downloads fill during the day, retention trims at 3am
        daily = 0.003 * ((local_hour - 3) % 24) / 24
        disk = disk_now - 0.06 * (1 - progress) - 0.003 + daily
        failed = not away and (ts // 900) % 97 == 0  # a rare real failure
        if away:
            reasons, success, duration = {"network": 1}, 0, 3.0
        elif failed:
            reasons, success, duration = {"http": 1}, 0, 8.0
        else:
            reasons, success = {}, 1
            duration = 120.0 + files * 6.5 if files else 2.1
        rows.append(
            (
                float(ts),
                success,
                0 if success else 1,
                duration,
                files if success else 0,
                size if success else 0,
                5200,
                files,
                round(disk, 4),
                0,
                json.dumps(reasons, separators=(",", ":")),
                0,
            )
        )
        ts += 900
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )


def write_settings(config: Path, dest: Path, dashcam: str) -> None:
    config.parent.mkdir(parents=True, exist_ok=True)
    env = {
        "ADDRESS": dashcam,
        "BLACKVUESYNC_SCHEDULE": "30 3 * * *",
        "BLACKVUESYNC_TIMEZONE": DEMO_TZ,
        "GROUPING": "daily",
        "KEEP": "30d",
        "MAX_USED_DISK": "90",
    }
    saved = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        store = SettingsStore(config)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    pw_hash = hash_password(DEMO_PASSWORD)
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username=DEMO_USER, password_hash=pw_hash),
            system=dataclasses.replace(s.system, destination=str(dest)),
            viewer=dataclasses.replace(s.viewer, journey_mode="full"),
            metrics=dataclasses.replace(
                s.metrics, state_file=str(config.parent / "metrics-state.json")
            ),
        )
    )


def wait_healthy(url: str, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/healthz", timeout=2):
                return
        except OSError:
            time.sleep(0.3)
    raise RuntimeError(f"server at {url} did not become healthy")


def capture(base: str, out: Path) -> None:
    """logs in, drives each page and writes the screenshots."""
    # pylint: disable-next=import-outside-toplevel
    from playwright.sync_api import sync_playwright

    def login(page: Any) -> None:
        page.goto(f"{base}/login")
        page.fill('input[name="username"]', DEMO_USER)
        page.fill('input[name="password"]', DEMO_PASSWORD)
        page.click('button[type="submit"]')
        page.wait_for_url(f"{base}/")

    def shot(page: Any, name: str) -> None:
        page.wait_for_timeout(600)  # lets transitions and charts settle
        page.screenshot(path=str(out / f"{name}.png"))
        print(f"  wrote {name}.png")

    def viewer(page: Any, name: str) -> None:
        page.goto(f"{base}/viewer")
        today = page.locator(".viewer-day").first
        today.locator(".viewer-rec").last.click()  # first segment of the drive
        page.wait_for_function(
            "document.getElementById('viewer-front').readyState >= 2"
        )
        page.wait_for_selector(".leaflet-tile-loaded")
        page.evaluate(
            "() => { const v = document.getElementById('viewer-front');"
            " v.pause(); v.currentTime = 38; }"
        )
        page.wait_for_load_state("networkidle")
        shot(page, name)

    with sync_playwright() as p:
        browser = p.chromium.launch()
        # the app's csp forbids eval; playwright's wait expressions need it.
        light = browser.new_context(
            viewport=VIEWPORT,
            color_scheme="light",
            timezone_id=DEMO_TZ,
            bypass_csp=True,
        )
        page = light.new_page()
        login(page)
        page.click("[data-action='sync-now']")
        page.wait_for_function("document.body.dataset.state === 'running'")
        page.wait_for_timeout(6000)
        shot(page, "dashboard-syncing")
        page.wait_for_function(
            "document.body.dataset.state === 'idle'", timeout=300_000
        )
        page.reload()
        page.wait_for_selector("[data-action='sync-now']")
        shot(page, "dashboard")

        page.goto(f"{base}/logs")
        page.wait_for_selector(".log-row")
        shot(page, "logs")

        page.goto(f"{base}/stats")
        page.wait_for_selector("canvas[data-chart='disk']")
        page.wait_for_timeout(1500)
        shot(page, "stats")

        page.goto(f"{base}/settings")
        page.click("[data-section-nav='sync']")
        shot(page, "settings")

        page.locator('[data-section-nav="camera-cloud"]').click()
        page.locator("#camera-panes .camera-field").first.wait_for(state="attached")
        shot(page, "camera-settings")

        viewer(page, "viewer")

        dark = browser.new_context(
            viewport=VIEWPORT,
            color_scheme="dark",
            timezone_id=DEMO_TZ,
            bypass_csp=True,
        )
        page = dark.new_page()
        login(page)
        page.wait_for_selector("[data-action='sync-now']")
        shot(page, "dashboard-dark")
        viewer(page, "viewer-dark")
        browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "docs/assets/screenshots")
    parser.add_argument("--keep", action="store_true", help="keeps the work dir")
    args = parser.parse_args()
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required", file=sys.stderr)
        return 1

    # pylint: disable-next=import-outside-toplevel
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(DEMO_TZ)
    work = Path(tempfile.mkdtemp(prefix="bvs2-screenshots-"))
    args.out.mkdir(parents=True, exist_ok=True)
    server: subprocess.Popen[bytes] | None = None
    dashcam: DemoDashcam | None = None
    try:
        print(f"work dir: {work}")
        media = make_media(work / "media")
        today = datetime.datetime.now(tz).date()
        plan = plan_library(today, tz)
        synced = [r for r in plan if r.start.date() < today]
        pending = [r for r in plan if r.start.date() == today]
        dest = work / "recordings"
        write_library(dest, media, synced)

        dashcam = DemoDashcam(free_port(), media, pending)
        threading.Thread(target=dashcam.serve_forever, daemon=True).start()
        dashcam_addr = f"127.0.0.1:{dashcam.server_address[1]}"

        config = work / "config" / "settings.json"
        write_settings(config, dest, dashcam_addr)
        usage = shutil.disk_usage(dest)
        seed_stats(config.parent / "stats.db", time.time(), usage.used / usage.total)

        port = free_port()
        env = {**os.environ, "TZ": DEMO_TZ, "PYTHONPATH": str(REPO)}
        with open(work / "serve.log", "wb") as log:
            server = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "blackvuesync_v2",
                    "serve",
                    "--config-path",
                    str(config),
                    "--port",
                    str(port),
                ],
                cwd=work,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        base = f"http://127.0.0.1:{port}"
        wait_healthy(base)
        capture(base, args.out)
        return 0
    finally:
        if server is not None:
            server.terminate()
            server.wait(timeout=30)
        if dashcam is not None:
            dashcam.shutdown()
        if args.keep:
            print(f"kept {work}")
        else:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
