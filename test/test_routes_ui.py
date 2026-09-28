"""tests for placeholder ui routes: /, /settings, /logs, /stats, /viewer."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from blackvuesync_v2 import __version__
from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import (
    SESSION_VERSION_KEY,
    hash_password,
    session_version,
)
from blackvuesync_v2.settings import SettingsStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a settings file path inside tmp_path."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore at settings_path with a dummy address for validation."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


@pytest.fixture()
def app_with_password(settings_path: Path) -> Flask:
    """returns an app with a pre-set password (login mode)."""
    store = _make_store(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )
    return create_app(store, testing=True)


@pytest.fixture()
def logged_in_client(app_with_password: Flask) -> FlaskClient:
    """returns a test client with a valid session."""
    c = app_with_password.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(
            app_with_password.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
        )
    return c


@pytest.fixture()
def anonymous_client(app_with_password: Flask) -> FlaskClient:
    """returns a test client with no session."""
    return app_with_password.test_client()


# ---------------------------------------------------------------------------
# authenticated access (returns 200)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/settings", "/logs", "/stats", "/viewer"])
def test_authenticated_access_returns_200(
    logged_in_client: FlaskClient, path: str
) -> None:
    """verifies each protected route returns 200 when the user is logged in."""
    r = logged_in_client.get(path)
    assert r.status_code == 200


@pytest.mark.parametrize("path", ["/", "/settings", "/logs", "/stats", "/viewer"])
def test_authenticated_access_contains_nav(
    logged_in_client: FlaskClient, path: str
) -> None:
    """verifies protected pages include the navigation bar."""
    r = logged_in_client.get(path)
    assert b"nav-link" in r.data or b"nav" in r.data.lower()


# ---------------------------------------------------------------------------
# unauthenticated access (redirects to /login)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/settings", "/logs", "/stats", "/viewer"])
def test_unauthenticated_access_redirects_to_login(
    anonymous_client: FlaskClient, path: str
) -> None:
    """verifies each protected route redirects to /login when not logged in."""
    r = anonymous_client.get(path)
    assert r.status_code == 302
    location = r.headers["Location"]
    assert "/login" in location or "/first-run" in location


# ---------------------------------------------------------------------------
# mode=none bypasses login for ui routes
# ---------------------------------------------------------------------------


def test_ui_routes_accessible_without_login_in_none_mode(
    settings_path: Path,
) -> None:
    """verifies all UI routes return 200 in auth.mode=none without a session."""
    store = _make_store(settings_path)
    pw_hash = hash_password("some-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth,
                mode="none",
                password_hash=pw_hash,
            ),
        )
    )
    app = create_app(store, testing=True)
    with app.test_client() as c:
        for path in ["/", "/settings", "/logs", "/stats", "/viewer"]:
            r = c.get(path)
            assert r.status_code == 200, f"expected 200 for {path}, got {r.status_code}"


# ---------------------------------------------------------------------------
# release version next to the title
# ---------------------------------------------------------------------------


def test_every_page_shows_the_version_next_to_the_title(
    logged_in_client: FlaskClient,
) -> None:
    label = f'<span class="site-version">ver: {__version__}</span>'.encode()
    for path in ["/", "/settings", "/logs", "/stats", "/viewer"]:
        body = logged_in_client.get(path).data
        assert label in body, path
        assert f"BlackVue Sync v2 {__version__}</span>".encode() in body, path


def test_login_page_does_not_disclose_the_version(
    anonymous_client: FlaskClient,
) -> None:
    body = anonymous_client.get("/login").data
    assert b"site-version" not in body
    assert __version__.encode() not in body


def test_version_is_shown_in_none_auth_mode(settings_path: Path) -> None:
    store = _make_store(settings_path)
    pw_hash = hash_password("some-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, mode="none", password_hash=pw_hash)
        )
    )
    with create_app(store, testing=True).test_client() as c:
        assert f"ver: {__version__}".encode() in c.get("/").data


# ---------------------------------------------------------------------------
# header nav and sign-out per auth mode
# ---------------------------------------------------------------------------

_PAGES = ["/", "/settings", "/logs", "/stats", "/viewer"]
_NAV = [b'href="/settings"', b'href="/logs"', b'href="/stats"', b'href="/viewer"']


def _mode_app(settings_path: Path, **auth: Any) -> Flask:
    store = _make_store(settings_path)
    pw_hash = hash_password("some-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=pw_hash, **auth)
        )
    )
    return create_app(store, testing=True)


def test_none_mode_shows_the_nav_without_sign_out(settings_path: Path) -> None:
    with _mode_app(settings_path, mode="none").test_client() as c:
        for path in _PAGES:
            body = c.get(path).data
            assert all(link in body for link in _NAV), path
            assert b"Sign out" not in body, path


def test_proxy_mode_shows_the_nav_without_sign_out(settings_path: Path) -> None:
    app = _mode_app(
        settings_path,
        mode="proxy",
        trusted_proxies=("127.0.0.1",),
        proxy_user_header="X-Remote-User",
    )
    with app.test_client() as c:
        resp = c.get(
            "/",
            headers={"X-Remote-User": "kumar"},
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert resp.status_code == 200
        assert all(link in resp.data for link in _NAV)
        assert b"Sign out" not in resp.data  # the proxy owns the session


def test_login_mode_shows_the_nav_and_sign_out(logged_in_client: FlaskClient) -> None:
    for path in _PAGES:
        body = logged_in_client.get(path).data
        assert all(link in body for link in _NAV), path
        assert b"Sign out" in body, path


def test_login_page_has_no_nav(anonymous_client: FlaskClient) -> None:
    body = anonymous_client.get("/login").data
    assert b'class="site-nav"' not in body
    assert b"Sign out" not in body


# ---------------------------------------------------------------------------
# placeholder content spot-checks
# ---------------------------------------------------------------------------


def test_dashboard_renders_real_grid(logged_in_client: FlaskClient) -> None:
    """verifies the dashboard renders the real card grid, not the placeholder."""
    r = logged_in_client.get("/")
    assert b"dashboard-grid" in r.data


def test_dashboard_not_placeholder(logged_in_client: FlaskClient) -> None:
    """verifies the dashboard no longer shows the sub-project #2 placeholder."""
    r = logged_in_client.get("/")
    assert b"sub-project" not in r.data.lower()
    assert b"coming" not in r.data.lower()
