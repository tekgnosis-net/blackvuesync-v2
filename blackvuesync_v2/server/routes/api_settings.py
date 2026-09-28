"""api settings routes: GET /api/settings, PATCH /api/settings/<section>."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

from flask import Blueprint, Response, current_app

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.routes._helpers import require_dict_body
from blackvuesync_v2.settings import (
    _REDACTED_FIELDS,
    _SECTION_FIELDS,
    _TUPLE_FIELDS,
    Settings,
    SettingsStore,
    ValidationError,
)

api_settings_bp = Blueprint("api_settings_bp", __name__, url_prefix="/api/settings")

_MIME_JSON = "application/json"

# _REDACTED_FIELDS is defined in settings.py and imported above; the literal
# "***" sentinel is returned for those fields and stripped on inbound patches,
# and any other inbound value for them is rejected.

_REDACTED_SENTINEL = "***"


def _section_to_dict(name: str, section: Any) -> dict[str, Any]:
    """converts a section dataclass to a dict, redacting secret fields and
    adding the _tier annotation."""
    d: dict[str, Any] = dataclasses.asdict(section)
    redact = _REDACTED_FIELDS.get(name, set())
    # unconditional redaction: emit '***' even when the field is empty, so
    # the first-run state (password_hash="") does not leak the auth-not-yet-set
    # condition through the api.
    for field_name in redact:
        if field_name in d:
            d[field_name] = _REDACTED_SENTINEL
    d["_tier"] = section.__class__.TIER
    return d


def _settings_to_dict(s: Settings) -> dict[str, Any]:
    """converts the full Settings to a redacted dict with per-section tier."""
    out: dict[str, Any] = {"version": s.version}
    for name in _SECTION_FIELDS:
        out[name] = _section_to_dict(name, getattr(s, name))
    return out


@api_settings_bp.route("", methods=["GET"])
@login_required
def get_settings() -> Response:
    """returns the full settings as JSON; secrets redacted to '***'."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    return Response(
        json.dumps(_settings_to_dict(store.get())),
        status=200,
        mimetype=_MIME_JSON,
    )


def _strip_redacted(
    payload: dict[str, Any], section_name: str
) -> tuple[dict[str, Any], list[str]]:
    """removes redacted fields from payload; returns it with rejected field names.

    a client that re-submits the redacted snapshot from GET must not
    overwrite the real secret with '***', so the sentinel is dropped and
    callers treat absent keys as 'leave unchanged'. any other value for a
    redacted field is rejected: secrets change only through
    POST /api/auth/password and DELETE /api/auth/sessions.
    """
    redact = _REDACTED_FIELDS.get(section_name, set())
    rejected = sorted(
        k for k in payload if k in redact and payload[k] != _REDACTED_SENTINEL
    )
    return {k: v for k, v in payload.items() if k not in redact}, rejected


def _invalid_response(field_errors: list[dict[str, str]]) -> Response:
    """returns the 422 SETTINGS_INVALID envelope for field_errors."""
    body = json.dumps(
        {
            "error": "settings validation failed",
            "code": "SETTINGS_INVALID",
            "details": {"field_errors": field_errors},
        }
    )
    return Response(body, status=422, mimetype=_MIME_JSON)


def _coerce_tuples(payload: dict[str, Any], section_name: str) -> dict[str, Any]:
    """converts list values to tuple for fields declared as tuple in the
    section dataclass.

    JSON has no tuple, so clients send a list for fields like sync.include.
    dataclasses.replace does not enforce type annotations at runtime, so
    without this step the in-memory dataclass would hold a list and violate
    its own type contract (the on-disk JSON round-trip would still restore
    a tuple, masking the bug until the next consumer relies on tuple
    semantics).
    """
    tuple_fields = _TUPLE_FIELDS.get(section_name, set())
    return {
        k: tuple(v) if k in tuple_fields and isinstance(v, list) else v
        for k, v in payload.items()
    }


@api_settings_bp.route("/<string:section_name>", methods=["PATCH"])
@login_required
def patch_section(section_name: str) -> Response:
    """updates a section partially; validates; returns the tier on success."""
    if section_name not in _SECTION_FIELDS:
        body = json.dumps(
            {
                "error": f"unknown settings section: {section_name!r}",
                "code": "SECTION_NOT_FOUND",
                "details": {"section": section_name},
            }
        )
        return Response(body, status=404, mimetype=_MIME_JSON)

    payload, err = require_dict_body()
    if err is not None:
        return err
    assert payload is not None  # type narrowing for mypy
    payload, rejected = _strip_redacted(payload, section_name)
    if rejected:
        return _invalid_response(
            [
                {
                    "path": f"{section_name}.{name}",
                    "message": f"{section_name}.{name} is read-only here; use "
                    "POST /api/auth/password or DELETE /api/auth/sessions",
                }
                for name in rejected
            ]
        )
    payload = _coerce_tuples(payload, section_name)

    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    section_cls = _SECTION_FIELDS[section_name]

    current = store.get()
    current_section = getattr(current, section_name)
    try:
        new_section = dataclasses.replace(current_section, **payload)
    except TypeError as e:
        # unknown field name in payload; treat as a validation failure.
        return _invalid_response([{"path": f"{section_name}.?", "message": str(e)}])

    # type checks run first inside validate(), so mistyped values never reach
    # the value checks or the store.
    errors = new_section.validate()
    if not errors:
        # re-applies the payload to the section as of the update, so a
        # concurrent change to another field of the section is not reverted.
        try:
            store.update(
                lambda s: dataclasses.replace(
                    s,
                    **{
                        section_name: dataclasses.replace(
                            getattr(s, section_name), **payload
                        )
                    },
                )
            )
        except ValidationError as e:
            errors = e.errors
    if errors:
        return _invalid_response(
            [{"path": section_name, "message": msg} for msg in errors]
        )

    body = json.dumps(
        {
            "section": section_name,
            "tier": section_cls.TIER,  # type: ignore[attr-defined]
            "applied": True,
        }
    )
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_settings_bp"]
