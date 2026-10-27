"""live-server fixture for browser e2e: runs the flask app in a daemon thread."""

from __future__ import annotations

import dataclasses
import email
import http.server
import os
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from werkzeug.serving import make_server

from blackvuesync_v2.server import create_app, sse
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
    # the stream limiter is process-wide; streams left open by earlier tests
    # only release their slot on their next write (up to 30 s later)
    sse._LIMITER.reset()  # pylint: disable=protected-access
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
        self.config = (_CAMERA_FIXTURES / "dr900x-plus-config.ini").read_bytes()
        self.vod: list[str] = []
        self.drop_uploads = False  # closes the connection without a reply
        version = (_CAMERA_FIXTURES / "dr900x-plus-version.bin").read_bytes()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _reply(self, status: int, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?")[0]
                if path == "/Config/config.ini":
                    self._reply(200, outer.config)
                elif path == "/Config/version.bin":
                    self._reply(200, version)
                elif path == "/blackvue_vod.cgi":
                    lines = ["v:1.00\r\n"]
                    lines += [f"n:/Record/{n},s:1000000\r\n" for n in outer.vod]
                    self._reply(200, "".join(lines).encode())
                else:
                    self._reply(404, b"")

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.split("?")[0] != "/upload.cgi":
                    self._reply(404, b"")
                    return
                if outer.drop_uploads:
                    self.close_connection = True
                    return
                head = b"Content-Type: " + self.headers["Content-Type"].encode()
                message = email.message_from_bytes(head + b"\r\n\r\n" + body)
                for part in message.walk():
                    disposition = part.get("Content-Disposition", "")
                    if 'name="FileData"' in disposition:
                        payload = part.get_payload(decode=True)
                        if isinstance(payload, bytes):
                            outer.config = payload
                self._reply(200, b"")

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
