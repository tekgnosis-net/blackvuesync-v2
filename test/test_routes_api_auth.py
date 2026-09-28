"""tests for /api/auth/* endpoints: me, password change, session rotation."""

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
    verify_password,
)
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a settings file path inside tmp_path."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore with a dummy address."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _seed_admin(store: SettingsStore, password: str = "test-password-1234") -> None:
    """seeds the admin user with the given password."""
    pw_hash = hash_password(password)
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )


@pytest.fixture()
def logged_in_client(settings_path: Path):  # type: ignore[no-untyped-def]
    """returns a logged-in flask test client."""
    store = _make_store(settings_path)
    _seed_admin(store)
    app = create_app(store, testing=True)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, store


class TestAuthMe:
    """tests for GET /api/auth/me."""

    def test_returns_current_user_in_login_mode(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["username"] == "admin"
        assert body["mode"] == "login"

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        store = _make_store(settings_path)
        _seed_admin(store)
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.get("/api/auth/me")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"

    def test_returns_anonymous_in_none_mode(self, settings_path: Path) -> None:
        """when auth.mode is 'none', /api/auth/me reports the anonymous user.

        seeds the admin password first to bypass the Phase C sticky first-run
        redirect, which fires whenever password_hash is empty regardless of
        the configured auth mode.
        """
        store = _make_store(settings_path)
        _seed_admin(store)
        store.update(
            lambda s: dataclasses.replace(
                s, auth=dataclasses.replace(s.auth, mode="none")
            )
        )
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.get("/api/auth/me")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["username"] == "anonymous"
        assert body["mode"] == "none"

    def test_returns_proxy_header_user_in_proxy_mode(self, settings_path: Path) -> None:
        """when auth.mode is 'proxy', /api/auth/me returns the proxy-supplied user.

        seeds the admin password first to bypass the Phase C sticky first-run
        redirect; the password is unused in proxy mode.
        """
        store = _make_store(settings_path)
        _seed_admin(store)
        store.update(
            lambda s: dataclasses.replace(
                s,
                auth=dataclasses.replace(
                    s.auth,
                    mode="proxy",
                    trusted_proxies=("127.0.0.1",),
                    proxy_user_header="X-Remote-User",
                ),
            )
        )
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.get("/api/auth/me", headers={"X-Remote-User": "alice"})
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["username"] == "alice"
        assert body["mode"] == "proxy"


class TestChangePassword:
    """tests for POST /api/auth/password."""

    def test_changes_password_when_current_is_correct(
        self, logged_in_client: Any
    ) -> None:
        client, store = logged_in_client
        resp = client.post(
            "/api/auth/password",
            json={
                "current_password": "test-password-1234",
                "new_password": "new-strong-password-9876",
            },
        )
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["applied"] is True
        # the new hash verifies against the new password
        assert verify_password(
            store.get().auth.password_hash, "new-strong-password-9876"
        )

    def test_rejects_wrong_current_password(self, logged_in_client: Any) -> None:
        client, store = logged_in_client
        original_hash = store.get().auth.password_hash
        resp = client.post(
            "/api/auth/password",
            json={
                "current_password": "wrong-password",
                "new_password": "new-strong-password-9876",
            },
        )
        assert resp.status_code == 401
        body = json.loads(resp.data)
        assert body["code"] == "INVALID_CURRENT_PASSWORD"
        # hash unchanged
        assert store.get().auth.password_hash == original_hash

    def test_rejects_weak_new_password(self, logged_in_client: Any) -> None:
        client, store = logged_in_client
        original_hash = store.get().auth.password_hash
        resp = client.post(
            "/api/auth/password",
            json={
                "current_password": "test-password-1234",
                "new_password": "short",
            },
        )
        assert resp.status_code == 422
        body = json.loads(resp.data)
        assert body["code"] == "WEAK_PASSWORD"
        assert "field_errors" in body["details"]
        assert store.get().auth.password_hash == original_hash

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        store = _make_store(settings_path)
        _seed_admin(store)
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.post(
                "/api/auth/password",
                json={"current_password": "x", "new_password": "y"},
            )
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"

    def test_non_dict_body_returns_400(self, logged_in_client: Any) -> None:
        """a JSON array as body must return 400 INVALID_BODY, not 500."""
        client, _ = logged_in_client
        resp = client.post("/api/auth/password", json=[1, 2, 3])
        assert resp.status_code == 400
        body = json.loads(resp.data)
        assert body["code"] == "INVALID_BODY"


class TestRotateSessions:
    """tests for DELETE /api/auth/sessions."""

    def test_rotates_session_secret(self, logged_in_client: Any) -> None:
        client, store = logged_in_client
        original_secret = store.get().auth.session_secret
        resp = client.delete("/api/auth/sessions")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["rotated"] is True
        assert body["restart_required"] is False
        # the persisted secret changed
        assert store.get().auth.session_secret != original_secret
        assert len(store.get().auth.session_secret) >= 32

    def test_returns_401_when_unauthenticated(self, settings_path: Path) -> None:
        store = _make_store(settings_path)
        _seed_admin(store)
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.delete("/api/auth/sessions")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"


class TestCsrf:
    """tests that POST /api/auth/password and DELETE /api/auth/sessions
    require a CSRF token."""

    def _csrf_app(self, settings_path: Path):  # type: ignore[no-untyped-def]
        store = _make_store(settings_path)
        _seed_admin(store)
        app = create_app(store, testing=False)
        app.config["WTF_CSRF_ENABLED"] = True
        app.config["TESTING"] = True
        return app

    def test_password_post_without_csrf_returns_400(self, settings_path: Path) -> None:
        app = self._csrf_app(settings_path)
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "admin"
                sess[SESSION_VERSION_KEY] = session_version(
                    app.settings_store.get().auth.password_hash
                )
            resp = client.post(
                "/api/auth/password",
                json={"current_password": "x", "new_password": "y" * 20},
            )
        assert resp.status_code == 400

    def test_sessions_delete_without_csrf_returns_400(
        self, settings_path: Path
    ) -> None:
        app = self._csrf_app(settings_path)
        with app.test_client() as client:
            with client.session_transaction() as sess:
                sess["user"] = "admin"
                sess[SESSION_VERSION_KEY] = session_version(
                    app.settings_store.get().auth.password_hash
                )
            resp = client.delete("/api/auth/sessions")
        assert resp.status_code == 400
