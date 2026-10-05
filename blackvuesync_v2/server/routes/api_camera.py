"""api camera routes: the dashcam's own settings (config.ini), read-only.

every read goes to the camera with camera.read_timeout_seconds; a successful
read replaces the private snapshot and reports what changed since the
previous one. when the camera is away the snapshot is served instead.
"""

from __future__ import annotations

import dataclasses
import json
import logging

from flask import Blueprint, Response, abort, current_app, request

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.camera_config import (
    CameraRead,
    CameraStore,
    Change,
    diff,
    fetch,
    safe_text,
)
from blackvuesync_v2.server.camera_crypto import (
    UndecodablePasswordError,
    decrypt_password,
)
from blackvuesync_v2.server.camera_schema import build_tabs, field_for, is_secret
from blackvuesync_v2.settings import Settings, SettingsStore

logger = logging.getLogger(__name__)

api_camera_bp = Blueprint("api_camera_bp", __name__, url_prefix="/api/camera")

_MIME_JSON = "application/json"
_CHANGED = "changed"  # stands in for a password in change reports


@dataclasses.dataclass(frozen=True)
class CameraState:
    """the camera's settings as last seen, and whether this read was live."""

    online: bool
    read: CameraRead | None
    changed: tuple[Change, ...]


def camera_store(store: SettingsStore) -> CameraStore:
    """returns the snapshot store next to settings.json."""
    return CameraStore(store.path.parent / "camera")


def _read_timeout(settings: Settings) -> float:
    return min(max(settings.camera.read_timeout_seconds, 0.5), 30.0)


def camera_state(store: SettingsStore) -> CameraState:
    """reads the camera live; falls back to the snapshot when it is away."""
    settings = store.get()
    snapshots = camera_store(store)
    previous = snapshots.load()
    live = fetch(settings.connection.address, _read_timeout(settings))
    if live is None:
        return CameraState(online=False, read=previous, changed=())
    try:
        snapshots.save(live)
    except OSError as error:
        logger.warning("could not save the camera snapshot: %s", error)
    changed = tuple(diff(previous.config, live.config)) if previous else ()
    return CameraState(online=True, read=live, changed=changed)


def change_views(changes: tuple[Change, ...]) -> list[dict[str, str]]:
    """describes changes for display; passwords show only that they changed."""
    views: list[dict[str, str]] = []
    for change in changes:
        hidden = is_secret(change.section, change.key)
        views.append(
            {
                "key": safe_text(f"{change.section}.{change.key}"),
                "label": safe_text(field_for(change.section, change.key).label),
                "from": _CHANGED if hidden else safe_text(change.before or ""),
                "to": _CHANGED if hidden else safe_text(change.after or ""),
            }
        )
    return views


def state_body(state: CameraState) -> dict[str, object]:
    """returns the json body for a camera state; passwords stay masked."""
    if state.read is None:
        return {"available": False, "online": False}
    read = state.read
    return {
        "available": True,
        "online": state.online,
        "read_at": read.read_at.isoformat(),
        "model": read.version.model,
        "firmware": read.version.firmware,
        "tabs": [dataclasses.asdict(tab) for tab in build_tabs(read.config)],
        "changed": change_views(state.changed),
    }


def _json(body: object, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, mimetype=_MIME_JSON)


@api_camera_bp.route("/config", methods=["GET"])
@login_required
def config() -> Response:
    """returns the camera settings by tab, read live when the camera answers."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    return _json(state_body(camera_state(store)))


@api_camera_bp.route("/secret", methods=["GET"])
@login_required
def secret() -> Response:
    """returns one decrypted password from the snapshot (the eye icon)."""
    section, _, key = request.args.get("key", "").partition(".")
    if not is_secret(section, key):
        abort(404)
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    read = camera_store(store).load()
    stored = read.config.value(section, key) if read else None
    if stored is None:
        abort(404)
    try:
        value = decrypt_password(stored)
    except UndecodablePasswordError:
        return _json(
            {"code": "UNDECODABLE_PASSWORD", "error": "this password can't be shown"},
            status=422,
        )
    resp = _json({"key": f"{section}.{key}", "value": value})
    resp.headers["Cache-Control"] = "no-store"
    return resp


__all__ = [
    "CameraState",
    "api_camera_bp",
    "camera_state",
    "camera_store",
    "change_views",
    "state_body",
]
