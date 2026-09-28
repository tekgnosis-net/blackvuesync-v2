"""unit tests for the sqlite per-run stats store."""

from __future__ import annotations

import time
from pathlib import Path

from blackvuesync_v2.metrics import SyncMetrics
from blackvuesync_v2.server.stats_store import RunRow, StatsStore


def _metrics(
    ts: float, *, files: int = 2, byts: int = 100, success: int = 1
) -> SyncMetrics:
    m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=ts)
    m.last_run_timestamp_seconds = ts
    m.last_run_success = success
    m.last_run_exit_code = 0
    m.run_duration_seconds = 1.5
    m.files_downloaded_last_run = files
    m.bytes_downloaded_last_run = byts
    m.destination_disk_used_ratio = 0.42
    m.file_download_failures_last_run = {
        "http": 1,
        "network": 0,
        "timeout": 0,
        "disk": 0,
        "unknown": 0,
    }
    m.last_run_failures = {
        "http": 0,
        "network": 0,
        "timeout": 0,
        "disk": 0,
        "unknown": 0,
    }
    return m


def test_record_and_query_roundtrip(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    store.record_run(_metrics(1000.0))
    rows = store.query()
    assert len(rows) == 1
    row = rows[0]
    assert isinstance(row, RunRow)
    assert row.ts_seconds == 1000.0
    assert row.files == 2
    assert row.bytes == 100
    assert row.disk_used_ratio == 0.42
    assert row.failures["http"] == 1


def test_query_orders_ascending_and_filters_since(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    for ts in (3000.0, 1000.0, 2000.0):
        store.record_run(_metrics(ts))
    assert [r.ts_seconds for r in store.query()] == [1000.0, 2000.0, 3000.0]
    assert [r.ts_seconds for r in store.query(since_ts=2000.0)] == [2000.0, 3000.0]


def test_record_run_skips_when_no_timestamp(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    m = SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=0.0)
    m.last_run_timestamp_seconds = None  # not finalized
    store.record_run(m)
    assert store.query() == []


def test_record_is_idempotent_on_same_ts(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    store.record_run(_metrics(1000.0, files=2))
    store.record_run(_metrics(1000.0, files=9))  # same ts -> replace
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].files == 9


def test_prune_deletes_old_rows(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    now = time.time()
    store.record_run(_metrics(now - 10 * 86400))  # 10 days old
    store.record_run(_metrics(now - 1 * 86400))  # 1 day old
    deleted = store.prune(retention_days=5)
    assert deleted == 1
    assert [round(r.ts_seconds) for r in store.query()] == [round(now - 86400)]


def test_prune_zero_keeps_all(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    store.record_run(_metrics(time.time() - 9999 * 86400))
    assert store.prune(retention_days=0) == 0
    assert len(store.query()) == 1


def test_dry_run_flag_persisted(tmp_path: Path) -> None:
    store = StatsStore(str(tmp_path / "stats.db"))
    m = _metrics(1000.0)
    m.dry_run = True
    store.record_run(m)
    assert store.query()[0].dry_run == 1


def test_in_memory_store_is_usable() -> None:
    store = StatsStore(":memory:")
    assert store.query() == []  # no rows, must not raise
    store.record_run(_metrics(1000.0, files=4))
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].files == 4
    assert store.prune(retention_days=0) == 0
