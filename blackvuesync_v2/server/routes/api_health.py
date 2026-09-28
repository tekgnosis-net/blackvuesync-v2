"""api health routes: storage and dashcam health probes."""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Blueprint, Response, current_app

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.settings import SettingsStore
from blackvuesync_v2.sync import filename_re

api_health_bp = Blueprint("api_health_bp", __name__, url_prefix="/api/health")

_MIME_JSON = "application/json"


def _count_recordings(destination: Path) -> int:
    """counts files in destination whose name matches the BlackVue regex."""
    count = 0
    for _, _, files in os.walk(destination):
        for name in files:
            if filename_re.fullmatch(name):
                count += 1
    return count


def _compute_storage(destination: Path) -> dict[str, object]:
    """computes storage stats for destination; returns a JSON-serializable dict.

    factored out so /api/health/storage and /hx/storage-card both share the
    same computation. uses shutil.disk_usage for consistency with sync.py's
    max_used_disk_percent threshold check (root-reserved blocks count as
    free, matching what the sync engine sees).
    when destination does not exist, returns
    {available: False, reason: ...} matching the structural-case contract.
    """
    if not destination.exists():
        return {"available": False, "reason": "destination not configured"}

    usage = shutil.disk_usage(destination)
    used_percent = round((usage.used / usage.total) * 100, 1) if usage.total else 0.0
    return {
        "available": True,
        "destination": str(destination),
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": usage.used,
        "used_percent": used_percent,
        "recording_count": _count_recordings(destination),
    }


@api_health_bp.route("/storage", methods=["GET"])
@login_required
def storage() -> Response:
    """returns storage usage at the destination directory."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    destination = Path(store.get().system.destination)
    body = json.dumps(_compute_storage(destination))
    return Response(body, status=200, mimetype=_MIME_JSON)


def _compute_dashcam(address: str, timeout: float = 2.0) -> dict[str, object]:
    """HEAD-probes http://<address>/blackvue_vod.cgi; returns reachability.

    factored out so /api/health/dashcam and /hx/dashcam-card share the same
    computation. blackvue dashcams expose http only (no https firmware).

    uses a fixed 2 s timeout rather than settings.connection.timeout_seconds:
    a dashboard HEAD probe should be quick (the card is polled every 5 s)
    and a slower timeout would block the page rendering.
    """
    if not address:
        return {"reachable": False, "reason": "no address configured"}

    # BlackVue dashcams expose HTTP only (no https firmware).
    url = f"http://{address}/blackvue_vod.cgi"
    req = urllib.request.Request(url, method="HEAD")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            elapsed_ms = round((time.monotonic() - start) * 1000, 1)
            return {
                "reachable": True,
                "address": address,
                "latency_ms": elapsed_ms,
            }
    except socket.timeout:
        return {"reachable": False, "address": address, "reason": "timeout"}
    except urllib.error.URLError as e:
        # urlopen wraps connect timeouts as URLError(reason=TimeoutError(...));
        # mirrors sync.py's classification so the ui sees a consistent reason.
        if isinstance(e.reason, (TimeoutError, socket.timeout)):
            return {"reachable": False, "address": address, "reason": "timeout"}
        return {"reachable": False, "address": address, "reason": str(e.reason)}
    except OSError as e:
        return {
            "reachable": False,
            "address": address,
            "reason": type(e).__name__.lower(),
        }


@api_health_bp.route("/dashcam", methods=["GET"])
@login_required
def dashcam() -> Response:
    """returns dashcam reachability via HEAD probe."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    address = store.get().connection.address
    body = json.dumps(_compute_dashcam(address))
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_health_bp"]
