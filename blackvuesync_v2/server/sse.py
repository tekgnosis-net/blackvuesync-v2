"""shared Server-Sent Events response helper."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from typing import Optional

from flask import Response, stream_with_context

# caps concurrent SSE streams across all endpoints; each open stream pins one
# waitress worker thread, so an unbounded count would starve normal requests.
MAX_STREAMS = 16


class _StreamLimiter:
    """thread-safe counter of open SSE streams with a fixed ceiling."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = 0

    def acquire(self) -> Optional[Callable[[], None]]:
        """reserves a slot; returns an idempotent release callable, or None
        when MAX_STREAMS are already open."""
        with self._lock:
            if self.active >= MAX_STREAMS:
                return None
            self.active += 1
        released = threading.Event()

        def release() -> None:
            """returns the slot once, however many times it is called."""
            with self._lock:
                if not released.is_set():
                    released.set()
                    self.active -= 1

        return release

    def reset(self) -> None:
        """forgets all open streams (tests only)."""
        with self._lock:
            self.active = 0


_LIMITER = _StreamLimiter()


def active_streams() -> int:
    """returns the number of currently open SSE streams."""
    return _LIMITER.active


def _guarded(events: Iterator[bytes], release: Callable[[], None]) -> Iterator[bytes]:
    """yields from events and releases the stream slot when the stream ends."""
    try:
        yield from events
    finally:
        release()


def sse_response(events: Iterator[bytes]) -> Response:
    """wraps an SSE byte-frame generator in a streaming text/event-stream
    response with the headers that defeat proxy buffering (Cache-Control,
    X-Accel-Buffering).

    never sets Transfer-Encoding: it is a hop-by-hop header that PEP 3333
    forbids from wsgi apps (waitress rejects it); the server chunks on its own.
    returns 503 TOO_MANY_STREAMS once MAX_STREAMS streams are open.
    """
    release = _LIMITER.acquire()
    if release is None:
        # an unstarted generator holds no resources; closes it anyway so an
        # eager iterator can clean up.
        close = getattr(events, "close", None)
        if close is not None:
            close()
        body = json.dumps(
            {
                "error": "too many open streams; try again later",
                "code": "TOO_MANY_STREAMS",
                "details": {"limit": MAX_STREAMS},
            }
        )
        resp = Response(body, status=503, mimetype="application/json")
        resp.headers["Retry-After"] = "5"
        return resp

    resp = Response(
        stream_with_context(_guarded(events, release)), mimetype="text/event-stream"
    )
    # the server always closes the response, even when the body generator
    # never started (and so never reached its finally).
    resp.call_on_close(release)
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
