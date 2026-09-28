"""tests for /api/schedule/pause and /api/schedule/resume."""

from __future__ import annotations

import dataclasses
import json
import os
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
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _make_app(settings_path: Path):  # type: ignore[no-untyped-def]
    store = _make_store(settings_path)
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


class TestPause:
    """tests for POST /api/schedule/pause."""

    def test_pause_sets_paused_true(self, logged_in_client: Any) -> None:
        client, store = logged_in_client
        resp = client.post("/api/schedule/pause")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["paused"] is True
        assert store.get().schedule.paused is True

    def test_pause_is_idempotent(self, logged_in_client: Any) -> None:
        """calling pause twice keeps paused=True with no error."""
        client, store = logged_in_client
        client.post("/api/schedule/pause")
        resp = client.post("/api/schedule/pause")
        assert resp.status_code == 200
        assert store.get().schedule.paused is True

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        app, _ = _make_app(settings_path)
        with app.test_client() as client:
            resp = client.post("/api/schedule/pause")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"


class TestResume:
    """tests for POST /api/schedule/resume."""

    def test_resume_sets_paused_false(self, logged_in_client: Any) -> None:
        client, store = logged_in_client
        # first pause
        client.post("/api/schedule/pause")
        assert store.get().schedule.paused is True
        # then resume
        resp = client.post("/api/schedule/resume")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["paused"] is False
        assert store.get().schedule.paused is False

    def test_resume_is_idempotent(self, logged_in_client: Any) -> None:
        """resuming an already-running schedule returns 200 with no change."""
        client, store = logged_in_client
        resp = client.post("/api/schedule/resume")
        assert resp.status_code == 200
        assert store.get().schedule.paused is False


class TestCsrf:
    """tests that pause and resume require CSRF when enabled."""

    def _csrf_app(self, settings_path: Path):  # type: ignore[no-untyped-def]
        store = _make_store(settings_path)
        pw_hash = hash_password("test-password-1234")
        store.update(
            lambda s: dataclasses.replace(
                s,
                auth=dataclasses.replace(
                    s.auth, username="admin", password_hash=pw_hash
                ),
            )
        )
        app = create_app(store, testing=False)
        app.config["WTF_CSRF_ENABLED"] = True
        app.config["TESTING"] = True
        return app

    def test_pause_without_csrf_returns_400(self, settings_path: Path) -> None:
        app = self._csrf_app(settings_path)
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "admin"
                sess[SESSION_VERSION_KEY] = session_version(
                    app.settings_store.get().auth.password_hash
                )
            resp = client.post("/api/schedule/pause")
        assert resp.status_code == 400

    def test_resume_without_csrf_returns_400(self, settings_path: Path) -> None:
        app = self._csrf_app(settings_path)
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "admin"
                sess[SESSION_VERSION_KEY] = session_version(
                    app.settings_store.get().auth.password_hash
                )
            resp = client.post("/api/schedule/resume")
        assert resp.status_code == 400
