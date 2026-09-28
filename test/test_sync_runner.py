"""tests for sync_runner: locking, daemon thread lifecycle, 409-on-already-running."""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.sync_runner import _sync_lock, trigger_sync

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_sync_lock() -> Generator[None, None, None]:
    """ensures the sync lock is released between tests."""
    yield
    # release the lock if a test left it acquired
    if _sync_lock.locked():
        with contextlib.suppress(RuntimeError):
            _sync_lock.release()


def _make_settings(**overrides: Any) -> MagicMock:
    """creates a minimal mock settings object for sync_runner tests."""
    s = MagicMock()
    s.connection.address = "192.168.1.1"
    s.connection.timeout_seconds = 5.0
    s.system.destination = "/tmp/bvs-test-dest"
    s.system.dry_run = False
    s.sync.grouping = "none"
    s.sync.priority = "date"
    s.sync.include = None
    s.sync.exclude = None
    s.retention.max_used_disk_percent = 90
    for key, val in overrides.items():
        setattr(s, key, val)
    return s


def _noop(
    _settings: Any,
    pub: ProgressPublisher,
    *,
    job_id: str,
    stats_store: Any = None,  # noqa: ARG001
) -> None:
    """no-op _do_sync stub; simulates sync.py owning begin_job/end_job."""
    pub.begin_job(0, job_id=job_id)
    pub.end_job(success=True)


def _slow_noop(
    _settings: Any,
    pub: ProgressPublisher,
    *,
    job_id: str,
    started: threading.Event,
    proceed: threading.Event,
) -> None:
    """slow _do_sync stub; waits for proceed before ending the job."""
    pub.begin_job(0, job_id=job_id)
    started.set()
    proceed.wait(timeout=5.0)
    pub.end_job(success=True)


# ---------------------------------------------------------------------------
# trigger_sync: basic behavior
# ---------------------------------------------------------------------------


class TestTriggerSync:
    """tests for trigger_sync return values and locking behavior."""

    def test_first_call_returns_started(self) -> None:
        pub = ProgressPublisher()
        settings = _make_settings()

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_noop):
            result = trigger_sync(settings, pub)

        time.sleep(0.05)  # let the thread finish
        assert result["status"] == "started"
        assert len(result["job_id"]) == 32

    def test_second_concurrent_call_returns_already_running(self) -> None:
        pub = ProgressPublisher()
        settings = _make_settings()
        started = threading.Event()
        proceed = threading.Event()

        def _slow(
            s: Any,
            p: ProgressPublisher,
            *,
            job_id: str,  # noqa: ARG001
            stats_store: Any = None,  # noqa: ARG001
        ) -> None:
            _slow_noop(s, p, job_id=job_id, started=started, proceed=proceed)

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_slow):
            result1 = trigger_sync(settings, pub)
            started.wait(timeout=2.0)  # wait until the thread is running
            result2 = trigger_sync(settings, pub)

        proceed.set()  # let the first sync complete
        time.sleep(0.1)

        assert result1["status"] == "started"
        assert result2["status"] == "already_running"

    def test_already_running_returns_current_job_id(self) -> None:
        pub = ProgressPublisher()
        settings = _make_settings()
        started = threading.Event()
        proceed = threading.Event()

        def _slow(
            s: Any,
            p: ProgressPublisher,
            *,
            job_id: str,  # noqa: ARG001
            stats_store: Any = None,  # noqa: ARG001
        ) -> None:
            _slow_noop(s, p, job_id=job_id, started=started, proceed=proceed)

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_slow):
            result1 = trigger_sync(settings, pub)
            started.wait(timeout=2.0)
            result2 = trigger_sync(settings, pub)

        proceed.set()
        time.sleep(0.1)

        assert result1["job_id"] == result2["job_id"]

    def test_after_completion_new_trigger_succeeds(self) -> None:
        pub = ProgressPublisher()
        settings = _make_settings()

        def _fast(
            _s: Any,
            p: ProgressPublisher,
            *,
            job_id: str,  # noqa: ARG001
            stats_store: Any = None,  # noqa: ARG001
        ) -> None:
            p.begin_job(0, job_id=job_id)
            time.sleep(0.05)
            p.end_job(success=True)

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_fast):
            result1 = trigger_sync(settings, pub)
            assert result1["status"] == "started"

            # wait for the first sync to complete and release the lock
            time.sleep(0.2)

            pub2 = ProgressPublisher()
            result2 = trigger_sync(settings, pub2)

        assert result2["status"] == "started"
        assert result1["job_id"] != result2["job_id"]

    def test_spawned_thread_is_daemon(self) -> None:
        pub = ProgressPublisher()
        settings = _make_settings()
        thread_ref: list[threading.Thread] = []
        proceed = threading.Event()

        def _record(
            _s: Any,
            p: ProgressPublisher,
            *,
            job_id: str,  # noqa: ARG001
            stats_store: Any = None,  # noqa: ARG001
        ) -> None:
            p.begin_job(0, job_id=job_id)
            thread_ref.append(threading.current_thread())
            proceed.wait(timeout=5.0)
            p.end_job(success=True)

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_record):
            trigger_sync(settings, pub)
            time.sleep(0.1)  # give the thread time to start

        proceed.set()
        time.sleep(0.1)

        assert len(thread_ref) == 1
        assert thread_ref[0].daemon is True


def test_do_sync_records_a_row_and_finalizes_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    import blackvuesync_v2.server.sync_runner as runner
    import blackvuesync_v2.sync as _sync
    from blackvuesync_v2.server.progress import ProgressPublisher
    from blackvuesync_v2.server.stats_store import StatsStore

    destination = tmp_path / "rec"
    destination.mkdir()

    monkeypatch.setattr(_sync, "ensure_destination", lambda _d: None)
    monkeypatch.setattr(_sync, "lock", lambda _d: 1)
    monkeypatch.setattr(_sync, "unlock", lambda _fd: None)
    monkeypatch.setattr(_sync, "clean_destination", lambda _d, _g: None)

    def fake_sync(
        _address: Any,
        _dest: Any,
        _grouping: Any,
        _prio: Any,
        _include: Any,
        _exclude: Any,
        metrics: Any = None,
        publisher: Any = None,  # noqa: ARG001
        job_id: Any = None,  # noqa: ARG001
    ) -> None:
        if metrics is not None:
            metrics.record_file_download(123)
            metrics.record_destination_disk_usage(50, 100)

    monkeypatch.setattr(_sync, "sync", fake_sync)

    settings = types.SimpleNamespace(
        connection=types.SimpleNamespace(address="1.2.3.4", timeout_seconds=10.0),
        system=types.SimpleNamespace(destination=str(destination), dry_run=False),
        sync=types.SimpleNamespace(
            grouping="none",
            priority="date",
            include=(),
            exclude=(),
            retry_failed_after="1d",
            skip_metadata=(),
            affinity_key=None,
        ),
        retention=types.SimpleNamespace(keep="", max_used_disk_percent=90),
        metrics=types.SimpleNamespace(
            file=None,
            pushgateway_url=None,
            job="blackvuesync",
            instance=None,
            state_file=str(tmp_path / "metrics-state.json"),
        ),
        stats=types.SimpleNamespace(retention_days=365),
    )
    store = StatsStore(str(tmp_path / "stats.db"))
    pub = ProgressPublisher()

    runner._do_sync(settings, pub, job_id="abc", stats_store=store)

    rows = store.query()
    assert len(rows) == 1
    assert rows[0].files == 1
    assert rows[0].bytes == 123
    assert rows[0].disk_used_ratio == 0.5
    assert rows[0].success == 1


def test_do_sync_records_failure_row_when_sync_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    import blackvuesync_v2.server.sync_runner as runner
    import blackvuesync_v2.sync as _sync
    from blackvuesync_v2.server.progress import ProgressPublisher
    from blackvuesync_v2.server.stats_store import StatsStore

    destination = tmp_path / "rec"
    destination.mkdir()

    monkeypatch.setattr(_sync, "ensure_destination", lambda _d: None)
    monkeypatch.setattr(_sync, "lock", lambda _d: 1)
    monkeypatch.setattr(_sync, "unlock", lambda _fd: None)
    monkeypatch.setattr(_sync, "clean_destination", lambda _d, _g: None)

    def fake_sync(
        _address: Any,
        _dest: Any,
        _grouping: Any,
        _prio: Any,
        _include: Any,
        _exclude: Any,
        metrics: Any = None,  # noqa: ARG001
        publisher: Any = None,  # noqa: ARG001
        job_id: Any = None,  # noqa: ARG001
    ) -> None:
        raise RuntimeError("dashcam unavailable: network")

    monkeypatch.setattr(_sync, "sync", fake_sync)

    settings = types.SimpleNamespace(
        connection=types.SimpleNamespace(address="1.2.3.4", timeout_seconds=10.0),
        system=types.SimpleNamespace(destination=str(destination), dry_run=False),
        sync=types.SimpleNamespace(
            grouping="none",
            priority="date",
            include=(),
            exclude=(),
            retry_failed_after="1d",
            skip_metadata=(),
            affinity_key=None,
        ),
        retention=types.SimpleNamespace(keep="", max_used_disk_percent=90),
        metrics=types.SimpleNamespace(
            file=None,
            pushgateway_url=None,
            job="blackvuesync",
            instance=None,
            state_file=str(tmp_path / "metrics-state.json"),
        ),
        stats=types.SimpleNamespace(retention_days=365),
    )
    store = StatsStore(str(tmp_path / "stats.db"))
    pub = ProgressPublisher()

    runner._do_sync(settings, pub, job_id="abc", stats_store=store)

    rows = store.query()
    assert len(rows) == 1
    assert rows[0].success == 0
    assert rows[0].exit_code == 1
    assert rows[0].failures.get("network", 0) >= 1


def test_do_sync_fails_without_calling_sync_when_address_is_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import types

    import blackvuesync_v2.server.sync_runner as runner
    import blackvuesync_v2.sync as _sync
    from blackvuesync_v2.server.progress import ProgressPublisher
    from blackvuesync_v2.server.stats_store import StatsStore

    destination = tmp_path / "rec"
    destination.mkdir()
    called: list[bool] = []
    monkeypatch.setattr(_sync, "sync", lambda *_a, **_k: called.append(True))

    settings = types.SimpleNamespace(
        connection=types.SimpleNamespace(address="", timeout_seconds=10.0),
        system=types.SimpleNamespace(destination=str(destination), dry_run=False),
        sync=types.SimpleNamespace(
            grouping="none",
            priority="date",
            include=(),
            exclude=(),
            retry_failed_after="1d",
            skip_metadata=(),
            affinity_key=None,
        ),
        retention=types.SimpleNamespace(keep="", max_used_disk_percent=90),
        metrics=types.SimpleNamespace(
            file=None,
            pushgateway_url=None,
            job="blackvuesync",
            instance=None,
            state_file=str(tmp_path / "metrics-state.json"),
        ),
        stats=types.SimpleNamespace(retention_days=365),
    )
    store = StatsStore(str(tmp_path / "stats.db"))
    pub = ProgressPublisher()

    runner._do_sync(settings, pub, job_id="abc", stats_store=store)

    assert not called
    assert pub.snapshot().state == "failed"
    rows = store.query()
    assert len(rows) == 1
    assert rows[0].success == 0
