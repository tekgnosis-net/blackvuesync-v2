"""api stats route: GET /api/stats/series (summary + series + disk forecast)."""

from __future__ import annotations

import json
import re
import time

from flask import Blueprint, Response, current_app, request

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.forecast import compute_forecast
from blackvuesync_v2.server.stats_store import RunRow, StatsStore

api_stats_bp = Blueprint("api_stats_bp", __name__, url_prefix="/api/stats")

_MIME_JSON = "application/json"
_SECONDS_PER_DAY = 86400.0

# range token -> lookback window in seconds (None = all history)
_RANGES: dict[str, float | None] = {
    "24h": 1 * _SECONDS_PER_DAY,
    "7d": 7 * _SECONDS_PER_DAY,
    "30d": 30 * _SECONDS_PER_DAY,
    "all": None,
}

_DURATION_RE = re.compile(r"^(\d+)([shdw]?)$")
_DURATION_DAYS = {"s": 1 / _SECONDS_PER_DAY, "h": 1 / 24.0, "d": 1.0, "w": 7.0, "": 1.0}


def _store() -> StatsStore:
    """returns the app-level stats store."""
    store: StatsStore = current_app.stats_store  # type: ignore[attr-defined]
    return store


def _keep_days(keep: str) -> float | None:
    """parses a retention.keep duration (e.g. '2w') to days; None if unparseable."""
    match = _DURATION_RE.match(keep or "")
    if not match:
        return None
    return int(match.group(1)) * _DURATION_DAYS[match.group(2)]


def _point(row: RunRow) -> dict[str, object]:
    """serializes one run row for the series."""
    return {
        "ts": row.ts_seconds,
        "bytes": row.bytes,
        "files": row.files,
        "duration": row.duration_seconds,
        "disk": row.disk_used_ratio,
        "success": row.success,
        "failures": row.failures,
        "dry_run": row.dry_run,
    }


def _summary(rows: list[RunRow]) -> dict[str, object]:
    """computes the summary tiles over the rows in range."""
    runs = len(rows)
    total_bytes = sum(r.bytes for r in rows if not r.dry_run)
    avg_duration = (sum(r.duration_seconds for r in rows) / runs) if runs else 0.0
    successes = sum(1 for r in rows if r.success)
    success_rate = (successes / runs) if runs else 0.0
    return {
        "runs": runs,
        "bytes": total_bytes,
        "avg_duration_seconds": avg_duration,
        "success_rate": success_rate,
    }


def _keep_steady_state(rows: list[RunRow], keep_days: float | None) -> float | None:
    """estimates the retention plateau as the mean disk ratio over the last
    keep_days, when that much history exists; otherwise None."""
    if keep_days is None or not rows:
        return None
    cutoff = rows[-1].ts_seconds - keep_days * _SECONDS_PER_DAY
    if rows[0].ts_seconds > cutoff:
        return None  # not enough history to cover a full retention window
    recent = [
        r.disk_used_ratio
        for r in rows
        if r.ts_seconds >= cutoff and r.disk_used_ratio is not None
    ]
    if not recent:
        return None
    return sum(recent) / len(recent)


@api_stats_bp.route("/series", methods=["GET"])
@login_required
def series() -> Response:
    """returns {range, summary, series, forecast} for the requested window."""
    range_token = request.args.get("range", "7d")
    if range_token not in _RANGES:
        body = json.dumps(
            {
                "error": "unknown range",
                "code": "BAD_RANGE",
                "details": {"range": range_token},
            }
        )
        return Response(body, status=400, mimetype=_MIME_JSON)

    window = _RANGES[range_token]
    since = (time.time() - window) if window is not None else None
    rows = _store().query(since_ts=since)

    settings = current_app.settings_store.get()  # type: ignore[attr-defined]
    max_used_ratio = settings.retention.max_used_disk_percent / 100.0
    keep_days = _keep_days(settings.retention.keep)
    steady = _keep_steady_state(rows, keep_days)

    disk_points = [(r.ts_seconds, r.disk_used_ratio) for r in rows]
    forecast = compute_forecast(
        disk_points,
        horizon_seconds=(window or 7 * _SECONDS_PER_DAY),
        steps=12,
        max_used_disk_ratio=max_used_ratio,
        keep_steady_state_ratio=steady,
    )

    body = json.dumps(
        {
            "range": range_token,
            "summary": _summary(rows),
            "series": {"points": [_point(r) for r in rows]},
            "forecast": {
                "projected": [
                    {"ts": ts, "disk": disk} for ts, disk in forecast.projected
                ],
                # limit values are 0..1 ratios (e.g. 0.9 == 90%), not percentages
                "limits": {
                    "max_used_disk_percent": forecast.max_used_disk_percent,
                    "keep_steady_state": forecast.keep_steady_state,
                },
            },
        },
        default=str,
    )
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_stats_bp"]
