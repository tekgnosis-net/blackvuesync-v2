"""structure tests for the phase 2c interactive dashboard (server-rendered)."""

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
def logged_in(settings_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    app, store = _make_app(settings_path, destination)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, app, store


class TestInitialState:
    def test_idle_data_state_when_no_sync(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        resp = client.get("/")
        assert resp.status_code == 200
        assert b'data-state="idle"' in resp.data

    def test_running_data_state_when_sync_active(self, logged_in: Any) -> None:
        client, app, _store = logged_in
        app.progress_publisher.begin_job(3)  # marks state running
        resp = client.get("/")
        assert b'data-state="running"' in resp.data

    def test_dashboard_js_loaded(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        resp = client.get("/")
        assert b"js/dashboard.js" in resp.data


class TestControls:
    def test_sync_now_and_stop_buttons_present(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        body = client.get("/").data
        assert b'data-action="sync-now"' in body
        assert b'data-action="stop"' in body

    def test_no_disabled_placeholder_controls(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        body = client.get("/").data
        assert b"Live controls arrive in the next update" not in body

    def test_pause_button_reflects_not_paused(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        body = client.get("/").data.decode()
        assert "Pause schedule" in body  # not paused -> offers Pause

    def test_pause_button_reflects_paused(self, logged_in: Any) -> None:
        client, _app, store = logged_in
        store.update(
            lambda s: dataclasses.replace(
                s, schedule=dataclasses.replace(s.schedule, paused=True)
            )
        )
        body = client.get("/").data.decode()
        assert "Resume schedule" in body  # paused -> offers Resume

    def test_active_hero_region_present(self, logged_in: Any) -> None:
        client, _app, _store = logged_in
        body = client.get("/").data
        assert b'id="active-hero"' in body
        assert b"active-only" in body  # hero + stop button carry the toggle class


class TestStopModal:
    def test_modal_markup_present(self, logged_in: Any) -> None:
        body = logged_in[0].get("/").data
        assert b'data-modal="stop-confirm"' in body
        assert b"Stop the running sync?" in body

    def test_modal_confirm_calls_dostop(self, logged_in: Any) -> None:
        body = logged_in[0].get("/").data
        # csp build requires bare method reference; no parens allowed.
        assert b'@click="doStop"' in body
