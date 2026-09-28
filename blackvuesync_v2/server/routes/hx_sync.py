"""htmx sync fragment routes: /hx/sync/status-card, /hx/sync/last-run-card."""

from __future__ import annotations

import datetime
import time
from typing import Any

from flask import Blueprint, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.stats_store import RunRow

hx_sync_bp = Blueprint("hx_sync_bp", __name__, url_prefix="/hx/sync")


def _publisher() -> ProgressPublisher:
    """returns the app-level progress publisher."""
    pub: ProgressPublisher = current_app.progress_publisher  # type: ignore[attr-defined]
    return pub


@hx_sync_bp.route("/status-card", methods=["GET"])
@login_required
def status_card() -> str:
    """renders the sync status card htmx partial."""
    snap = _publisher().snapshot()
    return render_template("_partials/sync_status_card.html", snap=snap)


def _ago(seconds: float) -> str:
    """renders an elapsed time as "just now", "12 min ago", "3 h ago", "2 d ago"."""
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} h ago"
    return f"{int(seconds // 86400)} d ago"


# run-failure reasons that mean the dashcam could not be reached (car away).
_UNREACHABLE_REASONS = frozenset({"network", "timeout"})


def _run_view(row: RunRow, now: float) -> dict[str, Any]:
    """shapes a stored run for the card; times use the process timezone (TZ)."""
    started = datetime.datetime.fromtimestamp(row.ts_seconds).astimezone()
    # stored runs list every reason with its count, most of them zero
    reasons = {reason for reason, count in row.failures.items() if count}
    unreachable = not row.success and bool(reasons) and reasons <= _UNREACHABLE_REASONS
    return {
        "unreachable": unreachable,
        "ago": _ago(max(0.0, now - row.ts_seconds)),
        "when": started.strftime("%a %d %b %H:%M %Z").strip(),
        "success": bool(row.success),
        "files": row.files,
        "bytes": row.bytes,
        "failures": sum(row.failures.values()),
        "dry_run": bool(row.dry_run),
    }


def last_run_context() -> dict[str, Any]:
    """returns the last-sync card's template context.

    while a job is live (running, or its 10 s post-completion window) the
    card shows the publisher's snapshot; otherwise it shows the most recent
    run recorded in the stats store, which survives restarts.
    """
    snap = _publisher().snapshot()
    last = None
    if snap.state == "idle":
        stats_store = getattr(current_app, "stats_store", None)
        row = stats_store.latest() if stats_store is not None else None
        last = _run_view(row, time.time()) if row is not None else None
    return {"snap": snap, "last": last}


@hx_sync_bp.route("/last-run-card", methods=["GET"])
@login_required
def last_run_card() -> str:
    """renders the last completed sync run card htmx partial."""
    return render_template("_partials/last_run_card.html", **last_run_context())


__all__ = ["hx_sync_bp", "last_run_context"]
