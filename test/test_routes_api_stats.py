"""flask test-client tests for /api/stats/series."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.metrics import SyncMetrics
from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import (
    SESSION_VERSION_KEY,
    hash_password,
    session_version,
)
from blackvuesync_v2.server.stats_store import StatsStore
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def app_and_client(tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth,
                username="admin",
                password_hash=hash_password("test-password-1234"),
            ),
            system=dataclasses.replace(s.system, destination=str(destination)),
        )
    )
    stats = StatsStore(str(tmp_path / "stats.db"))
    app = create_app(store, testing=True, stats_store=stats)
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = "admin"
            sess[SESSION_VERSION_KEY] = session_version(
                app.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
            )
        yield app, client, stats


def _seed(stats: StatsStore, n: int = 5) -> None:
    now = time.time()
    for i in range(n):
        m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=now)
        m.last_run_timestamp_seconds = now - (n - i) * 3600
        m.last_run_success = 1
        m.last_run_exit_code = 0
        m.run_duration_seconds = 2.0
        m.files_downloaded_last_run = i
        m.bytes_downloaded_last_run = i * 1000
        m.destination_disk_used_ratio = 0.40 + i * 0.01
        stats.record_run(m)


def test_series_requires_login(app_and_client: Any) -> None:
    app, _client, _stats = app_and_client
    resp = app.test_client().get("/api/stats/series?range=7d")
    assert resp.status_code in (302, 401)


def test_series_returns_summary_series_forecast(app_and_client: Any) -> None:
    app, client, stats = app_and_client
    _seed(stats, 5)
    resp = client.get("/api/stats/series?range=all")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["range"] == "all"
    assert body["summary"]["runs"] == 5
    assert len(body["series"]["points"]) == 5
    assert all("dry_run" in p for p in body["series"]["points"])
    assert "forecast" in body
    assert body["forecast"]["limits"]["max_used_disk_percent"] == pytest.approx(0.9)


def test_series_empty_store_ok(app_and_client: Any) -> None:
    app, client, _stats = app_and_client
    resp = client.get("/api/stats/series?range=24h")
    assert resp.status_code == 200
    body = json.loads(resp.data)
    assert body["summary"]["runs"] == 0
    assert body["series"]["points"] == []
    assert body["forecast"]["projected"] == []


def test_series_rejects_unknown_range(app_and_client: Any) -> None:
    app, client, _stats = app_and_client
    resp = client.get("/api/stats/series?range=bogus")
    assert resp.status_code == 400


def test_series_window_filters_rows_and_populates_forecast(app_and_client: Any) -> None:
    app, client, stats = app_and_client
    now = time.time()
    # two rows ~48h old (outside 24h) + four recent rows (inside 24h), rising disk
    timestamps = [
        now - 48 * 3600,
        now - 47 * 3600,
        now - 4 * 3600,
        now - 3 * 3600,
        now - 2 * 3600,
        now - 1 * 3600,
    ]
    for i, ts in enumerate(timestamps):
        m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=now)
        m.last_run_timestamp_seconds = ts
        m.last_run_success = 1
        m.last_run_exit_code = 0
        m.run_duration_seconds = 2.0
        m.files_downloaded_last_run = i
        m.bytes_downloaded_last_run = i * 1000
        m.destination_disk_used_ratio = 0.40 + i * 0.02
        stats.record_run(m)

    all_body = json.loads(client.get("/api/stats/series?range=all").data)
    day_body = json.loads(client.get("/api/stats/series?range=24h").data)

    assert all_body["summary"]["runs"] == 6
    assert day_body["summary"]["runs"] == 4  # only the four within 24h
    assert len(day_body["series"]["points"]) < len(all_body["series"]["points"])

    # >= 3 points -> forecast projects 12 steps, each clamped to <= the 0.9 cap
    projected = all_body["forecast"]["projected"]
    assert len(projected) == 12
    assert all(p["disk"] <= 0.9 + 1e-9 for p in projected)


def test_stats_page_renders(app_and_client: Any) -> None:
    app, client, _stats = app_and_client
    resp = client.get("/stats")
    assert resp.status_code == 200
    assert b"js/stats.js" in resp.data
    assert b"js/chart.umd.min.js" in resp.data
    assert b"data-range" in resp.data  # range selector present


def test_stats_page_has_noscript_fallback(app_and_client: Any) -> None:
    app, client, stats = app_and_client
    _seed(stats, 3)
    resp = client.get("/stats")
    assert b"<noscript>" in resp.data
    assert resp.data.count(b"<tr>") >= 4  # 1 header row + 3 seeded data rows


def _record(
    stats: StatsStore,
    ts: float,
    *,
    success: int,
    reason: str | None = None,
    files: int = 0,
    duration: float = 30.0,
) -> None:
    """records a run the way sync_runner does (every reason, most zero)."""
    m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=ts)
    m.last_run_timestamp_seconds = ts
    m.last_run_success = success
    m.last_run_exit_code = 0 if success else 1
    m.run_duration_seconds = duration
    m.files_downloaded_last_run = files
    m.bytes_downloaded_last_run = files * 1000
    if reason:
        m.record_run_failure(reason)
    stats.record_run(m)


def test_offline_runs_are_counted_separately(app_and_client: Any) -> None:
    """car away all day must not read as a failing sync."""
    _, client, stats = app_and_client
    now = time.time()
    _record(stats, now - 900, success=1, files=4, duration=60.0)
    _record(stats, now - 800, success=1, files=0, duration=20.0)
    _record(stats, now - 700, success=0, reason="http", duration=10.0)
    for i, reason in enumerate(("network", "timeout", "network")):
        _record(stats, now - 600 + i, success=0, reason=reason, duration=3.0)
    body = json.loads(client.get("/api/stats/series?range=24h").data)
    summary = body["summary"]
    assert summary["runs"] == 6
    assert summary["offline"] == 3
    assert summary["reachable_runs"] == 3
    assert summary["success_rate"] == pytest.approx(2 / 3)
    assert summary["avg_duration_seconds"] == pytest.approx(30.0)  # 60, 20, 10
    offline_flags = [p["offline"] for p in body["series"]["points"]]
    assert offline_flags == [False, False, False, True, True, True]


def test_success_rate_is_null_when_no_run_reached_the_dashcam(
    app_and_client: Any,
) -> None:
    _, client, stats = app_and_client
    now = time.time()
    for i in range(3):
        _record(stats, now - 300 + i, success=0, reason="network", duration=3.0)
    summary = json.loads(client.get("/api/stats/series?range=24h").data)["summary"]
    assert summary["offline"] == 3
    assert summary["reachable_runs"] == 0
    assert summary["success_rate"] is None


def test_a_mixed_failure_is_not_offline(app_and_client: Any) -> None:
    """a timeout plus a disk failure is a real failure, not the car away."""
    _, client, stats = app_and_client
    now = time.time()
    m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=now)
    m.last_run_timestamp_seconds = now - 60
    m.last_run_success = 0
    m.run_duration_seconds = 5.0
    m.record_run_failure("timeout")
    m.record_run_failure("disk")
    stats.record_run(m)
    summary = json.loads(client.get("/api/stats/series?range=24h").data)["summary"]
    assert summary["offline"] == 0
    assert summary["success_rate"] == 0.0
