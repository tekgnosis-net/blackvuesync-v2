"""tests for authentication routes: /login, /logout, /first-run."""

from __future__ import annotations

import dataclasses
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import (
    _failure_timestamps,
    _locked_until,
    hash_password,
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
def app_no_password(settings_path: Path) -> Flask:
    """returns an app with no password set (first-run state)."""
    store = _make_store(settings_path)
    return create_app(store, testing=True)


@pytest.fixture()
def app_with_password(settings_path: Path) -> Flask:
    """returns an app with a pre-set password."""
    store = _make_store(settings_path)
    pw_hash = hash_password("correct-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )
    return create_app(store, testing=True)


@pytest.fixture(autouse=True)
def clear_rate_limit_state() -> None:
    """clears in-memory rate-limit state before each test."""
    _failure_timestamps.clear()
    _locked_until.clear()


# ---------------------------------------------------------------------------
# first-run redirect (before_app_request)
# ---------------------------------------------------------------------------


def test_get_root_redirects_to_first_run_when_no_password(
    app_no_password: Flask,
) -> None:
    """verifies GET / redirects to /first-run when password_hash is empty."""
    with app_no_password.test_client() as c:
        r = c.get("/")
    assert r.status_code == 302
    assert "/first-run" in r.headers["Location"]


def test_healthz_not_redirected_to_first_run(app_no_password: Flask) -> None:
    """verifies /healthz is exempt from the first-run redirect."""
    with app_no_password.test_client() as c:
        r = c.get("/healthz")
    assert r.status_code == 200


def test_readyz_not_redirected_to_first_run(app_no_password: Flask) -> None:
    """verifies /readyz is exempt from the first-run redirect."""
    with app_no_password.test_client() as c:
        r = c.get("/readyz")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# GET /first-run
# ---------------------------------------------------------------------------


def test_get_first_run_shows_form_when_no_password(app_no_password: Flask) -> None:
    """verifies GET /first-run renders the setup form."""
    with app_no_password.test_client() as c:
        r = c.get("/first-run")
    assert r.status_code == 200
    assert b"Set up" in r.data or b"first" in r.data.lower()


def test_get_first_run_redirects_to_login_when_password_set(
    app_with_password: Flask,
) -> None:
    """verifies GET /first-run redirects to /login when password is already set."""
    with app_with_password.test_client() as c:
        r = c.get("/first-run")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


# ---------------------------------------------------------------------------
# POST /first-run
# ---------------------------------------------------------------------------


def test_post_first_run_valid_data_updates_store_and_redirects(
    app_no_password: Flask,
) -> None:
    """verifies POST /first-run with valid data updates the store and redirects to /login."""
    with app_no_password.test_client() as c:
        r = c.post(
            "/first-run",
            data={
                "username": "admin",
                "password": "valid-password-123",
                "confirm": "valid-password-123",
            },
        )
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]
    # verify store was updated
    settings = app_no_password.settings_store.get()  # type: ignore[attr-defined]
    assert settings.auth.password_hash != ""
    assert settings.auth.username == "admin"


def test_post_first_run_short_password_returns_400(app_no_password: Flask) -> None:
    """verifies POST /first-run with a too-short password returns 400."""
    with app_no_password.test_client() as c:
        r = c.post(
            "/first-run",
            data={"username": "admin", "password": "short", "confirm": "short"},
        )
    assert r.status_code == 400
    assert b"12" in r.data  # error mentions minimum length


def test_post_first_run_mismatched_confirm_returns_400(app_no_password: Flask) -> None:
    """verifies POST /first-run with mismatched confirm password returns 400."""
    with app_no_password.test_client() as c:
        r = c.post(
            "/first-run",
            data={
                "username": "admin",
                "password": "valid-password-123",
                "confirm": "different-password",
            },
        )
    assert r.status_code == 400
    assert b"match" in r.data.lower()


# ---------------------------------------------------------------------------
# GET /login
# ---------------------------------------------------------------------------


def test_get_login_renders_form(app_with_password: Flask) -> None:
    """verifies GET /login renders the login form."""
    with app_with_password.test_client() as c:
        r = c.get("/login")
    assert r.status_code == 200
    assert b"Sign in" in r.data or b"login" in r.data.lower()


# ---------------------------------------------------------------------------
# POST /login
# ---------------------------------------------------------------------------


def test_post_login_valid_creds_sets_session(app_with_password: Flask) -> None:
    """verifies POST /login with correct credentials sets the session and redirects."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={"username": "admin", "password": "correct-password-123"},
        )
    assert r.status_code == 302


def test_post_login_wrong_password_returns_401(app_with_password: Flask) -> None:
    """verifies POST /login with wrong password returns 401."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={"username": "admin", "password": "wrong-password"},
        )
    assert r.status_code == 401


def test_post_login_rate_limit_triggers_after_10_failures(
    app_with_password: Flask,
) -> None:
    """verifies the 11th failed login attempt returns 429 after 10 prior failures."""
    with app_with_password.test_client() as c:
        # 10 failed attempts to reach lockout threshold
        for _ in range(10):
            c.post(
                "/login",
                data={"username": "admin", "password": "wrong"},
            )
        # 11th attempt should be locked out (threshold reached)
        r = c.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
        )
    assert r.status_code == 429


def test_post_login_uniform_timing_on_failure(app_with_password: Flask) -> None:
    """verifies failed logins pad response time regardless of failure reason."""
    cases = [
        ("admin", "wrong-password"),  # right user, wrong password
        ("wrong-user", "wrong-password"),  # wrong user, wrong password
    ]
    durations = []
    with app_with_password.test_client() as c:
        for username, password in cases:
            t0 = time.perf_counter()
            c.post("/login", data={"username": username, "password": password})
            durations.append(time.perf_counter() - t0)

    # both should take at least ~1.4s (padded to 1.5s minus some tolerance)
    # and differ by less than 0.5s
    for d in durations:
        assert d >= 1.2, f"response too fast: {d:.2f}s"
    diff = abs(durations[0] - durations[1])
    assert diff < 0.5, f"timing difference too large: {durations}"


# ---------------------------------------------------------------------------
# POST /logout
# ---------------------------------------------------------------------------


def test_post_logout_clears_session(app_with_password: Flask) -> None:
    """verifies POST /logout clears the session and redirects to /login."""
    with app_with_password.test_client() as c:
        # log in first
        c.post(
            "/login",
            data={"username": "admin", "password": "correct-password-123"},
        )
        # now log out
        r = c.post("/logout")
    assert r.status_code == 302
    assert "/login" in r.headers["Location"]


def test_get_root_after_logout_redirects_to_login(app_with_password: Flask) -> None:
    """verifies visiting / after logout redirects to /login."""
    with app_with_password.test_client() as c:
        c.post("/login", data={"username": "admin", "password": "correct-password-123"})
        c.post("/logout")
        r = c.get("/", follow_redirects=False)
    # after logout session is gone; / should redirect to /login
    assert r.status_code == 302
    location = r.headers["Location"]
    assert "/login" in location or "/first-run" in location


# ---------------------------------------------------------------------------
# open-redirect validation for ?next= parameter
# ---------------------------------------------------------------------------


def test_post_login_next_protocol_relative_redirects_to_root(
    app_with_password: Flask,
) -> None:
    """verifies //evil.com is rejected: redirect goes to / not the attacker site."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-password-123",
                "next": "//evil.com",
            },
        )
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_post_login_next_absolute_http_redirects_to_root(
    app_with_password: Flask,
) -> None:
    """verifies http://evil.com is rejected: redirect goes to /."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-password-123",
                "next": "http://evil.com",
            },
        )
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_post_login_next_javascript_scheme_redirects_to_root(
    app_with_password: Flask,
) -> None:
    """verifies javascript:alert(1) is rejected: redirect goes to /."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-password-123",
                "next": "javascript:alert(1)",
            },
        )
    assert r.status_code == 302
    assert r.headers["Location"] == "/"


def test_post_login_next_legit_path_is_honored(
    app_with_password: Flask,
) -> None:
    """verifies a relative path like /settings is honored after login."""
    with app_with_password.test_client() as c:
        r = c.post(
            "/login",
            data={
                "username": "admin",
                "password": "correct-password-123",
                "next": "/settings",
            },
        )
    assert r.status_code == 302
    assert r.headers["Location"] == "/settings"


# ---------------------------------------------------------------------------
# CSRF rejection (WTF_CSRF_ENABLED=True)
# ---------------------------------------------------------------------------


def test_post_login_without_csrf_token_returns_400(settings_path: Path) -> None:
    """verifies POST /login without a CSRF token returns 400 when CSRF is enabled."""
    store = _make_store(settings_path)
    pw_hash = hash_password("correct-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
        )
    )
    # create app with CSRF enabled
    app = create_app(store, testing=False)
    app.config["WTF_CSRF_ENABLED"] = True
    app.config["TESTING"] = True
    with app.test_client() as c:
        # POST without csrf_token field
        r = c.post(
            "/login",
            data={"username": "admin", "password": "correct-password-123"},
        )
    assert r.status_code == 400
