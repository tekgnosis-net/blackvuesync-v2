"""in-memory ring-buffer logging handler for the live /logs viewer.

models ProgressPublisher's threading skeleton but with don't-drop delivery:
every log line matters, so subscribe() yields *batches of new lines* rather
than a latest-wins snapshot. serve-mode only; sync.py never imports this.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import logging
import queue
import threading
import uuid
from collections import deque
from collections.abc import Callable, Generator, Iterator
from typing import ClassVar


@dataclasses.dataclass(frozen=True)
class LogLine:
    """immutable, json-serializable view of a single log record."""

    seq: int
    ts: str  # iso-8601 utc with a trailing Z
    level: str
    level_no: int
    logger: str
    message: str


def verbosity_token(logging_settings: object) -> str:
    """maps the logging settings (quiet/verbose) to a ui segmented-control token.

    quiet -> "quiet"; verbose 0 -> "normal"; 1 -> "verbose"; >=2 -> "debug".
    accepts any object exposing .quiet and .verbose (duck-typed to avoid a
    settings import here).
    """
    if getattr(logging_settings, "quiet", False):
        return "quiet"
    verbose = getattr(logging_settings, "verbose", 0)
    return {0: "normal", 1: "verbose"}.get(verbose, "debug")


class Subscription(Iterator[list[LogLine]]):
    """iterator over subscriber batches whose close() always unregisters.

    a generator's finally only runs once it has started, so closing an
    unstarted _drain generator alone would leak its queue.
    """

    def __init__(
        self,
        batches: Generator[list[LogLine], None, None],
        unregister: Callable[[], None],
    ) -> None:
        self._batches = batches
        self._unregister = unregister

    def __next__(self) -> list[LogLine]:
        return next(self._batches)

    def close(self) -> None:
        """stops the iterator and unregisters its queue."""
        try:
            self._batches.close()
        finally:
            self._unregister()


# identifies this server process; seq restarts at 1 on every boot, so clients
# reset their dedup watermark when the boot id changes.
BOOT_ID = uuid.uuid4().hex


class LogBuffer(logging.Handler):
    """thread-safe ring buffer of recent log lines with an SSE fan-out.

    attach to the root logger in serve mode. emit() stores a LogLine and
    offers it to every subscriber queue. subscribe() drains its queue in
    batches; a slow consumer that overflows its queue simply drops frames and
    re-syncs from snapshot() on its next reconnect.
    """

    HEARTBEAT_SECONDS: float = 30.0
    _SUBSCRIBER_QUEUE_MAX: ClassVar[int] = 2048

    def __init__(self, capacity: int = 1000) -> None:
        super().__init__()
        self._capacity = max(1, capacity)
        self._lines: deque[LogLine] = deque(maxlen=self._capacity)
        self._lock = threading.RLock()
        self._subscribers: set[queue.Queue[LogLine]] = set()
        self._seq = 0

    @property
    def capacity(self) -> int:
        """returns the current ring-buffer capacity."""
        with self._lock:
            return self._capacity

    def emit(self, record: logging.LogRecord) -> None:
        """stores the record as a LogLine and fans it out to subscribers.

        never raises into the logging call site: a record whose getMessage()
        fails is routed to handleError and dropped.
        """
        try:
            message = record.getMessage()
            ts = (
                datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z")
            )
        except Exception:  # pylint: disable=broad-exception-caught
            self.handleError(record)
            return
        with self._lock:
            self._seq += 1
            line = LogLine(
                seq=self._seq,
                ts=ts,
                level=record.levelname,
                level_no=record.levelno,
                logger=record.name,
                message=message,
            )
            self._lines.append(line)
            # snapshot the subscriber set so a concurrent subscribe/unsubscribe
            # cannot mutate the iteration target.
            for sub in list(self._subscribers):
                with contextlib.suppress(queue.Full):
                    sub.put_nowait(line)

    def snapshot(self) -> list[LogLine]:
        """returns a copy of the current ring contents, oldest first."""
        with self._lock:
            return list(self._lines)

    def set_capacity(self, capacity: int) -> None:
        """resizes the ring buffer in place, truncating to the newest lines."""
        capacity = max(1, capacity)
        with self._lock:
            if capacity == self._capacity:
                return
            self._capacity = capacity
            self._lines = deque(self._lines, maxlen=capacity)

    def subscribe(self) -> Subscription:
        """returns an iterator over batches of lines emitted after this call.

        the subscriber queue is registered eagerly (before the first next()),
        so a line emitted between subscribe() and the first iteration is still
        captured. the stream intentionally does NOT replay the existing buffer:
        callers paint the initial view from snapshot() / the server-rendered
        page, then append streamed lines. an empty list is a heartbeat.
        close() the result to unregister, whether or not it was iterated.
        """
        q: queue.Queue[LogLine] = queue.Queue(maxsize=self._SUBSCRIBER_QUEUE_MAX)
        with self._lock:
            self._subscribers.add(q)
        return Subscription(self._drain(q), lambda: self._unregister(q))

    def _unregister(self, q: queue.Queue[LogLine]) -> None:
        """removes one subscriber queue from the fan-out set."""
        with self._lock:
            self._subscribers.discard(q)

    def _drain(self, q: queue.Queue[LogLine]) -> Generator[list[LogLine], None, None]:
        """yields batches drained from one subscriber queue until closed.

        blocks up to HEARTBEAT_SECONDS for the first line, then drains whatever
        else is queued so a burst coalesces into one batch.
        """
        try:
            while True:
                try:
                    first = q.get(timeout=self.HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield []
                    continue
                batch = [first]
                while True:
                    try:
                        batch.append(q.get_nowait())
                    except queue.Empty:
                        break
                yield batch
        finally:
            self._unregister(q)


__all__ = ["BOOT_ID", "LogLine", "LogBuffer", "Subscription", "verbosity_token"]
