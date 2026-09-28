"""tests for web-server hardening: proxy trust, session secret and revocation,
unauthenticated api responses, csrf lifetime, first-run race, log streams."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.middleware.proxy_fix import ProxyFix

from blackvuesync_v2.server import auth as auth_module
from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import (
    SESSION_VERSION_KEY,
    hash_password,
    is_trusted_proxy,
    session_version,
)
from blackvuesync_v2.server.log_buffer import BOOT_ID
from blackvuesync_v2.settings import SettingsStore

_PASSWORD = "test-password-1234"


def _clear_rate_limiter() -> None:
    """clears the module-level rate-limit tables."""
    auth_module._failure_timestamps.clear()  # pylint: disable=protected-access
    auth_module._locked_until.clear()  # pylint: disable=protected-access


def setup_function() -> None:
    """isolates the rate limiter per test."""
    _clear_rate_limiter()


def teardown_function() -> None:
    """leaves no rate-limit state behind."""
    _clear_rate_limiter()


def _make_store(tmp_path: Path, **auth_fields: Any) -> SettingsStore:
    """creates a store with an admin password and optional auth overrides."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password(_PASSWORD)
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, username="admin", password_hash=pw_hash, **auth_fields
            ),
        )
    )
    return store


def _login(app: Flask) -> FlaskClient:
    """returns a client logged in through the real /login form."""
    client = app.test_client()
    resp = client.post("/login", data={"username": "admin", "password": _PASSWORD})
    assert resp.status_code == 302
    return client


def _proxy_store(tmp_path: Path, trusted: tuple[str, ...]) -> SettingsStore:
    """creates a proxy-mode store trusting the given proxies."""
    return _make_store(
        tmp_path,
        mode="proxy",
        trusted_proxies=trusted,
        proxy_user_header="X-Remote-User",
    )


# ---------------------------------------------------------------------------
# proxy trust
# ---------------------------------------------------------------------------


def test_proxy_fix_disabled_without_trust_proxy_env(tmp_path: Path) -> None:
    """verifies X-Forwarded-* is ignored unless BLACKVUESYNC_TRUST_PROXY is set."""
    with patch.dict(os.environ, {"BLACKVUESYNC_TRUST_PROXY": ""}, clear=False):
        app = create_app(_make_store(tmp_path), testing=True)
    assert not isinstance(app.wsgi_app, ProxyFix)


def test_proxy_fix_enabled_with_trust_proxy_env(tmp_path: Path) -> None:
    """verifies ProxyFix wraps the app when BLACKVUESYNC_TRUST_PROXY=1."""
    with patch.dict(os.environ, {"BLACKVUESYNC_TRUST_PROXY": "1"}, clear=False):
        app = create_app(_make_store(tmp_path), testing=True)
    assert isinstance(app.wsgi_app, ProxyFix)


def test_proxy_mode_rejects_spoofed_forwarded_for(tmp_path: Path) -> None:
    """verifies a direct client cannot claim a trusted address via X-Forwarded-For."""
    store = _proxy_store(tmp_path, ("10.0.0.5",))
    for trust_env in ("", "1"):
        env = {"BLACKVUESYNC_TRUST_PROXY": trust_env}
        with patch.dict(os.environ, env, clear=False):
            app = create_app(store, testing=True)
        resp = app.test_client().get(
            "/api/auth/me",
            environ_base={"REMOTE_ADDR": "203.0.113.9"},
            headers={"X-Forwarded-For": "10.0.0.5", "X-Remote-User": "admin"},
        )
        assert resp.status_code == 401, trust_env


def test_proxy_mode_trusts_socket_peer_behind_proxy_fix(tmp_path: Path) -> None:
    """verifies a real trusted proxy is accepted even though ProxyFix rewrites
    remote_addr to the forwarded client address."""
    store = _proxy_store(tmp_path, ("10.0.0.5",))
    with patch.dict(os.environ, {"BLACKVUESYNC_TRUST_PROXY": "1"}, clear=False):
        app = create_app(store, testing=True)
    resp = app.test_client().get(
        "/api/auth/me",
        environ_base={"REMOTE_ADDR": "10.0.0.5"},
        headers={"X-Forwarded-For": "198.51.100.7", "X-Remote-User": "carol"},
    )
    assert resp.status_code == 200
    assert resp.get_json()["username"] == "carol"


def test_proxy_mode_accepts_cidr_entries(tmp_path: Path) -> None:
    """verifies trusted_proxies entries may be CIDR networks."""
    store = _proxy_store(tmp_path, ("172.16.0.0/12",))
    app = create_app(store, testing=True)
    resp = app.test_client().get(
        "/api/auth/me",
        environ_base={"REMOTE_ADDR": "172.20.1.2"},
        headers={"X-Remote-User": "dave"},
    )
    assert resp.status_code == 200


_TRUST_CASES: list[tuple[str, Any, bool]] = [
    ("10.0.0.1", ("10.0.0.1",), True),
    ("10.0.0.1", (" 10.0.0.0/24 ",), True),
    ("10.0.1.1", ("10.0.0.0/24",), False),
    ("::1", ("::1",), True),
    ("::1", ("127.0.0.1",), False),
    ("::ffff:10.0.0.1", ("10.0.0.0/8",), True),
    ("10.0.0.1", ("not-an-ip", "10.0.0.1"), True),
    ("10.0.0.1", "10.0.0.1", False),  # a bare string is not a collection
    ("10.0.0.1", None, False),
    ("", ("10.0.0.1",), False),
    ("garbage", ("10.0.0.1",), False),
]


def test_is_trusted_proxy() -> None:
    """verifies ip/cidr matching and defensive handling of bad input."""
    for address, trusted, expected in _TRUST_CASES:
        assert is_trusted_proxy(address, trusted) is expected, (address, trusted)


# ---------------------------------------------------------------------------
# rate limiter memory bound
# ---------------------------------------------------------------------------


def test_rate_limiter_prunes_stale_and_caps_entries() -> None:
    """verifies the failure table stays bounded under many distinct ips."""
    clock = [1000.0]
    with (
        patch.object(auth_module, "_MAX_TRACKED_IPS", 5),
        patch("blackvuesync_v2.server.auth.time.monotonic", lambda: clock[0]),
    ):
        for i in range(20):
            auth_module.record_login_failure(f"10.0.0.{i}")
        assert len(auth_module._failure_timestamps) <= 5  # noqa: SLF001
        # entries outside the window are dropped on the next insertion
        clock[0] += auth_module._FAILURE_WINDOW_SECONDS + 1  # noqa: SLF001
        for i in range(5):
            auth_module.record_login_failure(f"10.1.0.{i}")
        assert all(
            k.startswith("10.1.")
            for k in auth_module._failure_timestamps  # noqa: SLF001
        )


def test_rate_limiter_trims_old_timestamps_per_ip() -> None:
    """verifies one ip's deque only keeps timestamps inside the window."""
    clock = [1000.0]
    with patch("blackvuesync_v2.server.auth.time.monotonic", lambda: clock[0]):
        for _ in range(5):
            auth_module.record_login_failure("10.0.0.1")
        clock[0] += auth_module._FAILURE_WINDOW_SECONDS + 1  # noqa: SLF001
        auth_module.record_login_failure("10.0.0.1")
        assert len(auth_module._failure_timestamps["10.0.0.1"]) == 1  # noqa: SLF001


# ---------------------------------------------------------------------------
# session secret
# ---------------------------------------------------------------------------


def test_empty_session_secret_uses_random_key(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """verifies an empty secret never falls back to a public constant."""
    store = _make_store(tmp_path)
    blank = dataclasses.replace(
        store.get(), auth=dataclasses.replace(store.get().auth, session_secret="")
    )
    with (
        patch.object(store, "get", return_value=blank),
        caplog.at_level(logging.WARNING),
    ):
        first = create_app(store)
        second = create_app(store)
    first_key = first.secret_key
    assert isinstance(first_key, bytes)
    assert first_key != second.secret_key
    assert b"placeholder" not in first_key
    assert len(first_key) >= 32
    assert "session_secret is empty" in caplog.text


def test_rotating_sessions_signs_everyone_out(tmp_path: Path) -> None:
    """verifies DELETE /api/auth/sessions invalidates cookies without restart."""
    store = _make_store(tmp_path)
    app = create_app(store, testing=True)
    alice = _login(app)
    bob = _login(app)
    assert bob.get("/api/auth/me").status_code == 200

    resp = alice.delete("/api/auth/sessions")
    assert resp.status_code == 200
    assert resp.get_json()["restart_required"] is False
    assert app.secret_key == store.get().auth.session_secret.encode()
    assert bob.get("/api/auth/me").status_code == 401
    assert alice.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# password change revocation
# ---------------------------------------------------------------------------


def test_password_change_revokes_other_sessions(tmp_path: Path) -> None:
    """verifies other sessions end and the changing session survives."""
    store = _make_store(tmp_path)
    app = create_app(store, testing=True)
    alice = _login(app)
    bob = _login(app)

    resp = alice.post(
        "/api/auth/password",
        json={"current_password": _PASSWORD, "new_password": "brand-new-pass-9876"},
    )
    assert resp.status_code == 200
    assert alice.get("/api/auth/me").status_code == 200
    assert bob.get("/api/auth/me").status_code == 401


def test_session_without_version_is_rejected(tmp_path: Path) -> None:
    """verifies a session lacking the password version (pre-upgrade) is rejected."""
    app = create_app(_make_store(tmp_path), testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
    assert client.get("/api/auth/me").status_code == 401
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(
            app.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
        )
    assert client.get("/api/auth/me").status_code == 200


def test_password_change_rejects_non_string_fields(tmp_path: Path) -> None:
    """verifies non-string password fields return 422 instead of 500."""
    app = create_app(_make_store(tmp_path), testing=True)
    client = _login(app)
    payloads: list[dict[str, Any]] = [
        {"current_password": 123, "new_password": "brand-new-pass-9876"},
        {"current_password": _PASSWORD, "new_password": ["a", "b"]},
        {"current_password": None, "new_password": None},
    ]
    for payload in payloads:
        resp = client.post("/api/auth/password", json=payload)
        assert resp.status_code == 422, payload
        body = resp.get_json()
        assert body["code"] == "VALIDATION_ERROR"
        assert body["details"]["field_errors"]


# ---------------------------------------------------------------------------
# csrf lifetime and unauthenticated responses
# ---------------------------------------------------------------------------


def test_csrf_tokens_do_not_expire(tmp_path: Path) -> None:
    """verifies long-open pages keep a valid csrf token."""
    app = create_app(_make_store(tmp_path))
    assert app.config["WTF_CSRF_TIME_LIMIT"] is None


def test_unauthenticated_api_returns_401_json(tmp_path: Path) -> None:
    """verifies api paths answer 401 json instead of redirecting to html."""
    app = create_app(_make_store(tmp_path), testing=True)
    resp = app.test_client().get("/api/sync/progress")
    assert resp.status_code == 401
    assert resp.get_json() == {
        "error": "authentication required",
        "code": "AUTH_REQUIRED",
        "details": {},
    }


def test_unauthenticated_htmx_returns_hx_redirect(tmp_path: Path) -> None:
    """verifies htmx requests get 401 plus HX-Redirect to the login page."""
    app = create_app(_make_store(tmp_path), testing=True)
    resp = app.test_client().get("/hx/sync/status-card", headers={"HX-Request": "true"})
    assert resp.status_code == 401
    assert resp.headers["HX-Redirect"].startswith("/login?next=")


def test_unauthenticated_page_still_redirects(tmp_path: Path) -> None:
    """verifies page routes keep the 302 to /login."""
    app = create_app(_make_store(tmp_path), testing=True)
    resp = app.test_client().get("/settings")
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]


# ---------------------------------------------------------------------------
# first-run race
# ---------------------------------------------------------------------------


def test_first_run_does_not_overwrite_concurrent_winner(tmp_path: Path) -> None:
    """verifies a first-run post losing the race keeps the winner's password."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    app = create_app(store, testing=True)
    winner_hash = hash_password("winner-password-1234")
    real_hash = auth_module.hash_password

    def _racing_hash(plaintext: str) -> str:
        """simulates a concurrent first-run completing during hashing."""
        store.update(
            lambda s: dataclasses.replace(
                s,
                auth=dataclasses.replace(
                    s.auth, username="winner", password_hash=winner_hash
                ),
            )
        )
        return real_hash(plaintext)

    with patch("blackvuesync_v2.server.routes.auth.hash_password", _racing_hash):
        resp = app.test_client().post(
            "/first-run",
            data={
                "username": "loser",
                "password": "loser-password-1234",
                "confirm": "loser-password-1234",
            },
        )
    assert resp.status_code == 302
    assert "/login" in resp.headers["Location"]
    assert store.get().auth.password_hash == winner_hash
    assert store.get().auth.username == "winner"


# ---------------------------------------------------------------------------
# log stream subscriber cleanup and boot id
# ---------------------------------------------------------------------------


def test_logs_stream_disconnect_during_snapshot_unregisters(tmp_path: Path) -> None:
    """verifies closing the stream right after the snapshot frame drops the
    subscriber queue (it used to leak until process exit)."""
    app = create_app(_make_store(tmp_path, mode="none"), testing=True)
    buf = app.log_buffer  # type: ignore[attr-defined]
    buf.handle(logging.makeLogRecord({"msg": "hello", "levelno": 20}))
    resp = app.test_client().get("/api/logs/stream", buffered=False)
    first = next(iter(resp.response))
    assert b"hello" in first
    assert len(buf._subscribers) == 1  # pylint: disable=protected-access
    resp.close()
    assert len(buf._subscribers) == 0  # pylint: disable=protected-access


def test_logs_payloads_carry_boot_id(tmp_path: Path) -> None:
    """verifies the recent snapshot, stream frames and page expose the boot id."""
    app = create_app(_make_store(tmp_path, mode="none"), testing=True)
    buf = app.log_buffer  # type: ignore[attr-defined]
    buf.handle(logging.makeLogRecord({"msg": "hello", "levelno": 20}))
    client = app.test_client()
    assert client.get("/api/logs/recent").get_json()["boot_id"] == BOOT_ID

    resp = client.get("/api/logs/stream", buffered=False)
    chunk = next(iter(resp.response))
    assert isinstance(chunk, bytes)
    frame = chunk.decode()
    resp.close()
    payload = json.loads(frame.split("data: ", 1)[1])
    assert payload["boot_id"] == BOOT_ID

    page = client.get("/logs")
    assert f'data-boot-id="{BOOT_ID}"'.encode() in page.data
