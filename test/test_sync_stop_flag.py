"""tests for the cooperative stop flag in blackvuesync_v2.sync."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.sync_runner import trigger_sync


class TestStopFlag:
    """tests for request_stop / clear_stop / is_stop_requested."""

    def setup_method(self) -> None:
        """ensures each test starts with the flag cleared."""
        from blackvuesync_v2.sync import clear_stop

        clear_stop()

    def test_initial_state_is_not_requested(self) -> None:
        from blackvuesync_v2.sync import is_stop_requested

        assert is_stop_requested() is False

    def test_request_stop_sets_flag(self) -> None:
        from blackvuesync_v2.sync import is_stop_requested, request_stop

        request_stop()
        assert is_stop_requested() is True

    def test_clear_stop_resets_flag(self) -> None:
        from blackvuesync_v2.sync import clear_stop, is_stop_requested, request_stop

        request_stop()
        clear_stop()
        assert is_stop_requested() is False

    def test_request_stop_is_idempotent(self) -> None:
        """calling request_stop twice keeps the flag set."""
        from blackvuesync_v2.sync import is_stop_requested, request_stop

        request_stop()
        request_stop()
        assert is_stop_requested() is True


class TestTriggerSyncClearsStopFlag:
    """tests that trigger_sync clears the stop flag before spawning the thread."""

    def test_trigger_sync_clears_stale_stop_flag(self) -> None:
        """if a previous run left the flag set, trigger_sync clears it before
        the next run so we don't immediately abort.

        captures the flag value as observed from inside the daemon thread
        and asserts on it from the main thread. an in-thread assert would
        be swallowed by threading.excepthook and never fail the test.
        """
        from blackvuesync_v2.sync import is_stop_requested, request_stop

        # simulate a stale stop flag from a previous run
        request_stop()
        assert is_stop_requested() is True

        # trigger_sync should clear the flag before spawning
        publisher = ProgressPublisher()
        settings = MagicMock()
        settings.connection.address = "192.168.1.1"
        settings.connection.timeout_seconds = 5.0
        settings.system.destination = "/tmp/bvs-stop-test"
        settings.system.dry_run = False
        settings.sync.grouping = "none"
        settings.sync.priority = "date"
        settings.sync.include = None
        settings.sync.exclude = None
        settings.retention.max_used_disk_percent = 90

        observed: list[bool] = []
        thread_done = threading.Event()

        def _capture(
            _s: object,
            p: ProgressPublisher,
            *,
            job_id: str,
            stats_store: object = None,  # noqa: ARG001
            state_dir: object = None,  # noqa: ARG001
        ) -> None:
            """captures the flag value the thread sees; signals via Event."""
            observed.append(is_stop_requested())
            p.begin_job(0, job_id=job_id)
            p.end_job(success=True)
            thread_done.set()

        with patch("blackvuesync_v2.server.sync_runner._do_sync", side_effect=_capture):
            result = trigger_sync(settings, publisher)
            # wait deterministically for the thread to finish; 2s ceiling
            assert thread_done.wait(timeout=2.0), "daemon thread did not finish"

        assert result["status"] == "started"
        assert observed == [False], f"thread observed stale flag: {observed}"
