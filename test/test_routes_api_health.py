"""tests for /api/health/storage and /api/health/dashcam."""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _make_app(settings_path: Path, destination: Path | None = None):  # type: ignore[no-untyped-def]
    store = _make_store(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
            system=dataclasses.replace(
                s.system,
                destination=str(destination) if destination else s.system.destination,
            ),
        )
    )
    return create_app(store, testing=True), store


@pytest.fixture()
def logged_in_client(settings_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    """returns a logged-in flask test client with a real destination directory."""
    destination = tmp_path / "recordings"
    destination.mkdir()
    app, store = _make_app(settings_path, destination=destination)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, store, destination


class TestStorage:
    """tests for GET /api/health/storage."""

    def test_returns_available_true_for_existing_destination(
        self, logged_in_client: Any
    ) -> None:
        client, _, _ = logged_in_client
        resp = client.get("/api/health/storage")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["available"] is True
        assert "total_bytes" in body
        assert "free_bytes" in body
        assert "used_bytes" in body
        assert "used_percent" in body
        assert "recording_count" in body
        assert body["recording_count"] == 0

    def test_counts_recordings_in_destination(self, logged_in_client: Any) -> None:
        """recording_count reflects files matching the BlackVue filename regex."""
        client, _, destination = logged_in_client
        # create 2 valid recordings and 1 non-matching file
        (destination / "20231015_120000_NF.mp4").write_text("x")
        (destination / "20231015_115400_NR.mp4").write_text("y")
        (destination / "notes.txt").write_text("ignored")

        resp = client.get("/api/health/storage")
        body = json.loads(resp.data)
        assert body["recording_count"] == 2

    def test_does_not_count_files_with_prefix_match(
        self, logged_in_client: Any
    ) -> None:
        """fullmatch (not match) protects against suffix-bearing files like .bak."""
        client, _, destination = logged_in_client
        # the valid recording
        (destination / "20231015_120000_NF.mp4").write_text("x")
        # files that match the prefix but shouldn't count
        (destination / "20231015_120000_NF.mp4.bak").write_text("y")
        (destination / "20231015_120000_NF.mp4.partial").write_text("z")

        resp = client.get("/api/health/storage")
        body = json.loads(resp.data)
        assert body["recording_count"] == 1

    def test_returns_unavailable_for_missing_destination(
        self, settings_path: Path, tmp_path: Path
    ) -> None:
        """when destination does not exist on disk, returns available=false."""
        missing = tmp_path / "does-not-exist"
        app, _ = _make_app(settings_path, destination=missing)
        with app.test_client() as client:
            client.post(
                "/login",
                data={"username": "admin", "password": "test-password-1234"},
                follow_redirects=True,
            )
            resp = client.get("/api/health/storage")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["available"] is False
        assert body["reason"] == "destination not configured"

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        app, _ = _make_app(settings_path)
        with app.test_client() as client:
            resp = client.get("/api/health/storage")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"


class TestDashcam:
    """tests for GET /api/health/dashcam."""

    def test_returns_reachable_true_on_success(self, logged_in_client: Any) -> None:
        """when the HEAD probe succeeds, reachable=true with latency_ms set."""
        client, _, _ = logged_in_client
        # mock urlopen to simulate a successful HEAD response
        with patch("urllib.request.urlopen") as mock_open:
            mock_open.return_value.__enter__.return_value.status = 200
            resp = client.get("/api/health/dashcam")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["reachable"] is True
        assert "latency_ms" in body
        assert isinstance(body["latency_ms"], (int, float))
        assert body["address"] == "192.168.0.1"
        # verify the call shape locks the HEAD-probe contract
        mock_open.assert_called_once()
        req = mock_open.call_args.args[0]
        assert req.get_method() == "HEAD"
        assert req.full_url == "http://192.168.0.1/blackvue_vod.cgi"

    def test_returns_reachable_false_on_timeout(self, logged_in_client: Any) -> None:
        """when the HEAD probe times out, reachable=false with reason=timeout."""
        import socket

        client, _, _ = logged_in_client
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            resp = client.get("/api/health/dashcam")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["reachable"] is False
        assert body["reason"] == "timeout"

    def test_url_error_wrapped_timeout_classifies_as_timeout(
        self, logged_in_client: Any
    ) -> None:
        """connect timeouts arrive as URLError(reason=TimeoutError); the helper
        must still classify them as reason='timeout' for ui consistency."""
        import urllib.error

        client, _, _ = logged_in_client
        wrapped = urllib.error.URLError(reason=TimeoutError("connect timed out"))
        with patch("urllib.request.urlopen", side_effect=wrapped):
            resp = client.get("/api/health/dashcam")
        body = json.loads(resp.data)
        assert body["reachable"] is False
        assert body["reason"] == "timeout"

    def test_returns_reachable_false_on_connection_refused(
        self, logged_in_client: Any
    ) -> None:
        """when the HEAD probe is refused, reachable=false with a reason."""
        import urllib.error

        client, _, _ = logged_in_client
        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("Connection refused"),
        ):
            resp = client.get("/api/health/dashcam")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["reachable"] is False
        assert "reason" in body

    def test_compute_dashcam_returns_no_address_when_empty(self) -> None:
        """unit-test the _compute_dashcam helper with an empty address.

        we cannot exercise this through the route because
        ConnectionSettings.validate() rejects an empty address and
        SettingsStore.update would refuse the change. testing the helper
        directly is cleaner and covers the same code path.
        """
        from blackvuesync_v2.server.routes.api_health import _compute_dashcam

        result = _compute_dashcam("")
        assert result["reachable"] is False
        assert result["reason"] == "no address configured"
