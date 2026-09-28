"""unit tests for the blackvue .3gf accelerometer parser."""

from __future__ import annotations

import struct

from blackvuesync_v2.server.gsensor import SCALE_G, GForce, parse_gsensor


def _record(ms: int, x: int, y: int, z: int) -> bytes:
    return struct.pack(">Ihhh", ms, x, y, z)


def test_parses_big_endian_10_byte_records() -> None:
    data = _record(735, 130, 5, -20) + _record(840, 129, 5, -20)
    points = parse_gsensor(data)
    assert len(points) == 2
    assert isinstance(points[0], GForce)
    assert points[0].t == 0.735
    assert points[0].x == 130 / SCALE_G
    assert points[0].z == -20 / SCALE_G
    assert points[1].t == 0.840


def test_trailing_partial_record_ignored() -> None:
    data = _record(1000, 1, 2, 3) + b"\x00\x00\x00"  # 3 dangling bytes
    points = parse_gsensor(data)
    assert len(points) == 1


def test_empty_returns_empty() -> None:
    assert parse_gsensor(b"") == []
