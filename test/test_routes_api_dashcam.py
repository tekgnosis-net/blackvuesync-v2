"""tests for /api/dashcam/info and its parse helpers."""

from __future__ import annotations

import dataclasses
import json
import os
import socket
import urllib.error
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore

FIXTURES = Path(__file__).parent / "fixtures" / "camera"


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _make_app(settings_path: Path):  # type: ignore[no-untyped-def]
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )
    return create_app(store, testing=True), store


@pytest.fixture()
def logged_in_client(settings_path: Path):  # type: ignore[no-untyped-def]
    app, store = _make_app(settings_path)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, store


def _fake_response(body: bytes):  # type: ignore[no-untyped-def]
    """builds a context-manager stand-in for urlopen returning the given bytes."""

    class _Ctx:
        def __enter__(self) -> Any:
            return BytesIO(body)

        def __exit__(self, *a: Any) -> None:
            return None

    return _Ctx()


def _camera_urlopen(url: str, *_args: Any, **_kwargs: Any) -> Any:
    name = (
        "dr900x-plus-config.ini"
        if url.endswith("config.ini")
        else "dr900x-plus-version.bin"
    )
    return _fake_response((FIXTURES / name).read_bytes())


def _fixture_ciphertexts() -> list[str]:
    """extracts all password values from the fixture config file."""
    ciphertexts: list[str] = []
    for line in (FIXTURES / "dr900x-plus-config.ini").read_text().splitlines():
        if "_pw=" in line:
            ciphertexts.append(line.split("=", 1)[1])
    return ciphertexts


class TestParseHelpers:
    """unit tests for the version.bin and config.ini parsers."""

    def test_firmware_string_strips_control_chars(self) -> None:
        from blackvuesync_v2.server.camera_config import parse_version

        assert parse_version(b"DR900X-2.013\x00\x01").description == "DR900X-2.013"

    def test_config_sections_returns_nested_dict(self) -> None:
        from blackvuesync_v2.server.routes.api_dashcam import _config_sections

        text = "[Tab1]\nResolution=4K\n[Tab3]\nVoice=ON\n"
        parsed = _config_sections(text)
        assert parsed["Tab1"]["Resolution"] == "4K"
        assert parsed["Tab3"]["Voice"] == "ON"

    def test_config_sections_handles_missing_section_header(self) -> None:
        """legacy firmware may omit a leading section header; parser recovers."""
        from blackvuesync_v2.server.routes.api_dashcam import _config_sections

        parsed = _config_sections("Resolution=4K\nVoice=ON\n")
        # the synthetic default section captures the header-less keys
        assert any("Resolution" in keys for keys in parsed.values())

    def test_indented_secret_line_is_still_masked(self) -> None:
        from blackvuesync_v2.server.routes.api_dashcam import (
            _config_sections,
            _mask_secrets,
        )

        text = "[Wifi]\nap_ssid=Cam\n  ap_pw=0123456789ABCDEF\n"
        masked = _mask_secrets(_config_sections(text))
        assert masked["Wifi"]["ap_pw"] == "***"
        assert "0123456789ABCDEF" not in str(masked)

    def test_config_preview_flattens_and_limits(self) -> None:
        from blackvuesync_v2.server.routes.api_dashcam import _config_preview

        config = {"Tab1": {"A": "1", "B": "2"}, "Tab2": {"C": "3"}}
        preview = _config_preview(config, limit=2)
        assert preview == [("Tab1.A", "1"), ("Tab1.B", "2")]


class TestDashcamInfo:
    """tests for GET /api/dashcam/info."""

    def test_returns_available_with_parsed_data(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client

        def _fake_urlopen(req: Any, timeout: float = 0) -> Any:  # noqa: ARG001
            url = req.full_url if hasattr(req, "full_url") else req
            if url.endswith("version.bin"):
                return _fake_response(b"DR900X-2.013")
            return _fake_response(b"[Tab1]\nResolution=4K\n")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            resp = client.get("/api/dashcam/info")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["available"] is True
        assert body["firmware"] == "DR900X-2.013"
        assert body["config"]["Tab1"]["Resolution"] == "4K"
        assert body["setting_count"] == 1

    def test_returns_unavailable_when_unreachable(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            resp = client.get("/api/dashcam/info")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["available"] is False
        assert "reason" in body

    def test_returns_no_address_when_unconfigured(self) -> None:
        """unit-test the helper directly: ConnectionSettings rejects empty
        address so the route path cannot be exercised with one."""
        from blackvuesync_v2.server.routes.api_dashcam import _compute_dashcam_info

        result = _compute_dashcam_info("")
        assert result["available"] is False
        assert result["reason"] == "no address configured"

    def test_partial_data_when_only_config_reachable(
        self, logged_in_client: Any
    ) -> None:
        """if version.bin fails but config.ini succeeds, still available."""
        client, _ = logged_in_client

        def _fake_urlopen(req: Any, timeout: float = 0) -> Any:  # noqa: ARG001
            url = req.full_url if hasattr(req, "full_url") else req
            if url.endswith("version.bin"):
                raise urllib.error.URLError("refused")
            return _fake_response(b"[Tab3]\nVoice=ON\n")

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            resp = client.get("/api/dashcam/info")
        body = json.loads(resp.data)
        assert body["available"] is True
        assert body.get("firmware") in (None, "")
        assert body["config"]["Tab3"]["Voice"] == "ON"

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        app, _ = _make_app(settings_path)
        with app.test_client() as client:
            resp = client.get("/api/dashcam/info")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"


def test_info_masks_passwords_and_describes_the_model(
    logged_in_client: Any,
) -> None:
    client, _store = logged_in_client
    with patch("urllib.request.urlopen", _camera_urlopen):
        body = client.get("/api/dashcam/info").get_data(as_text=True)
    data = json.loads(body)
    assert data["firmware"] == "DR900X Plus · fw 1.015"
    assert data["config"]["Cloud"]["sta_pw"] == "***"
    assert data["config"]["Cloud"]["sta2_pw"] == "***"
    assert data["config"]["Cloud"]["sta3_pw"] == "***"
    assert data["config"]["Wifi"]["ap_pw"] == "***"
    assert data["config"]["Cloud"]["sta_ssid"] == "DemoHome"
    for ciphertext in _fixture_ciphertexts():
        assert ciphertext not in body


def test_info_card_never_contains_a_password(logged_in_client: Any) -> None:
    client, _store = logged_in_client
    with patch("urllib.request.urlopen", _camera_urlopen):
        html = client.get("/hx/dashcam-info-card").get_data(as_text=True)
    assert "DR900X Plus · fw 1.015" in html
    for ciphertext in _fixture_ciphertexts():
        assert ciphertext not in html
