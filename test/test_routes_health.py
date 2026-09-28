"""tests for health check routes: /healthz and /readyz."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient

from blackvuesync_v2.server import create_app
from blackvuesync_v2.settings import SettingsStore

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a path inside tmp_path for the settings file."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore at settings_path with a dummy address for validation."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


@pytest.fixture()
def app(settings_path: Path) -> Flask:
    """returns a Flask test app."""
    store = _make_store(settings_path)
    return create_app(store, testing=True)


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """returns a Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


def test_healthz_returns_200(client: FlaskClient) -> None:
    """verifies GET /healthz returns 200."""
    r = client.get("/healthz")
    assert r.status_code == 200


def test_healthz_returns_ok_status(client: FlaskClient) -> None:
    """verifies GET /healthz body contains status=ok."""
    r = client.get("/healthz")
    assert r.get_json() == {"status": "ok"}


def test_healthz_has_content_security_policy(client: FlaskClient) -> None:
    """verifies /healthz response includes the Content-Security-Policy header."""
    r = client.get("/healthz")
    assert "Content-Security-Policy" in r.headers


# ---------------------------------------------------------------------------
# /readyz
# ---------------------------------------------------------------------------


def test_readyz_returns_200_when_store_loaded(client: FlaskClient) -> None:
    """verifies GET /readyz returns 200 when settings store is loaded."""
    r = client.get("/readyz")
    assert r.status_code == 200


def test_readyz_returns_ready_status(client: FlaskClient) -> None:
    """verifies GET /readyz body shows status=ready when store is loaded."""
    r = client.get("/readyz")
    body = r.get_json()
    assert body is not None
    assert body["status"] == "ready"
    assert body["settings_loaded"] is True


def test_readyz_returns_503_when_store_is_none(settings_path: Path) -> None:
    """verifies GET /readyz returns 503 when settings_store is None."""
    store = _make_store(settings_path)
    app = create_app(store, testing=True)
    # simulate a not-yet-loaded store by replacing the attribute
    app.settings_store = None  # type: ignore[attr-defined]
    with app.test_client() as c:
        r = c.get("/readyz")
    assert r.status_code == 503
    body = r.get_json()
    assert body is not None
    assert body["status"] == "starting"
    assert body["settings_loaded"] is False
