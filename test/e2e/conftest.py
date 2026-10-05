"""live-server fixture for browser e2e: runs the flask app in a daemon thread."""

from __future__ import annotations

import dataclasses
import http.server
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from werkzeug.serving import make_server

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore


class _LiveServer:
    def __init__(self, app: Any, host: str, port: int) -> None:
        self.app = app
        self.url = f"http://{host}:{port}"
        self._srv = make_server(host, port, app, threaded=True)
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()


@pytest.fixture()
def live_server(tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, username="admin", password_hash=hash_password("pw-1234-test")
            ),
            system=dataclasses.replace(s.system, destination=str(destination)),
        )
    )
    app = create_app(store, testing=False)
    server = _LiveServer(app, "127.0.0.1", 0)
    # make_server with port 0 picks a free port; read it back
    server.url = f"http://127.0.0.1:{server._srv.server_port}"
    server.destination = destination
    server.start()
    yield server
    server.stop()


_CAMERA_FIXTURES = Path(__file__).parent.parent / "fixtures" / "camera"


class FakeCameraServer:
    """serves the fixture config.ini and version.bin like a dashcam."""

    def __init__(self) -> None:
        files = {
            "/Config/config.ini": (
                _CAMERA_FIXTURES / "dr900x-plus-config.ini"
            ).read_bytes(),
            "/Config/version.bin": (
                _CAMERA_FIXTURES / "dr900x-plus-version.bin"
            ).read_bytes(),
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = files.get(self.path.split("?")[0])
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

            def log_message(self, *_args: Any) -> None:
                return None

        self._srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.address = f"127.0.0.1:{self._srv.server_port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture()
def fake_camera(live_server: Any):  # type: ignore[no-untyped-def]
    camera = FakeCameraServer()
    live_server.app.settings_store.update(
        lambda s: dataclasses.replace(
            s, connection=dataclasses.replace(s.connection, address=camera.address)
        )
    )
    yield camera
    camera.stop()
