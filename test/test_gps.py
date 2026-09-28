"""unit tests for the blackvue .gps NMEA parser."""

from __future__ import annotations

import dataclasses
import json

import pytest

from blackvuesync_v2.server.gps import GpsPoint, parse_gps

# synthetic, anonymized -- matches the real $GN framing (NOT $GP) and the
# [epoch-ms] line prefix, with made-up coordinates.
_TWO_POINTS = (
    "[1000]$GNRMC,055056.00,A,3348.10000,S,15101.10000,E,0.000,,070626,,,A,V*06\r\n"
    "[1000]$GNGGA,055056.00,3348.10000,S,15101.10000,E,1,12,0.68,52.8,M,19.4,M,,*6B\r\n"
    "\n"
    "[2000]$GNRMC,055057.00,A,3348.20000,N,15101.20000,W,12.340,,070626,,,A,V*07\r\n"
)
_NO_FIX = "[1000]$GNRMC,055056.00,V,,,,,,,070626,,,N*53\r\n"
_GGA_ONLY = (
    "[5000]$GNGGA,055056.00,1234.50000,N,12345.60000,W,1,07,1.2,5.0,M,,M,,*40\r\n"
)


def test_parses_rmc_points_with_elapsed_time_and_decimal_coords() -> None:
    points = parse_gps(_TWO_POINTS)
    assert len(points) == 2
    assert isinstance(points[0], GpsPoint)
    assert points[0].t == 0.0
    assert points[1].t == 1.0
    assert points[0].lat == pytest.approx(-(33 + 48.1 / 60))
    assert points[0].lon == pytest.approx(151 + 1.1 / 60)
    assert points[1].lat == pytest.approx(33 + 48.2 / 60)
    assert points[1].lon == pytest.approx(-(151 + 1.2 / 60))
    assert points[0].speed == 0.0
    assert points[1].speed == 12.34


def test_skips_no_fix_sentences() -> None:
    assert parse_gps(_NO_FIX) == []


def test_falls_back_to_gga_when_no_rmc() -> None:
    points = parse_gps(_GGA_ONLY)
    assert len(points) == 1
    assert points[0].speed is None
    assert points[0].lat == pytest.approx(12 + 34.5 / 60)


def test_empty_and_garbage_tolerated() -> None:
    assert parse_gps("") == []
    assert parse_gps("not nmea\n[bad]\n\n") == []


def test_malformed_numeric_field_is_skipped_not_fatal() -> None:
    text = (
        "[1000]$GNRMC,055056.00,A,BAD,N,XXXX,E,0.0,,070626,,,A*00\r\n"
        "[2000]$GNRMC,055057.00,A,3348.20000,N,15101.20000,E,1.0,,070626,,,A*00\r\n"
    )
    points = parse_gps(text)
    assert len(points) == 1  # the malformed sentence is skipped; the good one parses
    assert points[0].t == 1.0  # the malformed sentence still anchors t=0


def test_time_zero_is_first_sentence_even_without_fix() -> None:
    text = (
        "[1000]$GNRMC,055056.00,V,,,,,,,070626,,,N*53\r\n"
        "[1500]$GNGGA,055056.50,,,,,0,00,,,M,,M,,*00\r\n"
        "[4000]$GNRMC,055059.00,A,3348.20000,N,15101.20000,E,1.0,,070626,,,A*00\r\n"
    )
    points = parse_gps(text)
    assert [p.t for p in points] == [3.0]


def test_out_of_order_timestamps_use_earliest_as_zero() -> None:
    text = (
        "[3000]$GNRMC,055059.00,A,3348.20000,N,15101.20000,E,1.0,,070626,,,A*00\r\n"
        "[2000]$GNRMC,055058.00,V,,,,,,,070626,,,N*53\r\n"
    )
    assert [p.t for p in parse_gps(text)] == [1.0]


def test_non_finite_speed_becomes_none_and_json_is_valid() -> None:
    for bad in ("nan", "NaN", "inf", "-inf"):
        text = (
            "[1000]$GNRMC,055056.00,A,3348.10000,S,15101.10000,E,"
            f"{bad},,070626,,,A*00\r\n"
        )
        points = parse_gps(text)
        assert len(points) == 1
        assert points[0].speed is None
        json.dumps([dataclasses.asdict(p) for p in points], allow_nan=False)


def test_non_finite_coordinate_is_skipped() -> None:
    text = "[1000]$GNRMC,055056.00,A,inf,S,15101.10000,E,1.0,,070626,,,A*00\r\n"
    assert parse_gps(text) == []
