"""live-server fixture for browser e2e: runs the flask app in a daemon thread."""

from __future__ import annotations

import dataclasses
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
