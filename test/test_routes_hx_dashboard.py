"""tests for /hx/storage-card, /hx/dashcam-card, /hx/next-scheduled-card,
/hx/recent-activity-card HTMX fragments."""

from __future__ import annotations

import dataclasses
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


def _make_app(settings_path: Path, destination: Path):  # type: ignore[no-untyped-def]
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
            system=dataclasses.replace(s.system, destination=str(destination)),
        )
    )
    return create_app(store, testing=True), store


@pytest.fixture()
def logged_in_client(settings_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    app, store = _make_app(settings_path, destination)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, store, destination


class TestStorageCard:
    """tests for GET /hx/storage-card."""

    def test_renders_html(self, logged_in_client: Any) -> None:
        client, _, _ = logged_in_client
        resp = client.get("/hx/storage-card")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        assert b"storage-card" in resp.data
        assert b"hx-trigger" in resp.data  # has polling trigger

    def test_redirects_when_unauthenticated(
        self, settings_path: Path, tmp_path: Path
    ) -> None:
        destination = tmp_path / "recordings"
        destination.mkdir()
        app, _ = _make_app(settings_path, destination)
        with app.test_client() as client:
            resp = client.get("/hx/storage-card")
        assert resp.status_code == 302


class TestDashcamCard:
    """tests for GET /hx/dashcam-card."""

    def test_renders_html(self, logged_in_client: Any) -> None:
        import socket

        client, _, _ = logged_in_client
        # mock the urlopen call to avoid hitting a real network
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            resp = client.get("/hx/dashcam-card")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        assert b"dashcam-card" in resp.data

    def test_partial_renders_checking_shell_without_context(self) -> None:
        """rendered with no context (the SSR shell case), the card shows a
        'checking' state rather than a misleading 'unreachable'."""
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from flask import render_template

        from blackvuesync_v2.server import create_app
        from blackvuesync_v2.settings import SettingsStore

        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
                store = SettingsStore(path)
            app = create_app(store, testing=True)
            with app.app_context():
                html = render_template("_partials/dashcam_card.html")
        assert "checking" in html.lower()
        assert "dashcam-card" in html


class TestNextScheduledCard:
    """tests for GET /hx/next-scheduled-card."""

    def test_renders_html_when_not_paused(self, logged_in_client: Any) -> None:
        client, _, _ = logged_in_client
        resp = client.get("/hx/next-scheduled-card")
        assert resp.status_code == 200
        assert b"next-scheduled-card" in resp.data

    def test_renders_paused_state(self, logged_in_client: Any) -> None:
        client, store, _ = logged_in_client
        store.update(
            lambda s: dataclasses.replace(
                s, schedule=dataclasses.replace(s.schedule, paused=True)
            )
        )
        resp = client.get("/hx/next-scheduled-card")
        assert resp.status_code == 200
        assert b"paused" in resp.data.lower()


class TestRecentActivityCard:
    """tests for GET /hx/recent-activity-card."""

    def test_renders_html(self, logged_in_client: Any) -> None:
        client, _, destination = logged_in_client
        (destination / "20231015_120000_NF.mp4").write_text("x")
        resp = client.get("/hx/recent-activity-card")
        assert resp.status_code == 200
        assert b"recent-activity-card" in resp.data
        assert b"20231015_120000_NF.mp4" in resp.data


class TestDashcamInfoCard:
    """tests for GET /hx/dashcam-info-card."""

    def test_renders_available(self, logged_in_client: Any) -> None:
        from io import BytesIO

        client, _, _ = logged_in_client

        class _Ctx:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return BytesIO(b"[Tab1]\nResolution=4K\n")

            def __exit__(self, *a):  # type: ignore[no-untyped-def]
                return None

        def _fake_urlopen(req, timeout=0):  # type: ignore[no-untyped-def]  # noqa: ARG001
            url = req.full_url if hasattr(req, "full_url") else req
            if url.endswith("version.bin"):

                class _V:
                    def __enter__(self_inner):  # type: ignore[no-untyped-def]  # noqa: N805
                        return BytesIO(b"DR900X-2.013")

                    def __exit__(self_inner, *a):  # type: ignore[no-untyped-def]  # noqa: N805
                        return None

                return _V()
            return _Ctx()

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
            resp = client.get("/hx/dashcam-info-card")
        assert resp.status_code == 200
        assert "text/html" in resp.content_type
        assert b"dashcam-info-card" in resp.data
        assert b"DR900X-2.013" in resp.data
        assert b"Tab1.Resolution" in resp.data

    def test_renders_unavailable(self, logged_in_client: Any) -> None:
        import socket

        client, _, _ = logged_in_client
        with patch("urllib.request.urlopen", side_effect=socket.timeout("timed out")):
            resp = client.get("/hx/dashcam-info-card")
        assert resp.status_code == 200
        assert b"dashcam-info-card" in resp.data
        assert b"dashcam unreachable" in resp.data
