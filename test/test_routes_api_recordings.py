"""tests for GET /api/recordings/recent."""

from __future__ import annotations

import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import create_app
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _make_store(settings_path: Path, destination: Path) -> SettingsStore:
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(settings_path)
    pw_hash = hash_password("test-password-1234")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, username="admin", password_hash=pw_hash),
            system=dataclasses.replace(s.system, destination=str(destination)),
        )
    )
    return store


@pytest.fixture()
def logged_in_client(settings_path: Path, tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "recordings"
    destination.mkdir()
    store = _make_store(settings_path, destination)
    app = create_app(store, testing=True)
    with app.test_client() as client:
        client.post(
            "/login",
            data={"username": "admin", "password": "test-password-1234"},
            follow_redirects=True,
        )
        yield client, destination


class TestRecent:
    """tests for GET /api/recordings/recent."""

    def test_returns_empty_for_empty_destination(self, logged_in_client: Any) -> None:
        client, _ = logged_in_client
        resp = client.get("/api/recordings/recent")
        assert resp.status_code == 200
        body = json.loads(resp.data)
        assert body["recordings"] == []
        assert body["total"] == 0

    def test_returns_newest_first(self, logged_in_client: Any) -> None:
        """newest files (highest mtime) appear first in the list."""
        client, destination = logged_in_client
        # create three valid recordings with controlled mtimes
        for name in (
            "20231015_115400_NF.mp4",
            "20231015_120000_NF.mp4",
            "20231015_113000_NF.mp4",
        ):
            (destination / name).write_text("x")
        # set mtimes so the 120000 file is newest
        now = time.time()
        os.utime(destination / "20231015_113000_NF.mp4", (now, now - 3600))
        os.utime(destination / "20231015_115400_NF.mp4", (now, now - 1800))
        os.utime(destination / "20231015_120000_NF.mp4", (now, now))

        resp = client.get("/api/recordings/recent")
        body = json.loads(resp.data)
        filenames = [r["filename"] for r in body["recordings"]]
        assert filenames == [
            "20231015_120000_NF.mp4",
            "20231015_115400_NF.mp4",
            "20231015_113000_NF.mp4",
        ]
        assert body["total"] == 3

    def test_default_limit_is_5(self, logged_in_client: Any) -> None:
        """without ?limit, returns at most 5 entries."""
        client, destination = logged_in_client
        for i in range(10):
            (destination / f"2023101{i}_120000_NF.mp4").write_text("x")
        resp = client.get("/api/recordings/recent")
        body = json.loads(resp.data)
        assert len(body["recordings"]) == 5
        assert body["total"] == 10

    def test_limit_query_param_respected(self, logged_in_client: Any) -> None:
        """?limit=3 returns 3 entries."""
        client, destination = logged_in_client
        for i in range(5):
            (destination / f"2023101{i}_120000_NF.mp4").write_text("x")
        resp = client.get("/api/recordings/recent?limit=3")
        body = json.loads(resp.data)
        assert len(body["recordings"]) == 3

    def test_non_matching_files_are_ignored(self, logged_in_client: Any) -> None:
        """files that don't match filename_re are not counted."""
        client, destination = logged_in_client
        (destination / "20231015_120000_NF.mp4").write_text("x")
        (destination / "README.txt").write_text("y")
        (destination / "random.bin").write_text("z")
        resp = client.get("/api/recordings/recent")
        body = json.loads(resp.data)
        assert body["total"] == 1

    def test_returns_401_when_unauthenticated(
        self, settings_path: Path, tmp_path: Path
    ) -> None:
        destination = tmp_path / "recordings"
        destination.mkdir()
        store = _make_store(settings_path, destination)
        app = create_app(store, testing=True)
        with app.test_client() as client:
            resp = client.get("/api/recordings/recent")
        assert resp.status_code == 401
        assert resp.get_json()["code"] == "AUTH_REQUIRED"
