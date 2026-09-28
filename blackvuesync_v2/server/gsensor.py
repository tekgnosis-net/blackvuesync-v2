"""parser for blackvue .3gf accelerometer sidecar files (binary).

format (see docs/reference/blackvue-file-formats.md): packed big-endian 10-byte
records `[uint32 ms-from-start][int16 x][int16 y][int16 z]`, ~10 Hz, no header.
stdlib-only.
"""

from __future__ import annotations

import dataclasses
import struct

_RECORD = struct.Struct(">Ihhh")  # 10 bytes: uint32 ms + 3x int16

# raw int16 units per g. a stationary recording reads magnitude ~= 1 g, which
# matches ~128. confirm the canonical divisor against bartbroere/blackvue-acc;
# relative magnitude is correct regardless of the exact value.
SCALE_G = 128.0


@dataclasses.dataclass(frozen=True)
class GForce:
    """one accelerometer sample: elapsed seconds, x/y/z in g."""

    t: float
    x: float
    y: float
    z: float


def parse_gsensor(data: bytes) -> list[GForce]:
    """parses .3gf bytes into GForce samples; ignores a trailing partial record."""
    usable = len(data) - (len(data) % _RECORD.size)
    return [
        GForce(ms / 1000.0, x / SCALE_G, y / SCALE_G, z / SCALE_G)
        for ms, x, y, z in _RECORD.iter_unpack(data[:usable])
    ]


__all__ = ["GForce", "SCALE_G", "parse_gsensor"]
