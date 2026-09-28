"""htmx sync fragment routes: /hx/sync/status-card, /hx/sync/last-run-card."""

from __future__ import annotations

from flask import Blueprint, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.progress import ProgressPublisher

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


@hx_sync_bp.route("/last-run-card", methods=["GET"])
@login_required
def last_run_card() -> str:
    """renders the last completed sync run card htmx partial."""
    snap = _publisher().snapshot()
    return render_template("_partials/last_run_card.html", snap=snap)


__all__ = ["hx_sync_bp"]
