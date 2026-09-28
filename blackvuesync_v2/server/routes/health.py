"""health check routes: /healthz (liveness) and /readyz (readiness)."""

from __future__ import annotations

from flask import Blueprint, current_app, jsonify
from werkzeug.wrappers import Response

bp = Blueprint("health_bp", __name__)


@bp.route("/healthz", methods=["GET"])
def healthz() -> Response:
    """liveness probe; always returns 200 when the process is running."""
    return jsonify(status="ok")


@bp.route("/readyz", methods=["GET"])
def readyz() -> tuple[Response, int]:
    """readiness probe; returns 200 when the settings store is loaded."""
    store_ok = current_app.settings_store is not None  # type: ignore[attr-defined]
    status = "ready" if store_ok else "starting"
    code = 200 if store_ok else 503
    return jsonify(status=status, settings_loaded=store_ok), code
