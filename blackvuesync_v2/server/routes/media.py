"""path-safe serving of recording files (.mp4/.thm) from the destination."""

from __future__ import annotations

import os
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, send_from_directory
from werkzeug.security import safe_join

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.settings import SettingsStore

media_bp = Blueprint("media_bp", __name__, url_prefix="/media")

_ALLOWED_SUFFIXES = (".mp4", ".thm")


@media_bp.route("/<path:relpath>", methods=["GET"])
@login_required
def media(relpath: str) -> Response:
    """serves a .mp4/.thm file under the destination; 404 on anything unsafe.

    layered defenses: extension allow-list, werkzeug safe_join (traversal),
    and a realpath-containment check (symlink escape). send_from_directory
    with conditional=True handles HTTP Range so the browser can stream/seek.
    """
    if not relpath.lower().endswith(_ALLOWED_SUFFIXES):
        abort(404)

    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    destination = Path(store.get().system.destination).resolve()

    joined = safe_join(str(destination), relpath)
    if joined is None:
        abort(404)
    resolved = Path(joined).resolve()
    if destination not in resolved.parents or not resolved.is_file():
        abort(404)

    mimetype = "image/jpeg" if resolved.suffix.lower() == ".thm" else None
    return send_from_directory(
        destination,
        os.path.relpath(resolved, destination),
        mimetype=mimetype,
        conditional=True,
    )


__all__ = ["media_bp"]
