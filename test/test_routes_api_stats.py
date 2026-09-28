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
