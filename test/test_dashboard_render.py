"""tests for the GET / dashboard page render (idle mode, server-side)."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2 import __version__
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


class TestDashboardRender:
    """tests for GET / rendering the real dashboard, not the placeholder."""

    def test_renders_dashboard_not_placeholder(self, logged_in_client: Any) -> None:
        client, _, _ = logged_in_client
        resp = client.get("/")
        assert resp.status_code == 200
        # the placeholder said "coming in sub-project #2"; the real one does not
        assert b"coming in sub-project" not in resp.data
        assert b"dashboard-grid" in resp.data
        assert b"dashboard-sidebar" in resp.data
        # footer renders the version, matching every other page
        assert __version__.encode() in resp.data

    def test_includes_all_six_card_ids(self, logged_in_client: Any) -> None:
        client, _, _ = logged_in_client
        resp = client.get("/")
        for card_id in (
            b"last-run-card",
            b"next-scheduled-card",
            b"storage-card",
            b"recent-activity-card",
            b"dashcam-card",
            b"dashcam-info-card",
        ):
            assert card_id in resp.data, f"missing {card_id!r}"

    def test_local_cards_are_server_rendered_populated(
        self, logged_in_client: Any
    ) -> None:
        """the four local cards render with real data on first paint (no JS),
        so the storage card shows a percentage and the recent card shows the
        seeded recording."""
        client, _, destination = logged_in_client
        (destination / "20231015_120000_NF.mp4").write_text("x")
        resp = client.get("/")
        # storage card SSR-populated: shows "% used"
        assert b"% used" in resp.data
        # recent-activity card SSR-populated: shows the seeded file
        assert b"20231015_120000_NF.mp4" in resp.data

    def test_network_cards_render_as_shells(self, logged_in_client: Any) -> None:
        """the two dashcam cards are shells on first paint (no network call in
        the route); they show 'checking' and self-load via htmx."""
        client, _, _ = logged_in_client
        resp = client.get("/")
        # both dashcam cards present and in the checking shell state
        assert resp.data.lower().count(b"checking") >= 2

    def test_does_no_network_io_on_render(self, logged_in_client: Any) -> None:
        """rendering the dashboard must not probe the dashcam (the cards do
        that asynchronously). if urlopen were called, this patch would raise."""
        client, _, _ = logged_in_client

        def _boom(*_a: Any, **_k: Any) -> None:
            raise AssertionError("dashboard render must not call urlopen")

        with patch("urllib.request.urlopen", side_effect=_boom):
            resp = client.get("/")
        assert resp.status_code == 200

    def test_redirects_to_login_when_unauthenticated(
        self, settings_path: Path, tmp_path: Path
    ) -> None:
        destination = tmp_path / "recordings"
        destination.mkdir()
        app, _ = _make_app(settings_path, destination)
        with app.test_client() as client:
            resp = client.get("/")
        assert resp.status_code == 302
