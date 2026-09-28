"""server-side disk-usage forecast for the statistics page.

projects the recent disk-used ratio forward with a least-squares line, clamped
to whichever configured ceiling binds first (the hard max-used-disk cap and/or
the keep-days steady state). pure functions -- unit-testable, no flask/sqlite.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

_MIN_POINTS = 3


@dataclasses.dataclass(frozen=True)
class Forecast:
    """a disk-usage projection plus the limit lines to draw."""

    projected: list[tuple[float, float]]  # (ts_seconds, disk_ratio), ascending
    max_used_disk_percent: float | None  # 0..1 ratio, or None when unset
    keep_steady_state: float | None  # 0..1 ratio, or None when not estimable


def _linear_fit(points: list[tuple[float, float]]) -> tuple[float, float]:
    """returns (slope, intercept) of the least-squares line through points."""
    n = len(points)
    mean_x = sum(x for x, _ in points) / n
    mean_y = sum(y for _, y in points) / n
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in points)
    denominator = sum((x - mean_x) ** 2 for x, _ in points)
    if denominator == 0:
        return 0.0, mean_y
    slope = numerator / denominator
    return slope, mean_y - slope * mean_x


def compute_forecast(
    disk_points: Sequence[tuple[float, float | None]],
    *,
    horizon_seconds: float,
    steps: int,
    max_used_disk_ratio: float | None,
    keep_steady_state_ratio: float | None,
) -> Forecast:
    """projects disk usage forward over horizon_seconds in `steps` points.

    points with a None ratio are ignored. with fewer than _MIN_POINTS valid
    points the projection is empty (the chart omits it). the projection is
    clamped to [0, 1] and to the lowest configured ceiling.
    """
    clean = [(x, y) for x, y in disk_points if y is not None]
    if len(clean) < _MIN_POINTS:
        return Forecast([], max_used_disk_ratio, keep_steady_state_ratio)

    slope, intercept = _linear_fit(clean)
    ceilings = [
        c for c in (max_used_disk_ratio, keep_steady_state_ratio) if c is not None
    ]
    ceiling = min(ceilings) if ceilings else None
    last_ts = clean[-1][0]

    projected: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        ts = last_ts + horizon_seconds * i / steps
        value = intercept + slope * ts
        if ceiling is not None:
            value = min(value, ceiling)
        value = max(0.0, min(1.0, value))
        projected.append((ts, value))
    return Forecast(projected, max_used_disk_ratio, keep_steady_state_ratio)


__all__ = ["Forecast", "compute_forecast"]
