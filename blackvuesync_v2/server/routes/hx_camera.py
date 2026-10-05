"""htmx fragment for the camera settings panes (read-only in this release)."""

from __future__ import annotations

from flask import Blueprint, Response, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.camera_schema import TABS, build_tabs
from blackvuesync_v2.server.routes.api_camera import camera_state, change_views
from blackvuesync_v2.settings import SettingsStore

hx_camera_bp = Blueprint("hx_camera_bp", __name__, url_prefix="/hx/camera")


@hx_camera_bp.route("/panes", methods=["GET"])
@login_required
def panes() -> Response:
    """renders one pane per camera tab from a live read, else the snapshot."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    state = camera_state(store)
    read = state.read
    html = render_template(
        "_partials/camera_panes.html",
        tabs=build_tabs(read.config) if read else [],
        placeholder_tabs=TABS,
        online=state.online,
        description=read.version.description if read else "",
        read_at=read.read_at.astimezone().strftime("%Y-%m-%d %H:%M") if read else "",
        changed=change_views(state.changed),
    )
    return Response(html, mimetype="text/html")
