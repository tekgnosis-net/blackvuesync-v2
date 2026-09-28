"""progress publisher: thread-safe state for sync run and per-file download progress."""

from __future__ import annotations

import contextlib
import dataclasses
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from typing import ClassVar, Literal


@dataclasses.dataclass(frozen=True)
class FileProgress:  # pylint: disable=too-many-instance-attributes
    """immutable snapshot of a single-file download in progress."""

    filename: str
    recording_base: str
    artifact: Literal["mp4", "thm", "3gf", "gps"]
    direction: Literal["F", "R", "I", "O"] | None
    total_bytes: int
    downloaded_bytes: int
    started_at_monotonic: float
    started_at_wall: float
    updated_at_monotonic: float
    bytes_per_second: float
    eta_seconds: float | None
    state: Literal["starting", "downloading", "complete", "failed"]
    failure_reason: str | None

    @property
    def percent(self) -> float:
        """returns download completion as 0-100; 0 when total is unknown."""
        if self.total_bytes <= 0:
            return 0.0
        return min(100.0, self.downloaded_bytes / self.total_bytes * 100.0)

    @property
    def elapsed_seconds(self) -> float:
        """returns seconds elapsed since download started."""
        return self.updated_at_monotonic - self.started_at_monotonic


@dataclasses.dataclass(frozen=True)
class SyncProgress:  # pylint: disable=too-many-instance-attributes
    """immutable snapshot of an in-progress or completed sync job."""

    job_id: str
    started_at_wall: float
    state: Literal["idle", "running", "complete", "failed"]
    current_file: FileProgress | None
    files_total: int
    files_completed: int
    files_failed: int
    bytes_downloaded_total: int
    last_event_monotonic: float
    # files found already downloaded or recently failed; excluded from files_total
    files_skipped: int = 0

    @classmethod
    def idle(cls) -> SyncProgress:
        """returns an idle sentinel with zeroed fields."""
        now = time.monotonic()
        return cls(
            job_id="",
            started_at_wall=0.0,
            state="idle",
            current_file=None,
            files_total=0,
            files_completed=0,
            files_failed=0,
            bytes_downloaded_total=0,
            last_event_monotonic=now,
        )

    @property
    def percent(self) -> float:
        """returns job completion as 0-100 based on files; 0 when no files known."""
        if self.files_total <= 0:
            return 0.0
        return min(
            100.0,
            (self.files_completed + self.files_failed) / self.files_total * 100.0,
        )


class ProgressPublisher:
    """thread-safe owner of sync progress state.

    the writer api (begin_job, set_total, start_file, update_bytes,
    finish_file, skip_file, end_job) is called from the sync thread. the reader api (snapshot, subscribe) is
    called from flask handlers. updates to in-memory state are unthrottled;
    publishing to subscribers is rate-limited to PUBLISH_HZ.
    """

    PUBLISH_HZ: ClassVar[float] = 5.0
    POST_COMPLETE_RETENTION: ClassVar[float] = 10.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state: SyncProgress = SyncProgress.idle()
        self._subscribers: set[queue.Queue[SyncProgress]] = set()
        self._last_publish_monotonic: float = 0.0
        self._retention_timer: threading.Timer | None = None

    # ------------------------------------------------------------------
    # writer api
    # ------------------------------------------------------------------

    def begin_job(self, files_total: int, job_id: str | None = None) -> str:
        """starts a new sync job; returns the job_id (generated if not provided)."""
        if job_id is None:
            job_id = uuid.uuid4().hex
        now_w = time.time()
        now_m = time.monotonic()
        with self._lock:
            # cancels any pending retention timer from a previous job
            if self._retention_timer is not None:
                self._retention_timer.cancel()
                self._retention_timer = None
            self._state = SyncProgress(
                job_id=job_id,
                started_at_wall=now_w,
                state="running",
                current_file=None,
                files_total=files_total,
                files_completed=0,
                files_failed=0,
                bytes_downloaded_total=0,
                last_event_monotonic=now_m,
            )
            self._force_publish(self._state)
        return job_id

    def set_total(self, files_total: int) -> None:
        """sets the number of files the running job considers for download."""
        with self._lock:
            self._state = dataclasses.replace(
                self._state,
                files_total=max(0, files_total - self._state.files_skipped),
                last_event_monotonic=time.monotonic(),
            )
            self._force_publish(self._state)

    def start_file(
        self,
        filename: str,
        artifact: Literal["mp4", "thm", "3gf", "gps"],
        total_bytes: int,
        direction: Literal["F", "R", "I", "O"] | None = None,
    ) -> None:
        """marks the beginning of a single file download."""
        now_w = time.time()
        now_m = time.monotonic()
        # recording_base is the filename without the extension
        recording_base = filename.rsplit(".", 1)[0] if "." in filename else filename
        file_progress = FileProgress(
            filename=filename,
            recording_base=recording_base,
            artifact=artifact,
            direction=direction,
            total_bytes=total_bytes,
            downloaded_bytes=0,
            started_at_monotonic=now_m,
            started_at_wall=now_w,
            updated_at_monotonic=now_m,
            bytes_per_second=0.0,
            eta_seconds=None,
            state="starting",
            failure_reason=None,
        )
        with self._lock:
            self._state = dataclasses.replace(
                self._state,
                current_file=file_progress,
                last_event_monotonic=now_m,
            )
            self._force_publish(self._state)

    def update_bytes(self, downloaded: int, total_bytes: int = 0) -> None:
        """updates downloaded byte count; throttled publish to subscribers.

        if total_bytes is non-zero it overrides the value set by start_file,
        which is useful when the caller only has content-length at the first
        chunk arrival rather than at start_file time.
        """
        now_m = time.monotonic()
        with self._lock:
            cf = self._state.current_file
            if cf is None:
                return
            effective_total = total_bytes if total_bytes > 0 else cf.total_bytes
            elapsed = now_m - cf.started_at_monotonic
            # exponential weighted moving average for bytes_per_second;
            # avg_bps_since_start is the cumulative average since the file started
            # (not a true instantaneous rate). TODO: switch to a rolling window EWMA.
            if elapsed > 0:
                avg_bps_since_start = downloaded / elapsed
                alpha = 0.3
                new_bps = (
                    alpha * avg_bps_since_start + (1.0 - alpha) * cf.bytes_per_second
                    if cf.bytes_per_second > 0
                    else avg_bps_since_start
                )
            else:
                new_bps = cf.bytes_per_second
            remaining = effective_total - downloaded
            eta = remaining / new_bps if new_bps > 0 and remaining > 0 else None
            updated_file = dataclasses.replace(
                cf,
                total_bytes=effective_total,
                downloaded_bytes=downloaded,
                updated_at_monotonic=now_m,
                bytes_per_second=new_bps,
                eta_seconds=eta,
                state="downloading",
            )
            self._state = dataclasses.replace(
                self._state,
                current_file=updated_file,
                last_event_monotonic=now_m,
            )
            # throttled publish: only emit if enough time has elapsed
            if now_m - self._last_publish_monotonic >= 1.0 / self.PUBLISH_HZ:
                self._publish_to_subscribers(self._state)
                self._last_publish_monotonic = now_m

    def finish_file(self, success: bool, reason: str | None = None) -> None:
        """transitions current file to complete or failed; bumps aggregate counts."""
        now_m = time.monotonic()
        with self._lock:
            cf = self._state.current_file
            if cf is None:
                return
            final_file = dataclasses.replace(
                cf,
                state="complete" if success else "failed",
                failure_reason=reason,
                updated_at_monotonic=now_m,
            )
            new_completed = self._state.files_completed + (1 if success else 0)
            new_failed = self._state.files_failed + (0 if success else 1)
            new_bytes = self._state.bytes_downloaded_total + (
                cf.downloaded_bytes if success else 0
            )
            self._state = dataclasses.replace(
                self._state,
                current_file=final_file,
                files_completed=new_completed,
                files_failed=new_failed,
                bytes_downloaded_total=new_bytes,
                last_event_monotonic=now_m,
            )
            self._force_publish(self._state)

    def skip_file(self) -> None:
        """records a file that needs no transfer; removes it from files_total."""
        now_m = time.monotonic()
        with self._lock:
            self._state = dataclasses.replace(
                self._state,
                files_total=max(0, self._state.files_total - 1),
                files_skipped=self._state.files_skipped + 1,
                last_event_monotonic=now_m,
            )
            # throttled like update_bytes; skips arrive in fast bursts
            if now_m - self._last_publish_monotonic >= 1.0 / self.PUBLISH_HZ:
                self._publish_to_subscribers(self._state)
                self._last_publish_monotonic = now_m

    def end_job(self, success: bool) -> None:
        """transitions the job to complete or failed; retains snapshot for POST_COMPLETE_RETENTION seconds."""
        now_m = time.monotonic()
        with self._lock:
            self._state = dataclasses.replace(
                self._state,
                state="complete" if success else "failed",
                current_file=None,
                last_event_monotonic=now_m,
            )
            self._force_publish(self._state)
            if self._retention_timer is not None:
                self._retention_timer.cancel()
            # schedules reset to idle after retention window
            t = threading.Timer(
                self.POST_COMPLETE_RETENTION,
                self._reset_to_idle,
                args=(self._state.job_id,),
            )
            t.daemon = True
            t.start()
            self._retention_timer = t

    # ------------------------------------------------------------------
    # reader api
    # ------------------------------------------------------------------

    def snapshot(self) -> SyncProgress:
        """returns the current progress snapshot; safe to call from any thread."""
        with self._lock:
            return self._state

    def subscribe(self) -> Iterator[SyncProgress]:
        """yields snapshots as they are published; drops frames if consumer is slow.

        the generator blocks for up to 30 seconds between items before yielding
        the current state as a heartbeat. callers should handle StopIteration
        to detect cleanup.
        """
        q: queue.Queue[SyncProgress] = queue.Queue(maxsize=2)
        with self._lock:
            self._subscribers.add(q)
            # sends current state immediately on subscribe
            initial = self._state
        try:
            yield initial  # yields outside the lock so writers are never blocked
            while True:
                try:
                    snap = q.get(timeout=30.0)
                except queue.Empty:
                    # heartbeat: snapshot under lock, yield outside so the sync
                    # thread is never blocked waiting for a slow SSE consumer.
                    with self._lock:
                        snap = self._state
                yield snap
        finally:
            with self._lock:
                self._subscribers.discard(q)

    # ------------------------------------------------------------------
    # internal helpers
    # ------------------------------------------------------------------

    def _reset_to_idle(self, job_id: str) -> None:
        """resets state to idle after the post-complete retention window.

        no-ops when a newer job has begun since the timer was scheduled; a
        cancelled timer may still fire if it was already waiting on the lock.
        """
        with self._lock:
            if self._state.job_id != job_id or self._state.state == "running":
                return
            self._retention_timer = None
            self._state = SyncProgress.idle()
            self._force_publish(self._state)

    def _force_publish(self, snapshot: SyncProgress) -> None:
        """publishes snapshot immediately, bypassing the throttle."""
        self._last_publish_monotonic = time.monotonic()
        self._publish_to_subscribers(snapshot)

    def _publish_to_subscribers(self, snapshot: SyncProgress) -> None:
        """puts snapshot in every subscriber queue; drops frames for slow consumers."""
        # snapshot the subscriber set so a concurrent subscribe/unsubscribe
        # cannot mutate the iteration target.
        for sub in list(self._subscribers):
            with contextlib.suppress(queue.Full):
                sub.put_nowait(snapshot)


class _NoopPublisher:
    """no-op publisher sentinel for the cli sync path.

    implements the writer api with no-op methods so sync.py needs no
    conditional checks and stays free of flask imports.
    """

    def begin_job(self, files_total: int = 0, job_id: str | None = None) -> str:
        """no-op begin_job; echoes the supplied job_id or empty string."""
        del files_total  # accepted for api compatibility; ignored by noop
        return job_id or ""

    def start_file(
        self,
        filename: str,
        artifact: Literal["mp4", "thm", "3gf", "gps"],
        total_bytes: int,
        direction: Literal["F", "R", "I", "O"] | None = None,
    ) -> None:
        """no-op start_file."""

    def set_total(self, files_total: int) -> None:
        """no-op set_total."""

    def update_bytes(self, downloaded: int, total_bytes: int = 0) -> None:
        """no-op update_bytes."""

    def skip_file(self) -> None:
        """no-op skip_file."""

    def finish_file(self, success: bool, reason: str | None = None) -> None:
        """no-op finish_file."""

    def end_job(self, success: bool) -> None:
        """no-op end_job."""


__all__ = [
    "FileProgress",
    "SyncProgress",
    "ProgressPublisher",
    "_NoopPublisher",
]
