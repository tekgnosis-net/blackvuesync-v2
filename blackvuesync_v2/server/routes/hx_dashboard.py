"""htmx fragment routes for the dashboard's 4 polled cards."""

from __future__ import annotations

from pathlib import Path

from flask import Blueprint, Response, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.routes.api_dashcam import (
    _compute_dashcam_info,
    _config_preview,
)
from blackvuesync_v2.server.routes.api_health import _compute_dashcam, _compute_storage
from blackvuesync_v2.server.routes.api_recordings import _DEFAULT_LIMIT, _compute_recent
from blackvuesync_v2.settings import SettingsStore

hx_dashboard_bp = Blueprint("hx_dashboard_bp", __name__, url_prefix="/hx")

_MIME_HTML = "text/html"


def _next_human(cron_expression: str, timezone: str) -> str:
    """returns a human-readable description of the next cron tick.

    uses apscheduler's CronTrigger to compute the next fire time; falls back
    to the raw cron expression if computation fails (e.g., invalid cron).
    """
    try:
        # pylint: disable=import-outside-toplevel
        from datetime import datetime
        from datetime import timezone as dt_timezone

        from apscheduler.triggers.cron import CronTrigger

        # pylint: enable=import-outside-toplevel

        trigger = CronTrigger.from_crontab(cron_expression, timezone=timezone)
        now = datetime.now(dt_timezone.utc)
        next_fire = trigger.get_next_fire_time(None, now)
        if next_fire is None:
            return "--"
        delta = next_fire - now
        total_seconds = int(delta.total_seconds())
        if total_seconds < 60:
            return f"in {total_seconds} s"
        if total_seconds < 3600:
            return f"in {total_seconds // 60} min"
        return str(next_fire.strftime("%H:%M %Z"))
    except Exception:  # pylint: disable=broad-exception-caught
        return cron_expression


@hx_dashboard_bp.route("/storage-card", methods=["GET"])
@login_required
def storage_card() -> Response:
    """renders the storage card fragment."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    destination = Path(store.get().system.destination)
    ctx = _compute_storage(destination)
    return Response(
        render_template("_partials/storage_card.html", **ctx),
        mimetype=_MIME_HTML,
    )


@hx_dashboard_bp.route("/dashcam-card", methods=["GET"])
@login_required
def dashcam_card() -> Response:
    """renders the dashcam card fragment."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    address = store.get().connection.address
    ctx = _compute_dashcam(address)
    return Response(
        render_template("_partials/dashcam_card.html", **ctx),
        mimetype=_MIME_HTML,
    )


@hx_dashboard_bp.route("/next-scheduled-card", methods=["GET"])
@login_required
def next_scheduled_card() -> Response:
    """renders the next-scheduled card fragment."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    schedule = store.get().schedule
    next_human = _next_human(schedule.cron_expression, schedule.timezone)
    return Response(
        render_template(
            "_partials/next_scheduled_card.html",
            paused=schedule.paused,
            cron_expression=schedule.cron_expression,
            timezone=schedule.timezone,
            next_human=next_human,
        ),
        mimetype=_MIME_HTML,
    )


@hx_dashboard_bp.route("/recent-activity-card", methods=["GET"])
@login_required
def recent_activity_card() -> Response:
    """renders the recent-activity card fragment."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    destination = Path(store.get().system.destination)
    ctx = _compute_recent(destination, _DEFAULT_LIMIT)
    return Response(
        render_template("_partials/recent_activity_card.html", **ctx),
        mimetype=_MIME_HTML,
    )


@hx_dashboard_bp.route("/dashcam-info-card", methods=["GET"])
@login_required
def dashcam_info_card() -> Response:
    """renders the dashcam-info card fragment (firmware + config preview)."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    address = store.get().connection.address
    ctx = _compute_dashcam_info(address)
    # ctx["config"] is typed object on the dict[str, object] return; it is a
    # config dict only when available, which the short-circuit guarantees.
    entries = (
        _config_preview(ctx["config"])  # type: ignore[arg-type]
        if ctx.get("available")
        else []
    )
    return Response(
        render_template("_partials/dashcam_info_card.html", entries=entries, **ctx),
        mimetype=_MIME_HTML,
    )


__all__ = ["hx_dashboard_bp"]
