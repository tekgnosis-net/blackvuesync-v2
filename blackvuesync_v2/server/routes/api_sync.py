"""api sync routes: /api/sync/progress, /api/sync/progress/stream, /api/sync/now, /api/sync/last."""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator

from flask import Blueprint, Response, current_app

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.progress import (
    FileProgress,
    ProgressPublisher,
    SyncProgress,
)
from blackvuesync_v2.server.sse import sse_response
from blackvuesync_v2.server.sync_runner import trigger_sync

# manual smoke test:
#   curl -N -H "Cookie: bvs_session=..." http://localhost:8080/api/sync/progress/stream
# then in another terminal trigger a sync via /api/sync/now and watch the stream.

api_sync_bp = Blueprint("api_sync_bp", __name__, url_prefix="/api/sync")

# all sync api responses serialize SyncProgress / error envelopes as json.
_MIME_JSON = "application/json"


def _publisher() -> ProgressPublisher:
    """returns the app-level progress publisher."""
    pub: ProgressPublisher = current_app.progress_publisher  # type: ignore[attr-defined]
    return pub


def _file_progress_to_dict(fp: FileProgress) -> dict[str, object]:
    """converts a FileProgress snapshot to a dict, including computed properties."""
    d: dict[str, object] = dataclasses.asdict(fp)
    d["percent"] = fp.percent
    d["elapsed_seconds"] = fp.elapsed_seconds
    return d


def _snap_to_dict(snap: SyncProgress) -> dict[str, object]:
    """converts a SyncProgress snapshot to a JSON-serializable dict.

    includes computed properties (percent, elapsed_seconds) so the ui
    does not need to recalculate them from raw fields.
    """
    d: dict[str, object] = dataclasses.asdict(snap)
    d["percent"] = snap.percent
    # replaces the nested current_file dict with one that also has computed props
    if snap.current_file is not None:
        d["current_file"] = _file_progress_to_dict(snap.current_file)
    return d


@api_sync_bp.route("/progress", methods=["GET"])
@login_required
def progress_snapshot() -> Response:
    """returns the current sync progress as JSON."""
    snap = _publisher().snapshot()
    return Response(
        json.dumps(_snap_to_dict(snap), default=str),
        status=200,
        mimetype=_MIME_JSON,
    )


@api_sync_bp.route("/progress/stream", methods=["GET"])
@login_required
def progress_stream() -> Response:
    """streams sync progress as Server-Sent Events.

    emits event: progress\\ndata: <json>\\n\\n on each state change
    (throttled to PUBLISH_HZ). emits a keepalive comment (: keepalive)
    every 30 seconds when no events arrive.
    """
    pub = _publisher()

    def _sse_events() -> Iterator[bytes]:
        last_event_monotonic: float = -1.0
        for snap in pub.subscribe():
            if snap.last_event_monotonic == last_event_monotonic:
                # same snapshot repeated -- no new events; emit keepalive comment
                yield b": keepalive\n\n"
            else:
                last_event_monotonic = snap.last_event_monotonic
                snap_dict = _snap_to_dict(snap)
                payload = json.dumps(snap_dict, default=str)
                yield f"event: progress\ndata: {payload}\n\n".encode()

    return sse_response(_sse_events())


@api_sync_bp.route("/now", methods=["POST"])
@login_required
def trigger_now() -> Response:
    """triggers an on-demand sync; returns 202 or 409 if already running.

    flask-wtf csrfprotect validates the X-CSRFToken header globally for all
    post requests; a missing or invalid token causes a 400 before this handler
    runs.
    """
    pub = _publisher()
    settings_store = current_app.settings_store  # type: ignore[attr-defined]
    settings = settings_store.get()

    stats_store = getattr(current_app, "stats_store", None)
    result = trigger_sync(settings, pub, stats_store, settings_store.path.parent)

    if result["status"] == "already_running":
        body = json.dumps(
            {
                "error": "sync already running",
                "code": "SYNC_ALREADY_RUNNING",
                "details": {"current_job_id": result["job_id"]},
            }
        )
        return Response(body, status=409, mimetype=_MIME_JSON)

    body = json.dumps({"job_id": result["job_id"]})
    return Response(body, status=202, mimetype=_MIME_JSON)


@api_sync_bp.route("/last", methods=["GET"])
@login_required
def last_sync() -> Response:
    """returns the most recently completed sync snapshot; 204 if none."""
    snap = _publisher().snapshot()
    if snap.state == "idle":
        return Response(status=204)
    body = json.dumps(_snap_to_dict(snap), default=str)
    return Response(body, status=200, mimetype=_MIME_JSON)


@api_sync_bp.route("/stop", methods=["POST"])
@login_required
def stop_sync() -> Response:
    """requests cooperative stop of the active sync.

    returns 202 + {job_id, stopping: true} if a sync was running;
    404 + {code: 'SYNC_NOT_RUNNING'} if no sync is active. the actual
    stop happens between download chunks; the next snapshot will report
    state='failed' with reason="stopped by user" once the chunk loop
    raises UserWarning.

    benign TOCTOU race: if the sync finishes between the snapshot read
    and request_stop(), the flag is set on no-op and cleared by the next
    trigger_sync() call (sync_runner.py clears it before each new run).
    """
    # pylint: disable=import-outside-toplevel
    from blackvuesync_v2.sync import request_stop

    # pylint: enable=import-outside-toplevel

    snap = _publisher().snapshot()
    if snap.state != "running":
        body = json.dumps(
            {
                "error": "no sync is running",
                "code": "SYNC_NOT_RUNNING",
                "details": {},
            }
        )
        return Response(body, status=404, mimetype=_MIME_JSON)

    request_stop()
    body = json.dumps({"job_id": snap.job_id, "stopping": True})
    return Response(body, status=202, mimetype=_MIME_JSON)


__all__ = ["api_sync_bp"]
