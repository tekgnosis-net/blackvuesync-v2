"""tests for the /api/camera json endpoints."""

# ruff: noqa: ARG001

from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import camera_config, create_app
from blackvuesync_v2.server.auth import (
    SESSION_VERSION_KEY,
    hash_password,
    session_version,
)
from blackvuesync_v2.settings import SettingsStore

FIXTURES = Path(__file__).parent / "fixtures" / "camera"
CONFIG = (FIXTURES / "dr900x-plus-config.ini").read_bytes()
VERSION = (FIXTURES / "dr900x-plus-version.bin").read_bytes()


class FakeCamera:
    """stands in for camera_config._get; files maps a path to bytes or None."""

    def __init__(self) -> None:
        self.files: dict[str, bytes | None] = {
            "/Config/config.ini": CONFIG,
            "/Config/version.bin": VERSION,
        }

    def get(self, url: str, timeout: float) -> bytes | None:
        assert timeout == 3.0
        return self.files.get(url.split("192.0.2.10", 1)[1])


@pytest.fixture()  # type: ignore[misc]
def camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    fake = FakeCamera()
    monkeypatch.setattr(camera_config, "_get", fake.get)
    return fake


@pytest.fixture()  # type: ignore[misc]
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password("pw-1234-test")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=pw_hash)
        )
    )
    app = create_app(store, testing=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(pw_hash)
    return c


def test_config_returns_tabs_with_masked_passwords(
    client: Any,
    camera: FakeCamera,
) -> None:
    resp = client.get("/api/camera/config")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    data = json.loads(text)
    assert (data["available"], data["online"]) == (True, True)
    assert data["model"] == "DR900X Plus" and data["firmware"] == "1.015"
    tabs = {t["name"]: t for t in data["tabs"]}
    cloud = {f["key"]: f for f in tabs["cloud"]["fields"]}
    assert cloud["Cloud.sta_pw"]["value"] == "•" * 8
    assert cloud["Cloud.sta_pw"]["raw"] is None
    assert "1E1BC3E1" not in text and "DemoHome-123" not in text
    assert data["changed"] == []


def test_config_reports_changes_since_the_last_read(
    client: Any, camera: FakeCamera
) -> None:
    client.get("/api/camera/config")
    camera.files["/Config/config.ini"] = CONFIG.replace(
        b"VOLUME=5", b"VOLUME=4"
    ).replace(b"sta_ssid=DemoHome", b"sta_ssid=NewHome")
    data = client.get("/api/camera/config").get_json()
    assert data["changed"] == [
        {"key": "Tab3.VOLUME", "label": "Volume", "from": "5", "to": "4"},
        {
            "key": "Cloud.sta_ssid",
            "label": "Home network 1 name",
            "from": "DemoHome",
            "to": "NewHome",
        },
    ]


def test_password_changes_are_reported_without_values(
    client: Any, camera: FakeCamera
) -> None:
    client.get("/api/camera/config")
    camera.files["/Config/config.ini"] = CONFIG.replace(b"ap_pw=", b"ap_pw=0")
    data = client.get("/api/camera/config").get_json()
    assert data["changed"] == [
        {
            "key": "Wifi.ap_pw",
            "label": "Camera hotspot password",
            "from": "changed",
            "to": "changed",
        }
    ]


def test_offline_serves_the_snapshot(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    camera.files = {}
    data = client.get("/api/camera/config").get_json()
    assert (data["available"], data["online"]) == (True, False)
    assert data["changed"] == []


def test_never_reached_reports_unavailable(client: Any, camera: FakeCamera) -> None:
    camera.files = {}
    assert client.get("/api/camera/config").get_json() == {
        "available": False,
        "online": False,
    }


def test_unwritable_snapshot_still_serves_live_settings(
    client: Any,
    camera: FakeCamera,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """review focus 3: a read-only settings directory is not a 500."""
    (tmp_path / "camera").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        data = client.get("/api/camera/config").get_json()
    assert data["online"] is True
    assert "could not save the camera snapshot" in caplog.text


def test_secret_returns_one_decrypted_password(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    resp = client.get("/api/camera/secret?key=Cloud.sta_pw")
    assert resp.status_code == 200
    assert resp.get_json() == {"key": "Cloud.sta_pw", "value": "DemoHome-123"}
    assert resp.headers["Cache-Control"] == "no-store"


def test_secret_refuses_other_keys_and_no_snapshot(
    client: Any,
    camera: FakeCamera,
) -> None:
    assert client.get("/api/camera/secret?key=Cloud.sta_pw").status_code == 404
    client.get("/api/camera/config")
    for key in ("Cloud.sta_ssid", "Tab1.TimeZone", "", "Cloud", "Wifi.ap_pw/x"):
        assert client.get(f"/api/camera/secret?key={key}").status_code == 404


def test_secret_that_does_not_decrypt_is_a_422(client: Any, camera: FakeCamera) -> None:
    """review focus 4: never show garbage as a password."""
    camera.files["/Config/config.ini"] = CONFIG.replace(
        b"ap_pw=", b"ap_pw=" + b"00" * 32 + b"\nIgnored="
    )
    client.get("/api/camera/config")
    resp = client.get("/api/camera/secret?key=Wifi.ap_pw")
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "UNDECODABLE_PASSWORD"


def test_camera_api_requires_login(tmp_path: Path, camera: FakeCamera) -> None:
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, password_hash=hash_password("pw-1234-test")
            ),
        )
    )
    anon = create_app(store, testing=True).test_client()
    assert anon.get("/api/camera/config").status_code == 401
    assert anon.get("/api/camera/secret?key=Cloud.sta_pw").status_code == 401


# ---------------------------------------------------------------------------
# pending count and apply
# ---------------------------------------------------------------------------


@pytest.fixture()  # type: ignore[misc]
def apply_camera(monkeypatch: pytest.MonkeyPatch) -> Any:
    from test_camera_apply import FakeCamera as ApplyCamera

    from blackvuesync_v2.server import camera_apply

    fake = ApplyCamera()
    monkeypatch.setattr(camera_config, "_get", fake.get)
    monkeypatch.setattr(camera_config, "_post", fake.post)
    monkeypatch.setattr(camera_apply, "_list_camera", lambda *_a: list(fake.vod))
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    return fake


def test_pending_counts_and_reports_offline(
    client: Any, apply_camera: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blackvuesync_v2.server import camera_apply

    resp = client.get("/api/camera/pending")
    assert (resp.status_code, resp.get_json()) == (200, {"pending": 0})

    def boom(*_args: object) -> list[str]:
        raise OSError("no route")

    monkeypatch.setattr(camera_apply, "_list_camera", boom)
    resp = client.get("/api/camera/pending")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "CAMERA_OFFLINE"


def test_apply_changes_route(client: Any, apply_camera: Any) -> None:
    resp = client.post("/api/camera/config", json={"changes": {"Tab3.VOLUME": "4"}})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["verified"] is True and data["came_back"] is True
    assert data["changed"] == ["Tab3.VOLUME"]


def test_apply_changes_route_rejects_bad_bodies(client: Any, apply_camera: Any) -> None:
    for body in (None, {}, {"changes": "x"}, {"changes": {"Tab3.VOLUME": 4}}):
        resp = client.post("/api/camera/config", json=body)
        assert resp.status_code == 422
        assert resp.get_json()["code"] == "INVALID_CHANGES"
    resp = client.post("/api/camera/config", json={"changes": {"Tab3.VOLUME": "x"}})
    assert (resp.status_code, resp.get_json()["code"]) == (422, "INVALID_CHANGES")


def test_apply_format_key_needs_confirmation(client: Any, apply_camera: Any) -> None:
    apply_camera.vod = ["20261027_210000_NF.mp4"]
    resp = client.post(
        "/api/camera/config", json={"changes": {"Tab1.TimeZone": "1100"}}
    )
    assert resp.status_code == 409
    data = resp.get_json()
    assert (data["code"], data["pending"]) == ("FORMAT_CONFIRMATION_REQUIRED", 1)
    assert apply_camera.uploads == []


def test_apply_requires_login_and_csrf(tmp_path: Path) -> None:
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password("pw-1234-test")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=pw_hash)
        )
    )
    anon = create_app(store, testing=True).test_client()
    resp = anon.post("/api/camera/config", json={"changes": {"Tab3.VOLUME": "4"}})
    assert resp.status_code == 401
    strict = create_app(store, testing=False).test_client()
    with strict.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(pw_hash)
    resp = strict.post("/api/camera/config", json={"changes": {"Tab3.VOLUME": "4"}})
    assert resp.status_code == 400


def test_format_confirmations_must_be_json_true(client: Any, apply_camera: Any) -> None:
    resp = client.post(
        "/api/camera/config",
        json={"changes": {"Tab1.TimeZone": "1100"}, "confirm_format": "false"},
    )
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "FORMAT_CONFIRMATION_REQUIRED"
    assert apply_camera.uploads == []
