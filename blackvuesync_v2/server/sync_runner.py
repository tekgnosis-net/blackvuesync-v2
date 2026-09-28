"""sync_runner: spawns run_sync in a daemon thread under a process-wide lock."""

from __future__ import annotations

import contextlib
import datetime
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blackvuesync_v2.server.progress import ProgressPublisher

if TYPE_CHECKING:
    from blackvuesync_v2.server.stats_store import StatsStore

logger = logging.getLogger(__name__)

# process-wide lock; held for the duration of an active sync; non-reentrant
# so a second trigger while running returns "already_running" immediately.
_sync_lock = threading.Lock()

# reference to the current sync thread; useful for diagnostics
_current_thread: threading.Thread | None = None

# job_id of the sync holding _sync_lock; the publisher only learns it once
# the sync thread begins the job, so a 409 reports it from here.
_current_job_id: str = ""


def trigger_sync(
    settings: Any,
    publisher: ProgressPublisher,
    stats_store: StatsStore | None = None,
    state_dir: Path | None = None,
) -> dict[str, str]:
    """triggers a sync in a background daemon thread; returns a status dict.

    returns {"status": "started", "job_id": "<id>"} when the sync was
    successfully scheduled, or {"status": "already_running", "job_id": "<id>"}
    when a sync is already active. the caller maps "already_running" to 409.

    the publisher is the sole source of sync state for api consumers; the
    sync thread calls publisher.end_job() in its finally block so the
    snapshot transitions to complete/failed after the run. state_dir (the
    settings file's directory) locates the default metrics state file.
    """
    global _current_thread, _current_job_id  # pylint: disable=global-statement

    if not _sync_lock.acquire(blocking=False):  # pylint: disable=consider-using-with
        # a sync is already running; the publisher may not have begun its job yet
        running_job_id = _current_job_id or publisher.snapshot().job_id
        return {"status": "already_running", "job_id": running_job_id}

    # clears any leftover stop flag from a previous run; the next request to
    # /api/sync/stop sets it again on demand.
    # pylint: disable=import-outside-toplevel
    from blackvuesync_v2.sync import clear_stop

    clear_stop()
    # pylint: enable=import-outside-toplevel

    # pre-generate job_id before spawning the thread so the 202 response and
    # the publisher state always agree on the same id.
    job_id = uuid.uuid4().hex
    _current_job_id = job_id

    def _run() -> None:
        """runs sync under the lock; releases lock in finally."""
        try:
            _do_sync(
                settings,
                publisher,
                job_id=job_id,
                stats_store=stats_store,
                state_dir=state_dir,
            )
        finally:
            with contextlib.suppress(Exception):
                _sync_lock.release()

    t = threading.Thread(target=_run, name=f"sync-{job_id[:8]}", daemon=True)
    _current_thread = t
    t.start()
    return {"status": "started", "job_id": job_id}


def _do_sync(  # pylint: disable=too-many-locals,too-many-statements
    settings: Any,
    publisher: ProgressPublisher,
    *,
    job_id: str,
    stats_store: StatsStore | None = None,
    state_dir: Path | None = None,
) -> None:
    """performs the actual sync on the daemon thread.

    builds a SyncMetrics, passes it to sync() (which records downloads into
    it), then finalizes and persists it: saves metrics state and emits
    prometheus metrics (this is what gives serve mode metrics at all), and
    records the run into the stats store + prunes when a store is supplied.
    """
    # pylint: disable=import-outside-toplevel
    import socket

    import blackvuesync_v2.sync as _sync
    from blackvuesync_v2.metrics import (
        SyncMetrics,
        classify_run_failure,
        count_failed_marker_files,
        emit_metrics,
        load_metrics_state,
        save_metrics_state,
    )
    from blackvuesync_v2.sync import (
        clean_destination,
        ensure_destination,
        lock,
        sync,
        unlock,
    )

    # pylint: enable=import-outside-toplevel

    destination = settings.system.destination
    state_file = _metrics_state_file(settings, state_dir)
    lf_fd = None
    metrics: SyncMetrics | None = None
    sync_success = False
    try:
        address = settings.connection.address
        grouping = settings.sync.grouping
        priority = settings.sync.priority
        include = settings.sync.include or None
        exclude = settings.sync.exclude or None
        timeout = settings.connection.timeout_seconds

        _sync.socket_timeout = timeout
        _sync.dry_run = settings.system.dry_run
        _sync.max_disk_used_percent = settings.retention.max_used_disk_percent

        socket.setdefaulttimeout(timeout)

        metrics = SyncMetrics(
            run_start_monotonic=time.perf_counter(),
            run_start_timestamp=time.time(),
            dry_run=settings.system.dry_run,
            metrics_job=settings.metrics.job,
            metrics_instance=settings.metrics.instance or address,
            last_successful_file_pull_timestamp_seconds=(
                load_metrics_state(state_file) if state_file else None
            ),
        )

        if not address:
            raise UserWarning(
                "dashcam address is not set; set it under Settings > Connection"
            )

        _apply_sync_settings(settings)

        ensure_destination(destination)
        lf_fd = lock(destination)

        try:
            sync(
                address,
                destination,
                grouping,
                priority,
                include,
                exclude,
                metrics=metrics,
                publisher=publisher,
                job_id=job_id,
            )
            sync_success = True
        finally:
            clean_destination(destination, grouping)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.exception("sync_runner: sync failed")
        if metrics is not None:
            with contextlib.suppress(Exception):
                metrics.record_run_failure(classify_run_failure(exc))
    finally:
        if lf_fd is not None:
            with contextlib.suppress(Exception):
                unlock(lf_fd)
        # failures before sync() began the job (bad settings, lock contention)
        # still surface as a failed job under the id returned to the caller.
        if publisher.snapshot().job_id != job_id:
            publisher.begin_job(0, job_id=job_id)
            publisher.end_job(success=False)
        if metrics is not None:
            with contextlib.suppress(Exception):
                metrics.failed_marker_files = count_failed_marker_files(destination)
            metrics.finalize(0 if sync_success else 1, sync_success)
            if state_file:
                with contextlib.suppress(Exception):
                    save_metrics_state(state_file, metrics)
            with contextlib.suppress(Exception):
                emit_metrics(
                    metrics,
                    settings.metrics.file,
                    settings.metrics.pushgateway_url,
                    settings.connection.timeout_seconds,
                )
            if stats_store is not None:
                try:
                    stats_store.record_run(metrics)
                    stats_store.prune(settings.stats.retention_days)
                except Exception:  # pylint: disable=broad-exception-caught
                    logger.warning(
                        "sync_runner: failed to record run stats", exc_info=True
                    )


def _metrics_state_file(settings: Any, state_dir: Path | None) -> str | None:
    """returns where metrics state is kept, or None when it is not needed.

    the state only feeds prometheus output, so nothing is read or written
    while metrics are disabled. an empty state_file means metrics-state.json
    next to settings.json.
    """
    metrics = settings.metrics
    if not (metrics.file or metrics.pushgateway_url):
        return None
    if metrics.state_file:
        return str(metrics.state_file)
    return str(state_dir / "metrics-state.json") if state_dir else None


def _apply_sync_settings(settings: Any) -> None:
    """copies the per-run settings the cli passes as flags onto sync.py's globals.

    today is refreshed first because calc_cutoff_date reads it and a
    long-running server would otherwise keep the date it was imported on.
    """
    # pylint: disable-next=import-outside-toplevel
    import blackvuesync_v2.sync as sync_module

    sync_module.today = datetime.date.today()
    keep = settings.retention.keep
    # an empty keep means recordings are kept forever
    sync_module.cutoff_date = sync_module.calc_cutoff_date(keep) if keep else None
    sync_module.retry_failed_after = sync_module.parse_duration(
        settings.sync.retry_failed_after, label="RETRY_FAILED_AFTER"
    )
    sync_module.skip_metadata = set(settings.sync.skip_metadata)
    sync_module.affinity_key = settings.sync.affinity_key or None


__all__ = ["trigger_sync"]
