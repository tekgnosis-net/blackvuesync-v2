"""ui routes: dashboard, settings, and placeholder pages."""

from __future__ import annotations

import dataclasses
from pathlib import Path

from flask import Blueprint, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.log_buffer import verbosity_token
from blackvuesync_v2.server.routes.api_health import _compute_storage
from blackvuesync_v2.server.routes.api_recordings import _DEFAULT_LIMIT, _compute_recent
from blackvuesync_v2.server.routes.api_settings import _settings_to_dict
from blackvuesync_v2.server.routes.hx_dashboard import _next_human
from blackvuesync_v2.server.routes.hx_sync import last_run_context
from blackvuesync_v2.server.settings_form import build_sections

bp = Blueprint("ui_bp", __name__)


@bp.route("/", methods=["GET"])
@login_required
def dashboard() -> str:
    """renders the real dashboard.

    the four local cards (last sync, next scheduled, storage, recent activity)
    are pre-rendered populated -- each via its own render_template call so the
    shared `available` key cannot collide across cards -- and injected into the
    page with | safe. the page is therefore useful without javascript and
    paints instantly. the two network cards (dashcam reachability, dashcam
    info) are included as shells and fetched by htmx after load, so a slow or
    offline dashcam never blocks the page render.
    """
    store = current_app.settings_store  # type: ignore[attr-defined]
    current = store.get()
    destination = Path(current.system.destination)
    publisher = current_app.progress_publisher  # type: ignore[attr-defined]
    schedule = current.schedule
    snap = publisher.snapshot()
    sync_state = "running" if snap.state == "running" else "idle"

    last_run_html = render_template(
        "_partials/last_run_card.html", **last_run_context()
    )
    next_scheduled_html = render_template(
        "_partials/next_scheduled_card.html",
        paused=schedule.paused,
        cron_expression=schedule.cron_expression,
        timezone=schedule.timezone,
        next_human=_next_human(schedule.cron_expression, schedule.timezone),
    )
    storage_html = render_template(
        "_partials/storage_card.html", **_compute_storage(destination)
    )
    recent_activity_html = render_template(
        "_partials/recent_activity_card.html",
        **_compute_recent(destination, _DEFAULT_LIMIT),
    )

    return render_template(
        "dashboard.html",
        page="dashboard",
        auth_mode=current.auth.mode,
        last_run_html=last_run_html,
        next_scheduled_html=next_scheduled_html,
        storage_html=storage_html,
        recent_activity_html=recent_activity_html,
        sync_state=sync_state,
        paused=schedule.paused,
    )


@bp.route("/settings", methods=["GET"])
@login_required
def settings() -> str:
    """renders the settings page (sidebar sections + per-section forms)."""
    store = current_app.settings_store  # type: ignore[attr-defined]
    settings_dict = _settings_to_dict(store.get())  # redacted, per-section _tier
    return render_template(
        "settings.html",
        page="settings",
        sections=build_sections(settings_dict),
    )


@bp.route("/logs", methods=["GET"])
@login_required
def logs() -> str:
    """renders the live log viewer, server-painting the current buffer snapshot."""
    buf = current_app.log_buffer  # type: ignore[attr-defined]
    store = current_app.settings_store  # type: ignore[attr-defined]
    logging_settings = store.get().logging
    return render_template(
        "logs.html",
        page="logs",
        lines=[dataclasses.asdict(ln) for ln in buf.snapshot()],
        log_file_path=current_app.log_file_path or "",  # type: ignore[attr-defined]
        capacity=buf.capacity,
        verbosity=verbosity_token(logging_settings),
    )


@bp.route("/stats", methods=["GET"])
@login_required
def stats() -> str:
    """renders the statistics page; charts hydrate client-side from the api."""
    store = current_app.stats_store  # type: ignore[attr-defined]
    recent = list(reversed(store.query()))[:20]  # newest 20 for the no-js fallback
    return render_template(
        "stats.html",
        page="stats",
        recent=recent,
    )


@bp.route("/viewer", methods=["GET"])
@login_required
def viewer() -> str:
    """renders the dashcam viewer; recordings + telemetry hydrate client-side."""
    viewer_settings = current_app.settings_store.get().viewer  # type: ignore[attr-defined]
    return render_template(
        "viewer.html",
        page="viewer",
        journey_mode=viewer_settings.journey_mode,
        speed_unit=viewer_settings.speed_unit,
    )
