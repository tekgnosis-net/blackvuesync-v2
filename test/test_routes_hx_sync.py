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


def _row(**overrides: Any) -> Any:
    import time

    from blackvuesync_v2.server.stats_store import RunRow

    base: dict[str, Any] = {
        "ts_seconds": time.time() - 600,
        "success": 1,
        "exit_code": 0,
        "duration_seconds": 42.0,
        "files": 12,
        "bytes": 5_000_000,
        "recordings_seen": 100,
        "recordings_selected": 3,
        "disk_used_ratio": 0.5,
        "failed_markers": 0,
        "failures": {},
        "dry_run": 0,
    }
    base.update(overrides)
    return RunRow(**base)


class TestLastRunCardHistory:
    """the card falls back to the stats store once the publisher is idle."""

    def test_shows_latest_stored_run_when_idle(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        store = client.application.stats_store
        with patch.object(store, "latest", return_value=_row()):
            body = client.get("/hx/sync/last-run-card").data
        assert b"10 min ago" in body
        assert b"12 files" in body
        assert b"badge-complete" in body
        assert b"no completed sync recorded" not in body

    def test_labels_failed_and_dry_run_runs(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        store = client.application.stats_store
        row = _row(success=0, dry_run=1, failures={"http": 2})
        with patch.object(store, "latest", return_value=row):
            body = client.get("/hx/sync/last-run-card").data
        assert b"badge-failed" in body
        assert b"dry run" in body
        assert b"2 failed" in body

    def test_live_job_wins_over_stored_history(self, logged_in_client: Any) -> None:
        client, pub = logged_in_client
        pub.begin_job(2)
        store = client.application.stats_store
        with patch.object(store, "latest", return_value=_row()) as latest:
            body = client.get("/hx/sync/last-run-card").data
        assert b"badge-running" in body
        assert b"min ago" not in body
        latest.assert_not_called()


def test_ago_wording_boundaries() -> None:
    from blackvuesync_v2.server.routes.hx_sync import _ago

    assert _ago(59) == "just now"
    assert _ago(60) == "1 min ago"
    assert _ago(3599) == "59 min ago"
    assert _ago(3600) == "1 h ago"
    assert _ago(86399) == "23 h ago"
    assert _ago(86400) == "1 d ago"


class TestLastRunCardUnreachable:
    """a run that could not reach the dashcam reads as an expected state."""

    def test_network_or_timeout_failure_shows_not_reachable(
        self, logged_in_client: Any
    ) -> None:
        client, _ = logged_in_client
        store = client.application.stats_store
        # the shape stored runs really have: every reason, most of them zero
        zeros = {"network": 0, "timeout": 0, "http": 0, "disk": 0, "unknown": 0}
        for reason in ("network", "timeout"):
            row = _row(success=0, files=0, bytes=0, failures={**zeros, reason: 1})
            with patch.object(store, "latest", return_value=row):
                body = client.get("/hx/sync/last-run-card").data
            assert b"dashcam not reachable" in body
            assert b"badge-offline" in body
            assert b"badge-failed" not in body
            assert b"1 failed" not in body

    def test_other_failures_stay_failed(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        store = client.application.stats_store
        for failures in (
            {"disk": 1, "network": 0},
            {"network": 1, "disk": 1},
            {"network": 0, "timeout": 0},
        ):
            row = _row(success=0, failures=failures)
            with patch.object(store, "latest", return_value=row):
                body = client.get("/hx/sync/last-run-card").data
            assert b"badge-failed" in body
            assert b"not reachable" not in body
