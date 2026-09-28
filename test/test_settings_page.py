"""structure tests for the settings page (server-rendered)."""

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
def logged_in(tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth,
                username="admin",
                password_hash=hash_password("test-password-1234"),
            ),
            system=dataclasses.replace(s.system, destination=str(destination)),
        )
    )
    app = create_app(store, testing=True)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, store


class TestSettingsPage:
    def test_renders_not_placeholder(self, logged_in: Any) -> None:
        client, _ = logged_in
        body = client.get("/settings").data
        assert b"coming in sub-project" not in body

    def test_all_nine_sections_present(self, logged_in: Any) -> None:
        client, _ = logged_in
        body = client.get("/settings").data.decode()
        for name in (
            "connection",
            "schedule",
            "sync",
            "retention",
            "logging",
            "metrics",
            "web",
            "auth",
            "system",
        ):
            assert f'data-section="{name}"' in body

    def test_settings_js_and_css_loaded(self, logged_in: Any) -> None:
        client, _ = logged_in
        body = client.get("/settings").data
        assert b"js/settings.js" in body
        assert b"css/settings.css" in body


class TestFieldWidgets:
    def test_select_renders_options(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        # sync.priority is a select with the three literal options
        assert 'data-field="priority"' in body
        assert "<select" in body and ">date<" in body and ">rdate<" in body

    def test_number_field_has_data_type(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert 'data-field="timeout_seconds"' in body
        assert 'data-type="number"' in body

    def test_toggle_field_is_checkbox(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert 'data-field="dry_run"' in body
        assert 'data-type="bool"' in body

    def test_lines_field_is_textarea(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert 'data-field="include"' in body
        assert 'data-type="lines"' in body
        assert "<textarea" in body


class TestAuthControls:
    def test_secrets_shown_as_set_not_hash(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert "Password: set" in body  # password_hash was set in the fixture
        assert "***" not in body  # never leak the redaction sentinel
        # the bcrypt/argon hash must never appear
        assert "$argon2" not in body

    def test_change_password_and_rotate_controls(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert 'data-action="change-password"' in body
        assert 'data-action="rotate-sessions"' in body

    def test_password_dialog_present(self, logged_in: Any) -> None:
        body = logged_in[0].get("/settings").data.decode()
        assert 'data-dialog="password"' in body
