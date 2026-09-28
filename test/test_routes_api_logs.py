"""flask test-client tests for /api/logs/* and the /logs page."""

from __future__ import annotations

import dataclasses
import json
import logging
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
def app_and_client(tmp_path: Path):  # type: ignore[no-untyped-def]
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
    app = create_app(store, testing=True, log_file_path="/config/logs/blackvuesync.log")
    with app.test_client() as client:
        with client.session_transaction() as sess:
            sess["user"] = "admin"
            sess[SESSION_VERSION_KEY] = session_version(
                app.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
            )
        yield app, client


def _emit(
    app: Any, msg: str, level: int = logging.INFO, name: str = "blackvuesync"
) -> None:
    app.log_buffer.emit(logging.LogRecord(name, level, "p.py", 1, msg, None, None))


def test_recent_returns_buffered_lines_and_meta(app_and_client: Any) -> None:
    app, client = app_and_client
    _emit(app, "first line", logging.WARNING)
    resp = client.get("/api/logs/recent")
    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert data["file_path"] == "/config/logs/blackvuesync.log"
    assert data["capacity"] == app.log_buffer.capacity
    assert data["verbosity"] == "normal"
    assert data["lines"][-1]["message"] == "first line"
    assert data["lines"][-1]["level"] == "WARNING"


def test_recent_requires_login(app_and_client: Any) -> None:
    app, _client = app_and_client
    anon = app.test_client()
    resp = anon.get("/api/logs/recent")
    assert resp.status_code in (302, 401)


def test_stream_emits_sse_log_frames(app_and_client: Any) -> None:
    app, client = app_and_client
    _emit(app, "streamed line", logging.ERROR)
    resp = client.get("/api/logs/stream", buffered=False)
    assert resp.status_code == 200
    assert resp.mimetype == "text/event-stream"
    assert resp.headers["X-Accel-Buffering"] == "no"
    assert resp.headers["Cache-Control"] == "no-store"
    chunk = next(resp.response)
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    assert text.startswith("event: logs\ndata: ")
    payload = json.loads(text.split("data: ", 1)[1].strip())
    assert payload["lines"][0]["message"] == "streamed line"
    resp.close()


def test_logs_page_renders_snapshot_server_side(app_and_client: Any) -> None:
    app, client = app_and_client
    _emit(app, "rendered-in-html", logging.INFO)
    resp = client.get("/logs")
    assert resp.status_code == 200
    assert b"rendered-in-html" in resp.data
    assert b"js/logs.js" in resp.data
    assert b"/config/logs/blackvuesync.log" in resp.data


def test_logs_page_escapes_message_text(app_and_client: Any) -> None:
    app, client = app_and_client
    _emit(app, "<script>alert(1)</script>", logging.INFO)
    resp = client.get("/logs")
    assert b"<script>alert(1)</script>" not in resp.data
    assert b"&lt;script&gt;" in resp.data
