"""tests for FileProgress, SyncProgress, and ProgressPublisher."""

from __future__ import annotations

import dataclasses
import threading
import time
from unittest.mock import patch

import pytest

from blackvuesync_v2.server.progress import (
    FileProgress,
    ProgressPublisher,
    SyncProgress,
    _NoopPublisher,
)

# ---------------------------------------------------------------------------
# FileProgress
# ---------------------------------------------------------------------------


class TestFileProgress:
    """tests for the FileProgress frozen dataclass."""

    def _make(self, **kwargs: object) -> FileProgress:
        defaults: dict[str, object] = {
            "filename": "20230101_120000_NF.mp4",
            "recording_base": "20230101_120000_NF",
            "artifact": "mp4",
            "direction": "F",
            "total_bytes": 1000,
            "downloaded_bytes": 500,
            "started_at_monotonic": 0.0,
            "started_at_wall": 1000.0,
            "updated_at_monotonic": 1.0,
            "bytes_per_second": 500.0,
            "eta_seconds": 1.0,
            "state": "downloading",
            "failure_reason": None,
        }
        defaults.update(kwargs)
        return FileProgress(**defaults)  # type: ignore[arg-type]

    def test_percent_halfway(self) -> None:
        fp = self._make(total_bytes=1000, downloaded_bytes=500)
        assert fp.percent == pytest.approx(50.0)

    def test_percent_complete(self) -> None:
        fp = self._make(total_bytes=1000, downloaded_bytes=1000)
        assert fp.percent == pytest.approx(100.0)

    def test_percent_zero_total(self) -> None:
        fp = self._make(total_bytes=0, downloaded_bytes=0)
        assert fp.percent == 0.0

    def test_percent_clamped_at_100(self) -> None:
        # downloaded_bytes can exceed total_bytes in edge cases
        fp = self._make(total_bytes=100, downloaded_bytes=200)
        assert fp.percent == pytest.approx(100.0)

    def test_elapsed_seconds(self) -> None:
        fp = self._make(started_at_monotonic=1.0, updated_at_monotonic=3.5)
        assert fp.elapsed_seconds == pytest.approx(2.5)

    def test_frozen(self) -> None:
        fp = self._make()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            fp.filename = "other"  # type: ignore[misc]

    def test_direction_none(self) -> None:
        fp = self._make(direction=None)
        assert fp.direction is None

    def test_failure_state(self) -> None:
        fp = self._make(state="failed", failure_reason="TimeoutError")
        assert fp.failure_reason == "TimeoutError"


# ---------------------------------------------------------------------------
# SyncProgress
# ---------------------------------------------------------------------------


class TestSyncProgress:
    """tests for the SyncProgress frozen dataclass."""

    def test_idle_factory(self) -> None:
        sp = SyncProgress.idle()
        assert sp.state == "idle"
        assert sp.job_id == ""
        assert sp.current_file is None
        assert sp.files_total == 0
        assert sp.files_completed == 0

    def test_percent_halfway(self) -> None:
        sp = dataclasses.replace(
            SyncProgress.idle(),
            files_total=10,
            files_completed=5,
            files_failed=0,
            state="running",
        )
        assert sp.percent == pytest.approx(50.0)

    def test_percent_zero_when_no_files(self) -> None:
        sp = SyncProgress.idle()
        assert sp.percent == 0.0

    def test_percent_includes_failed(self) -> None:
        sp = dataclasses.replace(
            SyncProgress.idle(),
            files_total=10,
            files_completed=3,
            files_failed=2,
            state="running",
        )
        assert sp.percent == pytest.approx(50.0)

    def test_frozen(self) -> None:
        sp = SyncProgress.idle()
        with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
            sp.job_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ProgressPublisher -- basic state machine
# ---------------------------------------------------------------------------


class TestProgressPublisherStateMachine:
    """tests for ProgressPublisher writer api state transitions."""

    def test_initial_state_is_idle(self) -> None:
        pub = ProgressPublisher()
        snap = pub.snapshot()
        assert snap.state == "idle"

    def test_begin_job_transitions_to_running(self) -> None:
        pub = ProgressPublisher()
        job_id = pub.begin_job(files_total=5)
        snap = pub.snapshot()
        assert snap.state == "running"
        assert snap.job_id == job_id
        assert len(job_id) == 32  # uuid4().hex

    def test_begin_job_returns_unique_ids(self) -> None:
        pub = ProgressPublisher()
        id1 = pub.begin_job(5)
        pub.end_job(success=True)
        time.sleep(0.01)  # let retention timer be scheduled
        id2 = pub.begin_job(5)
        assert id1 != id2

    def test_start_file_populates_current_file(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(files_total=1)
        pub.start_file("20230101_120000_NF.mp4", "mp4", 1024)
        snap = pub.snapshot()
        assert snap.current_file is not None
        assert snap.current_file.filename == "20230101_120000_NF.mp4"
        assert snap.current_file.artifact == "mp4"
        assert snap.current_file.total_bytes == 1024
        assert snap.current_file.state == "starting"

    def test_start_file_with_direction_propagates_to_file_progress(self) -> None:
        """start_file with a direction produces a FileProgress with that direction."""
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("20230101_120000_NF.mp4", "mp4", 0, direction="F")
        snap = pub.snapshot()
        assert snap.current_file is not None
        assert snap.current_file.direction == "F"

    def test_start_file_without_direction_leaves_direction_none(self) -> None:
        """start_file without direction results in direction=None on FileProgress."""
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("20230101_120000_NF.3gf", "3gf", 0)
        snap = pub.snapshot()
        assert snap.current_file is not None
        assert snap.current_file.direction is None

    def test_update_bytes_updates_current_file(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("20230101_120000_NF.mp4", "mp4", 1024)
        pub.update_bytes(512)
        snap = pub.snapshot()
        assert snap.current_file is not None
        assert snap.current_file.downloaded_bytes == 512
        assert snap.current_file.state == "downloading"

    def test_finish_file_success_increments_completed(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(2)
        pub.start_file("a.mp4", "mp4", 100)
        pub.update_bytes(100)
        pub.finish_file(success=True)
        snap = pub.snapshot()
        assert snap.files_completed == 1
        assert snap.files_failed == 0
        assert snap.bytes_downloaded_total == 100

    def test_finish_file_failure_increments_failed(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(2)
        pub.start_file("a.mp4", "mp4", 100)
        pub.finish_file(success=False, reason="TimeoutError")
        snap = pub.snapshot()
        assert snap.files_completed == 0
        assert snap.files_failed == 1
        assert snap.bytes_downloaded_total == 0
        assert snap.current_file is not None
        assert snap.current_file.failure_reason == "TimeoutError"

    def test_end_job_success_transitions_to_complete(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.end_job(success=True)
        snap = pub.snapshot()
        assert snap.state == "complete"
        assert snap.current_file is None

    def test_end_job_failure_transitions_to_failed(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.end_job(success=False)
        snap = pub.snapshot()
        assert snap.state == "failed"

    def test_update_bytes_noop_when_no_current_file(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        # no start_file called
        pub.update_bytes(100)
        snap = pub.snapshot()
        assert snap.current_file is None

    def test_finish_file_noop_when_no_current_file(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.finish_file(success=True)
        snap = pub.snapshot()
        assert snap.files_completed == 0


# ---------------------------------------------------------------------------
# ProgressPublisher -- post-complete retention
# ---------------------------------------------------------------------------


class TestPostCompleteRetention:
    """tests for the 10-second post-complete retention window."""

    def test_state_remains_complete_during_retention(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.end_job(success=True)
        # immediately after end_job, still complete
        assert pub.snapshot().state == "complete"

    def test_retention_timer_resets_to_idle(self) -> None:
        pub = ProgressPublisher()
        # override retention to be very short
        with patch.object(ProgressPublisher, "POST_COMPLETE_RETENTION", 0.05):
            pub.begin_job(1)
            pub.end_job(success=True)
            assert pub.snapshot().state == "complete"
            time.sleep(0.15)
            assert pub.snapshot().state == "idle"

    def test_new_begin_job_cancels_retention_timer(self) -> None:
        pub = ProgressPublisher()
        with patch.object(ProgressPublisher, "POST_COMPLETE_RETENTION", 0.2):
            pub.begin_job(1)
            pub.end_job(success=True)
            assert pub.snapshot().state == "complete"
            # start a new job before retention expires
            pub.begin_job(1)
            time.sleep(0.3)
            # state should be running (or later), not idle from the old timer
            assert pub.snapshot().state != "idle"

    def test_stale_retention_timer_does_not_reset_new_job(self) -> None:
        """stale timer from job-1 must not reset state while job-2 is running.

        regression test for the double end_job / leaked timer bug: with a very
        short retention window, begin_job -> end_job -> begin_job (new job) ->
        wait longer than the retention window -> state must still be running.
        """
        pub = ProgressPublisher()
        with patch.object(ProgressPublisher, "POST_COMPLETE_RETENTION", 0.1):
            pub.begin_job(1)
            pub.end_job(success=True)
            # immediately start a second job; begin_job must cancel the timer
            pub.begin_job(1)
            # wait well past what the first timer would have fired
            time.sleep(0.25)
            # the second job is still running; must not have been reset to idle
            assert pub.snapshot().state == "running"


# ---------------------------------------------------------------------------
# ProgressPublisher -- lock not held across yield
# ---------------------------------------------------------------------------


class TestSubscribeLockBehavior:
    """tests that subscribe() does not hold the lock while yielded."""

    def test_update_bytes_completes_quickly_while_subscriber_suspended_at_heartbeat(
        self,
    ) -> None:
        """update_bytes must complete within 50 ms even when a subscriber is
        suspended mid-heartbeat (i.e. the lock must not be held at the yield
        inside the heartbeat path).

        this is a regression test for the 'yield inside with self._lock' bug.
        the test forces the heartbeat branch by patching queue.Queue.get so it
        raises queue.Empty, then parks the consumer thread before it calls
        next(). with the fix the sync thread's update_bytes finishes quickly
        because the lock is not held across the yield.
        """
        import queue as _queue  # pylint: disable=import-outside-toplevel
        from collections.abc import Generator  # pylint: disable=import-outside-toplevel

        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("a.mp4", "mp4", 10_000)

        consumer_at_yield = threading.Event()
        let_consumer_continue = threading.Event()

        def _patched_get(
            _self: _queue.Queue[SyncProgress],
            *_args: object,
            **_kwargs: object,
        ) -> SyncProgress:
            """always raise Empty to force the heartbeat path in subscribe()."""
            raise _queue.Empty

        # subscribe() returns an Iterator; cast to Generator so we can call .close()
        gen: Generator[SyncProgress, None, None] = pub.subscribe()  # type: ignore[assignment]
        # consume the initial snapshot; generator is now parked at q.get()
        next(gen)

        def _consumer() -> None:
            """forces the heartbeat branch, then signals before consuming the snap."""
            with patch("queue.Queue.get", _patched_get):
                # next() triggers the heartbeat branch: lock acquired, snap taken,
                # lock released, then yield. with the bug the lock is held at yield.
                consumer_at_yield.set()
                snap = next(gen)  # noqa: F841  # pylint: disable=unused-variable
            let_consumer_continue.set()

        t = threading.Thread(target=_consumer, daemon=True)
        t.start()
        consumer_at_yield.wait(timeout=2.0)
        # small pause to let the consumer thread enter next() and reach the yield
        time.sleep(0.01)

        # update_bytes must not block waiting for the consumer to call next()
        start = time.monotonic()
        pub.update_bytes(5_000)
        elapsed = time.monotonic() - start

        let_consumer_continue.set()
        t.join(timeout=2.0)
        gen.close()

        assert elapsed < 0.05, f"update_bytes blocked for {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# ProgressPublisher -- throttle
# ---------------------------------------------------------------------------


class TestPublishThrottle:
    """tests for the 5 Hz publish throttle."""

    def test_update_bytes_throttles_subscriber_notifications(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("a.mp4", "mp4", 1_000_000)

        # subscribe to get future messages
        events: list[SyncProgress] = []
        done_event = threading.Event()

        def _reader() -> None:
            gen = pub.subscribe()
            # consume initial snapshot from subscribe()
            next(gen)
            start = time.monotonic()
            while time.monotonic() - start < 1.0:
                try:
                    snap = next(gen)
                    events.append(snap)
                except StopIteration:
                    break
            done_event.set()

        t = threading.Thread(target=_reader, daemon=True)
        t.start()

        # inject 1000 updates within 1 second
        for i in range(1, 1001):
            pub.update_bytes(i)

        done_event.wait(timeout=2.0)

        # at 5 Hz over 1 second, expect at most ~8 publish events
        # (5 Hz * 1s = 5, plus initial snap + small timing tolerance)
        assert len(events) <= 8, f"expected <= 8, got {len(events)}"


# ---------------------------------------------------------------------------
# ProgressPublisher -- subscriber drop semantics
# ---------------------------------------------------------------------------


class TestSubscriberDropSemantics:
    """tests that the bounded queue drops frames for slow consumers."""

    def test_slow_consumer_does_not_block_publisher(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("a.mp4", "mp4", 10000)

        # create a subscriber but don't consume from it
        gen = pub.subscribe()
        next(gen)  # consume initial snapshot

        # rapid updates should not block even though the queue is full
        start = time.monotonic()
        for i in range(1, 101):
            pub.update_bytes(i * 100)
        elapsed = time.monotonic() - start

        assert elapsed < 1.0, f"updates took too long: {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# ProgressPublisher -- snapshot is frozen (no torn reads)
# ---------------------------------------------------------------------------


class TestSnapshotConsistency:
    """tests that snapshots are always consistent under concurrent writes."""

    def test_concurrent_updates_produce_consistent_snapshots(self) -> None:
        pub = ProgressPublisher()
        pub.begin_job(1)
        pub.start_file("a.mp4", "mp4", 100_000)

        errors: list[str] = []

        def _writer() -> None:
            for i in range(500):
                pub.update_bytes(i * 200)

        def _reader() -> None:
            for _ in range(500):
                snap = pub.snapshot()
                # the snapshot is a frozen dataclass; reading is always consistent
                if snap.current_file is not None:
                    # percent must be 0-100
                    p = snap.current_file.percent
                    if not (0.0 <= p <= 100.0):
                        errors.append(f"invalid percent: {p}")

        writer = threading.Thread(target=_writer, daemon=True)
        reader = threading.Thread(target=_reader, daemon=True)
        writer.start()
        reader.start()
        writer.join(timeout=5.0)
        reader.join(timeout=5.0)
        assert not errors, f"consistency errors: {errors}"


# ---------------------------------------------------------------------------
# _NoopPublisher
# ---------------------------------------------------------------------------


class TestNoopPublisher:
    """tests that _NoopPublisher silently accepts all writer api calls."""

    def test_begin_job_returns_empty_string(self) -> None:
        noop = _NoopPublisher()
        result = noop.begin_job(5)
        assert result == ""

    def test_all_methods_callable(self) -> None:
        noop = _NoopPublisher()
        noop.begin_job(3)
        noop.start_file("a.mp4", "mp4", 1024, direction="F")
        noop.update_bytes(512)
        noop.finish_file(success=True)
        noop.end_job(success=True)
