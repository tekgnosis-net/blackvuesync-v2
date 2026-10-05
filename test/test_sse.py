"""tests for the shared SSE response helper, including under waitress."""

from __future__ import annotations

import dataclasses
import http.client
import json
import os
import socket
import threading
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

from flask import Flask, Response
from waitress.server import create_server  # type: ignore[import-untyped]

from blackvuesync_v2.server import create_app, sse
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.server.sse import MAX_STREAMS, active_streams, sse_response
from blackvuesync_v2.settings import SettingsStore


def _events() -> Iterator[bytes]:
    """yields one frame and ends."""
    yield b"event: ping\ndata: {}\n\n"


def _endless() -> Iterator[bytes]:
    """yields keepalive frames forever."""
    while True:
        yield b": keepalive\n\n"


def _free_port() -> int:
    """returns an ephemeral localhost port that was free a moment ago."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def setup_function() -> None:
    """isolates the module-level stream counter between tests."""
    sse._LIMITER.reset()  # pylint: disable=protected-access


def teardown_function() -> None:
    """leaves no open-stream count behind."""
    sse._LIMITER.reset()  # pylint: disable=protected-access


def test_release_after_reset_does_not_free_a_newer_slot() -> None:
    """a stream opened before a reset must not release a slot taken after it."""
    stale = sse._LIMITER.acquire()  # pylint: disable=protected-access
    assert stale is not None
    sse._LIMITER.reset()  # pylint: disable=protected-access
    fresh = sse._LIMITER.acquire()  # pylint: disable=protected-access
    assert fresh is not None
    assert active_streams() == 1
    stale()
    assert active_streams() == 1
    fresh()
    assert active_streams() == 0


def test_sse_response_has_no_hop_by_hop_headers() -> None:
    """verifies Transfer-Encoding is left to the server (PEP 3333)."""
    app = Flask(__name__)
    with app.test_request_context():
        resp = sse_response(_events())
    assert "Transfer-Encoding" not in resp.headers
    assert resp.mimetype == "text/event-stream"
    resp.close()


def test_sse_stream_serves_200_under_waitress() -> None:
    """regression: waitress rejects hop-by-hop headers with a 500."""
    app = Flask(__name__)

    @app.route("/stream")
    def stream() -> Response:
        return sse_response(_events())

    port = _free_port()
    server = create_server(app, host="127.0.0.1", port=port, threads=2)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/stream")
        resp = conn.getresponse()
        body = resp.read()
        conn.close()
    finally:
        server.close()
        thread.join(timeout=1)
    assert resp.status == 200
    assert resp.getheader("Content-Type", "").startswith("text/event-stream")
    assert b"event: ping" in body


def test_sse_rejects_streams_beyond_cap() -> None:
    """verifies 503 TOO_MANY_STREAMS once MAX_STREAMS are open."""
    app = Flask(__name__)
    with app.test_request_context():
        open_resps = [sse_response(_endless()) for _ in range(MAX_STREAMS)]
        assert active_streams() == MAX_STREAMS
        rejected = sse_response(_endless())
    assert rejected.status_code == 503
    body = json.loads(rejected.get_data())
    assert body["code"] == "TOO_MANY_STREAMS"
    assert "error" in body
    for resp in open_resps:
        resp.close()
    assert active_streams() == 0


def test_sse_slot_released_once_after_iteration_and_close() -> None:
    """verifies the generator finally and response close do not double-release."""
    app = Flask(__name__)

    @app.route("/stream")
    def stream() -> Response:
        return sse_response(_events())

    with app.test_client() as client:
        resp = client.get("/stream")
        assert resp.data.startswith(b"event: ping")
        resp.close()
    assert active_streams() == 0


def test_real_app_sync_stream_serves_200_under_waitress(tmp_path: Path) -> None:
    """runs the real app's /api/sync/progress/stream under waitress."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, mode="none", password_hash=pw_hash)
        )
    )
    app = create_app(store)
    port = _free_port()
    server = create_server(app, host="127.0.0.1", port=port, threads=2)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/sync/progress/stream")
        resp = conn.getresponse()
        status = resp.status
        content_type = resp.getheader("Content-Type", "")
        first = resp.fp.readline()
        conn.close()
    finally:
        server.close()
        thread.join(timeout=1)
    assert status == 200
    assert content_type.startswith("text/event-stream")
    assert first
