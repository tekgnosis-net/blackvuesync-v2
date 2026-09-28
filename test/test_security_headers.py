"""tests for security headers middleware."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from flask.testing import FlaskClient

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a settings file path inside tmp_path."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore at settings_path with a dummy address."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


@pytest.fixture()
def client(settings_path: Path) -> FlaskClient:
    """returns a test client for an app with a set password."""
    store = _make_store(settings_path)
    pw_hash = hash_password("secure-password-123")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, password_hash=pw_hash),
        )
    )
    app = create_app(store, testing=True)
    return app.test_client()


# ---------------------------------------------------------------------------
# content-security-policy
# ---------------------------------------------------------------------------


def test_csp_header_present(client: FlaskClient) -> None:
    """verifies every response includes a Content-Security-Policy header."""
    r = client.get("/healthz")
    assert "Content-Security-Policy" in r.headers


def test_csp_allows_self(client: FlaskClient) -> None:
    """verifies the CSP includes default-src 'self'."""
    r = client.get("/healthz")
    csp = r.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp


def test_csp_includes_openstreetmap_for_tiles(client: FlaskClient) -> None:
    """verifies the CSP img-src allowance includes openstreetmap tile domain."""
    r = client.get("/healthz")
    csp = r.headers["Content-Security-Policy"]
    assert "https://*.tile.openstreetmap.org" in csp


def test_csp_denies_frame_ancestors(client: FlaskClient) -> None:
    """verifies the CSP frame-ancestors directive is 'none'."""
    r = client.get("/healthz")
    csp = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in csp


# ---------------------------------------------------------------------------
# other security headers
# ---------------------------------------------------------------------------


def test_x_content_type_options_nosniff(client: FlaskClient) -> None:
    """verifies X-Content-Type-Options is set to nosniff."""
    r = client.get("/healthz")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"


def test_x_frame_options_deny(client: FlaskClient) -> None:
    """verifies X-Frame-Options is set to DENY."""
    r = client.get("/healthz")
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_referrer_policy_same_origin(client: FlaskClient) -> None:
    """verifies Referrer-Policy is set to same-origin."""
    r = client.get("/healthz")
    assert r.headers.get("Referrer-Policy") == "same-origin"


def test_permissions_policy_restricts_sensors(client: FlaskClient) -> None:
    """verifies Permissions-Policy restricts geolocation, microphone, and camera."""
    r = client.get("/healthz")
    pp = r.headers.get("Permissions-Policy", "")
    assert "geolocation=()" in pp
    assert "microphone=()" in pp
    assert "camera=()" in pp


def test_hsts_not_set_for_http_request(client: FlaskClient) -> None:
    """verifies Strict-Transport-Security is not set for plain HTTP requests."""
    r = client.get("/healthz")
    # the test client uses HTTP (not HTTPS), so HSTS must be absent
    assert "Strict-Transport-Security" not in r.headers


def test_csp_script_src_no_unsafe_eval(client: FlaskClient) -> None:
    """verifies script-src never contains unsafe-eval (csp alpine build regression guard)."""
    r = client.get("/healthz")
    csp = r.headers["Content-Security-Policy"]
    assert "unsafe-eval" not in csp


def test_security_headers_on_non_healthz_route(client: FlaskClient) -> None:
    """verifies security headers are present on all routes, not just /healthz."""
    r = client.get("/login")
    assert "Content-Security-Policy" in r.headers
    assert r.headers.get("X-Frame-Options") == "DENY"
