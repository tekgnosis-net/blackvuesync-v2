"""tests for the path-safe /media file route."""

from __future__ import annotations

import dataclasses
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
def client_and_dest(tmp_path: Path):  # type: ignore[no-untyped-def]
    dest = tmp_path / "recordings"
    dest.mkdir()
    (dest / "20260607_101500_NF.mp4").write_bytes(b"video-bytes-here")
    (dest / "20260607_101500_NF.thm").write_bytes(b"\xff\xd8\xff\xe0jpeg")
    (dest / "secret.json").write_text("{}")
    sub = dest / "2026-06-07"
    sub.mkdir()
    (sub / "20260607_101500_NF.mp4").write_bytes(b"nested-video")
    with patch.dict(os.environ, {"ADDRESS": "1.2.3.4"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, password_hash=hash_password("pw-1234-test")
            ),
            system=dataclasses.replace(s.system, destination=str(dest)),
        )
    )
    app = create_app(store, testing=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(
            app.settings_store.get().auth.password_hash  # type: ignore[attr-defined]
        )
    return client, dest


def test_serves_mp4(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/media/20260607_101500_NF.mp4")
    assert resp.status_code == 200
    assert resp.data == b"video-bytes-here"


def test_thm_served_as_jpeg(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/media/20260607_101500_NF.thm")
    assert resp.status_code == 200
    assert resp.mimetype == "image/jpeg"


def test_range_request_returns_206(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/media/20260607_101500_NF.mp4", headers={"Range": "bytes=0-4"})
    assert resp.status_code == 206
    assert resp.data == b"video"


def test_traversal_rejected(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    assert client.get("/media/../settings.json").status_code == 404
    assert client.get("/media/%2e%2e%2fsettings.json").status_code == 404


def test_disallowed_extension_rejected(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    assert client.get("/media/secret.json").status_code == 404


def test_requires_login(client_and_dest: Any) -> None:
    _, dest = client_and_dest
    with patch.dict(os.environ, {"ADDRESS": "1.2.3.4"}, clear=False):
        anon = create_app(SettingsStore(dest.parent / "settings.json"), testing=True)
    resp = anon.test_client().get("/media/20260607_101500_NF.mp4")
    assert resp.status_code in (302, 401)


def test_serves_file_in_grouping_subdir(client_and_dest: Any) -> None:
    client, _ = client_and_dest
    resp = client.get("/media/2026-06-07/20260607_101500_NF.mp4")
    assert resp.status_code == 200
    assert resp.data == b"nested-video"


def test_symlink_escape_rejected(client_and_dest: Any) -> None:
    client, dest = client_and_dest
    outside = dest.parent / "outside.mp4"
    outside.write_bytes(b"SECRET")
    (dest / "evil.mp4").symlink_to(outside)
    assert client.get("/media/evil.mp4").status_code == 404
