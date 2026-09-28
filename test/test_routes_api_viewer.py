"""tests for the /api/viewer JSON endpoints."""

from __future__ import annotations

import dataclasses
import json
import os
import struct
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import (
    SESSION_VERSION_KEY,
    hash_password,
    session_version,
)
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def client_and_dest(tmp_path: Path):  # type: ignore[no-untyped-def]
    dest = tmp_path / "recordings"
    dest.mkdir()
    for name in (
        "20260607_101500_NF.mp4",
        "20260607_101500_NR.mp4",
        "20260607_101600_NF.mp4",
    ):
        (dest / name).write_bytes(b"x")
    (dest / "20260607_101500_N.gps").write_text(
        "[1000]$GNRMC,055056.00,A,3348.10000,S,15101.10000,E,0.000,,070626,,,A,V*06\r\n"
    )
    (dest / "20260607_101500_N.3gf").write_bytes(struct.pack(">Ihhh", 0, 130, 5, -20))
    with patch.dict(os.environ, {"ADDRESS": "1.2.3.4"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, password_hash=hash_password("pw-1234-test")
            ),
            system=dataclasses.replace(s.system, destination=str(dest)),
        )
    )
    app = create_app(store, testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(
            app.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
        )
    return client, dest


def test_recordings_grouped_newest_first(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    body = json.loads(client.get("/api/viewer/recordings").data)
    bases = [r["base_filename"] for day in body["days"] for r in day["recordings"]]
    assert bases == ["20260607_101600", "20260607_101500"]
    first = next(
        r
        for day in body["days"]
        for r in day["recordings"]
        if r["base_filename"] == "20260607_101500"
    )
    assert first["directions"] == ["F", "R"]
    assert first["has_gps"] is True and first["has_3gf"] is True


def test_journey_chain(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    body = json.loads(
        client.get("/api/viewer/recordings/20260607_101500_N/journey").data
    )
    assert [s["base_filename"] for s in body["segments"]] == [
        "20260607_101500",
        "20260607_101600",
    ]


def test_journey_includes_other_types_only_with_continuous_play(
    client_and_dest: Any,
) -> None:
    client, dest = client_and_dest
    (dest / "20260607_101700_EF.mp4").write_bytes(b"x")
    (dest / "20260607_101800_NF.mp4").write_bytes(b"x")
    url = "/api/viewer/recordings/20260607_101500_N/journey"

    def segments() -> list[str]:
        body = json.loads(client.get(url).data)
        return [s["base_filename"] + "_" + s["type"] for s in body["segments"]]

    # default: the event is skipped and the next normal segment is linked
    assert segments() == ["20260607_101500_N", "20260607_101600_N", "20260607_101800_N"]
    client.application.settings_store.update(
        lambda s: dataclasses.replace(
            s, viewer=dataclasses.replace(s.viewer, continuous_play=True)
        )
    )
    assert segments() == [
        "20260607_101500_N",
        "20260607_101600_N",
        "20260607_101700_E",
        "20260607_101800_N",
    ]


def test_gps_and_gsensor_json(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    gps = json.loads(client.get("/api/viewer/recordings/20260607_101500_N/gps").data)
    assert gps["points"][0]["lat"] == pytest.approx(-(33 + 48.1 / 60))
    g = json.loads(client.get("/api/viewer/recordings/20260607_101500_N/gsensor").data)
    assert g["samples"][0]["x"] == 130 / 128.0


def test_requires_login(client_and_dest: Any) -> None:
    _, dest = client_and_dest
    with patch.dict(os.environ, {"ADDRESS": "1.2.3.4"}, clear=False):
        anon = create_app(SettingsStore(dest.parent / "s2.json"), testing=True)
    assert anon.test_client().get("/api/viewer/recordings").status_code in (302, 401)


def test_unknown_key_returns_404(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    assert (
        client.get("/api/viewer/recordings/99999999_000000_N/journey").status_code
        == 404
    )
    assert client.get("/api/viewer/recordings/99999999_000000_N/gps").status_code == 404


def test_gps_404_when_recording_has_no_gps(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    # 20260607_101600_N exists (mp4) but has no .gps sidecar
    assert client.get("/api/viewer/recordings/20260607_101600_N/gps").status_code == 404


def test_viewer_page_renders(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/viewer")
    assert resp.status_code == 200
    assert b"js/viewer.js" in resp.data
    assert b"js/leaflet.js" in resp.data
    assert b'id="viewer-app"' in resp.data
    assert b"data-journey-mode" in resp.data


def _recording(client: Any, base: str) -> dict[str, Any]:
    body = json.loads(client.get("/api/viewer/recordings").data)
    return next(
        r
        for day in body["days"]
        for r in day["recordings"]
        if r["base_filename"] == base
    )


def test_upload_flag_urls_use_real_filenames(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    (dest / "20260607_103000_EFL.mp4").write_bytes(b"x")
    (dest / "20260607_103000_ERS.mp4").write_bytes(b"x")
    (dest / "20260607_103000_EF.thm").write_bytes(b"x")
    (dest / "20260607_103000_E.gps").write_text(
        "[1000]$GNRMC,055056.00,A,3348.10000,S,15101.10000,E,0.000,,070626,,,A,V*06\r\n"
    )
    rec = _recording(client, "20260607_103000")
    assert rec["videos"] == {
        "F": "/media/20260607_103000_EFL.mp4",
        "R": "/media/20260607_103000_ERS.mp4",
    }
    assert rec["thumb"] == "/media/20260607_103000_EF.thm"
    assert rec["has_thm"] is True and rec["has_gps"] is True
    for url in (*rec["videos"].values(), rec["thumb"]):
        assert client.get(url).status_code == 200
    gps = client.get("/api/viewer/recordings/20260607_103000_E/gps")
    assert gps.status_code == 200


def test_thumb_url_uses_direction_that_has_thm(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    (dest / "20260607_101500_NR.thm").write_bytes(b"x")
    rec = _recording(client, "20260607_101500")
    assert rec["thumb"] == "/media/20260607_101500_NR.thm"
    assert client.get(rec["thumb"]).status_code == 200
    (dest / "20260607_101500_NF.thm").write_bytes(b"x")
    rec = _recording(client, "20260607_101500")
    assert rec["thumb"] == "/media/20260607_101500_NF.thm"  # front preferred


def test_thumb_none_without_thm(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    rec = _recording(client, "20260607_101600")
    assert rec["thumb"] is None and rec["has_thm"] is False


def test_gps_nan_speed_is_valid_json(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    (dest / "20260607_101500_N.gps").write_text(
        "[1000]$GNRMC,055056.00,A,3348.10000,S,15101.10000,E,nan,,070626,,,A,V*06\r\n"
    )
    resp = client.get("/api/viewer/recordings/20260607_101500_N/gps")
    body = json.loads(resp.data, parse_constant=_reject_constant)
    assert body["points"][0]["speed"] is None


def _reject_constant(name: str) -> None:
    raise ValueError(f"invalid JSON constant {name}")


def _second_day(dest: Path) -> None:
    (dest / "20260608_000030_NF.mp4").write_bytes(b"x")
    (dest / "20260608_000130_NF.mp4").write_bytes(b"x")


def test_days_lists_counts_newest_first(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    _second_day(dest)
    body = json.loads(client.get("/api/viewer/days").data)
    assert body == {
        "days": [
            {"date": "2026-06-08", "count": 2},
            {"date": "2026-06-07", "count": 2},
        ]
    }


def test_recordings_defaults_to_the_newest_day(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    _second_day(dest)
    body = json.loads(client.get("/api/viewer/recordings").data)
    assert [d["date"] for d in body["days"]] == ["2026-06-08"]


def test_recordings_date_filters_to_one_day(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    _second_day(dest)
    body = json.loads(client.get("/api/viewer/recordings?date=2026-06-07").data)
    assert [d["date"] for d in body["days"]] == ["2026-06-07"]
    assert [r["base_filename"] for r in body["days"][0]["recordings"]] == [
        "20260607_101600",
        "20260607_101500",
    ]
    empty = json.loads(client.get("/api/viewer/recordings?date=2020-01-01").data)
    assert empty == {"days": []}


def test_recordings_invalid_date_is_422(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/api/viewer/recordings?date=yesterday")
    assert resp.status_code == 422
    assert json.loads(resp.data)["code"] == "INVALID_DATE"


def test_days_requires_login(client_and_dest: Any) -> None:
    _, dest = client_and_dest
    with patch.dict(os.environ, {"ADDRESS": "1.2.3.4"}, clear=False):
        anon = create_app(SettingsStore(dest.parent / "s3.json"), testing=True)
    assert anon.test_client().get("/api/viewer/days").status_code in (302, 401)


def test_journey_crosses_midnight_between_daily_directories(
    client_and_dest: Any,
) -> None:
    client, dest = client_and_dest
    app = client.application
    app.settings_store.update(
        lambda s: dataclasses.replace(
            s, sync=dataclasses.replace(s.sync, grouping="daily")
        )
    )
    for day, name in (
        ("2026-06-09", "20260609_235930_NF.mp4"),
        ("2026-06-10", "20260610_000030_NF.mp4"),
    ):
        (dest / day).mkdir()
        (dest / day / name).write_bytes(b"x")
    body = json.loads(
        client.get("/api/viewer/recordings/20260609_235930_N/journey").data
    )
    assert [s["base_filename"] for s in body["segments"]] == [
        "20260609_235930",
        "20260610_000030",
    ]
    assert body["segments"][1]["videos"]["F"] == (
        "/media/2026-06-10/20260610_000030_NF.mp4"
    )
