"""api auth routes: GET /api/auth/me, POST /api/auth/password, DELETE /api/auth/sessions."""

from __future__ import annotations

import dataclasses
import json
import secrets

from flask import Blueprint, Response, current_app, g, request, session

from blackvuesync_v2.server.auth import (
    MIN_PASSWORD_LENGTH,
    SESSION_VERSION_KEY,
    clear_login_failures,
    hash_password,
    is_login_locked_out,
    login_required,
    record_login_failure,
    session_version,
    verify_password,
)
from blackvuesync_v2.server.routes._helpers import require_dict_body
from blackvuesync_v2.settings import SettingsStore

api_auth_bp = Blueprint("api_auth_bp", __name__, url_prefix="/api/auth")

_MIME_JSON = "application/json"


@api_auth_bp.route("/me", methods=["GET"])
@login_required
def me() -> Response:
    """returns the current authenticated user and auth mode."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    mode = store.get().auth.mode
    body = json.dumps({"username": g.current_user, "mode": mode})
    return Response(body, status=200, mimetype=_MIME_JSON)


@api_auth_bp.route("/password", methods=["POST"])
@login_required
def change_password() -> Response:
    """changes the current user's password; requires the current password."""
    ip = request.remote_addr or "unknown"
    if is_login_locked_out(ip):
        body = json.dumps(
            {
                "error": "too many failures; try again later",
                "code": "RATE_LIMITED",
                "details": {},
            }
        )
        return Response(body, status=429, mimetype=_MIME_JSON)

    payload, err = require_dict_body()
    if err is not None:
        return err
    assert payload is not None  # type narrowing for mypy
    current = payload.get("current_password", "")
    new = payload.get("new_password", "")
    type_errors = [
        {"path": name, "message": "must be a string"}
        for name, value in (("current_password", current), ("new_password", new))
        if not isinstance(value, str)
    ]
    if type_errors:
        body = json.dumps(
            {
                "error": "invalid request body",
                "code": "VALIDATION_ERROR",
                "details": {"field_errors": type_errors},
            }
        )
        return Response(body, status=422, mimetype=_MIME_JSON)

    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    stored_hash = store.get().auth.password_hash

    if not verify_password(stored_hash, current):
        record_login_failure(ip)
        body = json.dumps(
            {
                "error": "current password is incorrect",
                "code": "INVALID_CURRENT_PASSWORD",
                "details": {},
            }
        )
        return Response(body, status=401, mimetype=_MIME_JSON)

    if len(new) < MIN_PASSWORD_LENGTH:
        body = json.dumps(
            {
                "error": "new password too short",
                "code": "WEAK_PASSWORD",
                "details": {
                    "field_errors": [
                        {
                            "path": "new_password",
                            "message": f"must be at least {MIN_PASSWORD_LENGTH} characters",
                        }
                    ]
                },
            }
        )
        return Response(body, status=422, mimetype=_MIME_JSON)

    new_hash = hash_password(new)
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=new_hash)
        )
    )
    clear_login_failures(ip)
    # the new hash revokes every other session; re-issues this one so the
    # caller stays signed in.
    if "user" in session:
        session[SESSION_VERSION_KEY] = session_version(new_hash)

    body = json.dumps({"applied": True})
    return Response(body, status=200, mimetype=_MIME_JSON)


@api_auth_bp.route("/sessions", methods=["DELETE"])
@login_required
def rotate_sessions() -> Response:
    """rotates the session secret, signing every session out immediately.

    create_app() listens for the change and swaps app.secret_key, which Flask
    reads per request, so no restart is needed.
    """
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    new_secret = secrets.token_hex(32)
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, session_secret=new_secret)
        )
    )
    # the caller's cookie would otherwise be re-signed with the new key on
    # this response; clearing it signs the caller out as well.
    session.clear()
    body = json.dumps({"rotated": True, "restart_required": False})
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_auth_bp"]
