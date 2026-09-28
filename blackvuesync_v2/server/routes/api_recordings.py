"""api routes for browsing downloaded recordings."""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Blueprint, Response, current_app, request

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.settings import SettingsStore
from blackvuesync_v2.sync import filename_re

api_recordings_bp = Blueprint(
    "api_recordings_bp", __name__, url_prefix="/api/recordings"
)

_MIME_JSON = "application/json"
_DEFAULT_LIMIT = 5
_MAX_LIMIT = 50


def _compute_recent(destination: Path, limit: int) -> dict[str, object]:
    """returns the N most recently modified BlackVue recordings at destination.

    factored out so /api/recordings/recent and /hx/recent-activity-card share
    the same computation. files that do not match filename_re are ignored.
    uses filename_re.fullmatch for consistency with sync.py and other health
    probes (prevents false positives from .bak or .partial suffixes).
    """
    if not destination.exists():
        return {"recordings": [], "total": 0}

    matches: list[tuple[float, str, str]] = []
    for root, _, files in os.walk(destination):
        for name in files:
            if not filename_re.fullmatch(name):
                continue
            path = os.path.join(root, name)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            matches.append((mtime, name, path))

    matches.sort(key=lambda m: m[0], reverse=True)
    head = matches[:limit]
    return {
        "recordings": [
            {"filename": name, "mtime": mtime, "path": path}
            for mtime, name, path in head
        ],
        "total": len(matches),
    }


@api_recordings_bp.route("/recent", methods=["GET"])
@login_required
def recent() -> Response:
    """returns the N most recently modified BlackVue recordings."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    destination = Path(store.get().system.destination)

    try:
        limit = int(request.args.get("limit", _DEFAULT_LIMIT))
    except (TypeError, ValueError):
        limit = _DEFAULT_LIMIT
    limit = max(1, min(limit, _MAX_LIMIT))

    body = json.dumps(_compute_recent(destination, limit))
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_recordings_bp"]
