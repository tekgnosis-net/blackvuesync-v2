"""tests for /hx/sync/* htmx fragment endpoints."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.settings import SettingsStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a settings file path inside tmp_path."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore with a dummy address for validation."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _make_app(
    settings_path: Path,
    publisher: ProgressPublisher | None = None,
) -> tuple[Any, ProgressPublisher]:
    """creates a test app with a pre-set password."""
    store = _make_store(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )
    pub = publisher or ProgressPublisher()
    return create_app(store, testing=True, progress_publisher=pub), pub


@pytest.fixture()
def app_and_pub(settings_path: Path) -> tuple[Any, ProgressPublisher]:
    """returns (app, publisher) pair in testing mode."""
    return _make_app(settings_path)


@pytest.fixture()
def logged_in_client(app_and_pub: tuple[Any, ProgressPublisher]):  # type: ignore[no-untyped-def]
    """returns (logged-in test client, publisher)."""
    app, pub = app_and_pub
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, pub


# ---------------------------------------------------------------------------
# /hx/sync/status-card
# ---------------------------------------------------------------------------


class TestStatusCard:
    """tests for GET /hx/sync/status-card."""

    def test_returns_200_with_html_when_authenticated(
        self, logged_in_client: Any
    ) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/status-card")
        assert resp.status_code == 200
        assert b"sync-status-card" in resp.data

    def test_shows_idle_state(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/status-card")
        assert resp.status_code == 200
        assert b"idle" in resp.data

    def test_shows_running_state(self, logged_in_client: Any) -> None:
        client, pub = logged_in_client
        pub.begin_job(3)
        resp = client.get("/hx/sync/status-card")
        assert resp.status_code == 200
        assert b"running" in resp.data

    def test_redirects_to_login_when_not_authenticated(
        self, settings_path: Path
    ) -> None:
        app, _ = _make_app(settings_path)
        with app.test_client() as client:
            resp = client.get("/hx/sync/status-card")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_content_type_is_html(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/status-card")
        assert "text/html" in resp.content_type


# ---------------------------------------------------------------------------
# /hx/sync/last-run-card
# ---------------------------------------------------------------------------


class TestLastRunCard:
    """tests for GET /hx/sync/last-run-card."""

    def test_returns_200_with_html_when_authenticated(
        self, logged_in_client: Any
    ) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/last-run-card")
        assert resp.status_code == 200
        assert b"last-run-card" in resp.data
        assert b"card-label" in resp.data

    def test_self_polls_via_hx_trigger(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/last-run-card")
        assert resp.status_code == 200
        assert b"hx-trigger" in resp.data

    def test_shows_no_completed_sync_message_initially(
        self, logged_in_client: Any
    ) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/last-run-card")
        assert resp.status_code == 200
        assert b"no completed sync recorded" in resp.data

    def test_shows_complete_state_after_sync(self, logged_in_client: Any) -> None:
        client, pub = logged_in_client
        pub.begin_job(2)
        pub.end_job(success=True)
        resp = client.get("/hx/sync/last-run-card")
        assert resp.status_code == 200
        assert b"complete" in resp.data
        assert b"badge-" in resp.data

    def test_redirects_to_login_when_not_authenticated(
        self, settings_path: Path
    ) -> None:
        app, _ = _make_app(settings_path)
        with app.test_client() as client:
            resp = client.get("/hx/sync/last-run-card")
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]

    def test_content_type_is_html(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/hx/sync/last-run-card")
        assert "text/html" in resp.content_type
