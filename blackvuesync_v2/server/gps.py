"""parser for blackvue .gps sidecar files: timestamped NMEA-0183 text.

format (see docs/reference/blackvue-file-formats.md): each line is
`[epoch-ms]$G?RMC,...` or `[epoch-ms]$G?GGA,...`. the talker is multi-gnss
($GN), so matching is talker-agnostic on the sentence type. stdlib-only.
"""

from __future__ import annotations

import dataclasses
import math
import re

# [epoch-ms] + $ + G + any talker letter + RMC|GGA + comma + the field body.
_SENTENCE_RE = re.compile(
    r"\[(?P<ms>\d+)\]\$G[A-Z](?P<kind>RMC|GGA),(?P<fields>[^*\r\n]*)"
)


@dataclasses.dataclass(frozen=True)
class GpsPoint:
    """one GPS fix: seconds from the file's first sentence, lat/lon, knots."""

    t: float
    lat: float
    lon: float
    speed: float | None


def _dm_to_decimal(value: str, hemisphere: str) -> float | None:
    """converts a DDMM.mmmmm / DDDMM.mmmmm + hemisphere string to decimal degrees."""
    if not value:
        return None
    raw = float(value)
    if not math.isfinite(raw):
        return None
    degrees = int(raw // 100)
    minutes = raw - degrees * 100
    decimal = degrees + minutes / 60.0
    return -decimal if hemisphere in ("S", "W") else decimal


def _parse_rmc(fields: list[str]) -> tuple[float, float, float | None] | None:
    """returns (lat, lon, speed_knots) from RMC fields, or None when no fix."""
    # RMC: time, status, lat, N/S, lon, E/W, speed, course, date, ...
    if len(fields) < 7 or fields[1] != "A":
        return None
    lat = _dm_to_decimal(fields[2], fields[3])
    lon = _dm_to_decimal(fields[4], fields[5])
    if lat is None or lon is None:
        return None
    speed = float(fields[6]) if fields[6] else None
    if speed is not None and not math.isfinite(speed):
        speed = None  # nan/inf would serialize as invalid JSON
    return lat, lon, speed


def _parse_gga(fields: list[str]) -> tuple[float, float, float | None] | None:
    """returns (lat, lon, None) from GGA fields, or None when no fix."""
    # GGA: time, lat, N/S, lon, E/W, fix-quality, ...
    if len(fields) < 6 or fields[5] in ("", "0"):
        return None
    lat = _dm_to_decimal(fields[1], fields[2])
    lon = _dm_to_decimal(fields[3], fields[4])
    if lat is None or lon is None:
        return None
    return lat, lon, None


def parse_gps(text: str) -> list[GpsPoint]:
    """parses .gps text into GpsPoints, ascending by time, one per epoch-ms.

    RMC is preferred (it carries speed); a GGA-only timestamp is used as a
    position fallback. invalid / no-fix / unparseable lines are skipped. t=0 is
    the earliest timestamp of any matched sentence, fix or not, since the file
    starts with the video even before the receiver has a fix.
    """
    by_ms: dict[int, tuple[float, float, float | None]] = {}
    rmc_ms: set[int] = set()
    first_ms: int | None = None
    for match in _SENTENCE_RE.finditer(text):
        ms = int(match.group("ms"))
        first_ms = ms if first_ms is None else min(first_ms, ms)
        fields = match.group("fields").split(",")
        try:
            if match.group("kind") == "RMC":
                parsed = _parse_rmc(fields)
                if parsed is not None:
                    by_ms[ms] = parsed
                    rmc_ms.add(ms)
            elif ms not in rmc_ms:  # GGA only fills positions without an RMC
                parsed = _parse_gga(fields)
                if parsed is not None:
                    by_ms[ms] = parsed
        except ValueError:
            continue  # one malformed sentence does not abort the whole parse
    if first_ms is None:
        return []
    return [
        GpsPoint((ms - first_ms) / 1000.0, lat, lon, speed)
        for ms, (lat, lon, speed) in sorted(by_ms.items())
    ]


__all__ = ["GpsPoint", "parse_gps"]
