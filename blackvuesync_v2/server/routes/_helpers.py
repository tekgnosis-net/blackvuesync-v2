"""shared utilities for route handlers."""

from __future__ import annotations

import json
from typing import Any, Optional

from flask import Response, request

_MIME_JSON = "application/json"


def require_dict_body() -> tuple[Optional[dict[str, Any]], Optional[Response]]:
    """parses the request body and validates it is a JSON object.

    returns (payload, None) on success, or (None, error_response) on failure
    so callers can early-return the error. handlers that accept a non-object
    JSON body (array, scalar) would otherwise crash with AttributeError on
    .get() or .items(), surfacing as a 500.
    """
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        body = json.dumps(
            {
                "error": "request body must be a JSON object",
                "code": "INVALID_BODY",
                "details": {},
            }
        )
        return None, Response(body, status=400, mimetype=_MIME_JSON)
    return payload, None


__all__ = ["require_dict_body"]
