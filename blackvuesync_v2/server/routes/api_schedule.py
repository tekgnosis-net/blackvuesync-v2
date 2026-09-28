"""api schedule routes: pause and resume the scheduler."""

from __future__ import annotations

import dataclasses
import json

from flask import Blueprint, Response, current_app

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.settings import SettingsStore

api_schedule_bp = Blueprint("api_schedule_bp", __name__, url_prefix="/api/schedule")

_MIME_JSON = "application/json"


def _set_paused(paused: bool) -> Response:
    """sets schedule.paused to the given value and returns the new state."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    store.update(
        lambda s: dataclasses.replace(
            s, schedule=dataclasses.replace(s.schedule, paused=paused)
        )
    )
    body = json.dumps({"paused": paused})
    return Response(body, status=200, mimetype=_MIME_JSON)


@api_schedule_bp.route("/pause", methods=["POST"])
@login_required
def pause() -> Response:
    """pauses scheduled syncs. manual POST /api/sync/now still works."""
    return _set_paused(True)


@api_schedule_bp.route("/resume", methods=["POST"])
@login_required
def resume() -> Response:
    """resumes scheduled syncs."""
    return _set_paused(False)


__all__ = ["api_schedule_bp"]
