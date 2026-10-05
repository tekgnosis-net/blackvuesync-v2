"""tests for reading the dashcam's config.ini and version.bin."""

from __future__ import annotations

import datetime
import http.client
import logging
import os
import stat
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from blackvuesync_v2.server import camera_config
from blackvuesync_v2.server.camera_config import (
    GENERAL_SECTION,
    CameraRead,
    CameraStore,
    Change,
    VersionInfo,
    diff,
    fetch,
    parse,
    parse_version,
    safe_text,
)

FIXTURES = Path(__file__).parent / "fixtures" / "camera"
CONFIG = (FIXTURES / "dr900x-plus-config.ini").read_bytes()
VERSION = (FIXTURES / "dr900x-plus-version.bin").read_bytes()


def test_parse_indexes_every_key_in_file_order() -> None:
    cfile = parse(CONFIG)
    assert len(cfile.entries) == 85
    assert cfile.sections() == ["Tab1", "Tab2", "Tab3", "Wifi", "Cloud"]
    assert cfile.entries[0].key == "TimeSet"
    assert cfile.value("Tab1", "TimeZone") == "1000"
    assert cfile.value("Tab1", "SetTime") == ""
    assert cfile.value("Cloud", "CloudSettingVersion") == "2026-01-01 00:00:00"
    assert cfile.value("Tab1", "Missing") is None
    assert cfile.raw == CONFIG


def test_crlf_and_lf_files_index_the_same() -> None:
    crlf = CONFIG.replace(b"\n", b"\r\n")
    assert parse(crlf).entries == parse(CONFIG).entries
    assert parse(crlf).raw == crlf


def test_keys_before_any_section_land_in_general() -> None:
    cfile = parse(b"Voice=1\n[Tab1]\nVOLUME=3\n")
    assert cfile.value(GENERAL_SECTION, "Voice") == "1"
    assert cfile.value("Tab1", "VOLUME") == "3"


def test_non_utf8_bytes_survive_and_display_safely() -> None:
    """review focus 1: a latin-1 ssid neither breaks parsing nor rendering."""
    raw = b"[Cloud]\nsta_ssid=Caf\xe9\n"
    cfile = parse(raw)
    assert cfile.raw == raw
    value = cfile.value("Cloud", "sta_ssid")
    assert value is not None
    assert safe_text(value) == "Caf�"
    safe_text(value).encode("utf-8")  # no surrogates left


def test_parse_version_reads_ini_form() -> None:
    info = parse_version(VERSION)
    assert (info.model, info.firmware) == ("DR900X Plus", "1.015")
    assert info.description == "DR900X Plus · fw 1.015"


def test_parse_version_keeps_a_bare_string_as_the_model() -> None:
    info = parse_version(b"DR900X-2CH 1.012\x00")
    assert (info.model, info.firmware) == ("DR900X-2CH 1.012", None)
    assert info.description == "DR900X-2CH 1.012"


def test_parse_version_of_nothing() -> None:
    info = parse_version(b"")
    assert (info.model, info.firmware) == (None, None)
    assert info.description == "BlackVue camera"


def test_parse_skips_non_key_lines_in_sections() -> None:
    """review focus 2: html attributes and non-key lines are skipped."""
    cfile = parse(b'[Tab1]\n<meta charset="utf-8">\nVOLUME=3\n')
    assert len(cfile.entries) == 1
    assert cfile.entries[0].key == "VOLUME"
    assert cfile.entries[0].value == "3"


def _serve(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes | None]) -> None:
    def fake_get(url: str, timeout: float) -> bytes | None:
        assert timeout == 1.5
        return files.get(url)

    monkeypatch.setattr(camera_config, "_get", fake_get)


def test_fetch_reads_both_files(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(
        monkeypatch,
        {
            "http://cam/Config/config.ini": CONFIG,
            "http://cam/Config/version.bin": VERSION,
        },
    )
    read = fetch("cam", 1.5)
    assert read is not None
    assert read.config.raw == CONFIG
    assert read.version.model == "DR900X Plus"
    assert read.read_at.tzinfo is not None


def test_fetch_without_version_bin(monkeypatch: pytest.MonkeyPatch) -> None:
    _serve(monkeypatch, {"http://cam/Config/config.ini": CONFIG})
    read = fetch("cam", 1.5)
    assert read is not None
    assert read.version.model is None


def test_fetch_returns_none_when_config_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, {})
    assert fetch("cam", 1.5) is None
    assert fetch("", 1.5) is None


def test_fetch_treats_a_non_ini_answer_as_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review focus 2: an html error page or empty body is not a settings file."""
    for body in (
        b"<html><body>404</body></html>",
        b'<!doctype html>\n<html><head><meta charset="utf-8"></head><body>404</body></html>',
        b"",
    ):
        _serve(monkeypatch, {"http://cam/Config/config.ini": body})
        assert fetch("cam", 1.5) is None


def test_get_maps_network_errors_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert camera_config._get("http://cam/x", 1.0) is None

    def bad_url(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("bad url")

    monkeypatch.setattr(urllib.request, "urlopen", bad_url)
    assert camera_config._get("http://cam x/x", 1.0) is None

    def incomplete(*_args: Any, **_kwargs: Any) -> Any:
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(urllib.request, "urlopen", incomplete)
    assert camera_config._get("http://cam/x", 1.0) is None


def _read(raw: bytes = CONFIG) -> CameraRead:
    return CameraRead(
        config=parse(raw),
        version=VersionInfo("DR900X Plus", "1.015"),
        read_at=datetime.datetime(2026, 10, 5, 1, 2, 3, tzinfo=datetime.timezone.utc),
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_store_round_trips_a_read_with_private_permissions(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "camera")
    assert store.load() is None
    store.save(_read())
    assert _mode(tmp_path / "camera") == 0o700
    assert _mode(tmp_path / "camera" / "config.ini") == 0o600
    assert _mode(tmp_path / "camera" / "snapshot.json") == 0o600
    loaded = store.load()
    assert loaded is not None
    assert loaded.config.raw == CONFIG
    assert loaded.version == VersionInfo("DR900X Plus", "1.015")
    assert loaded.read_at == _read().read_at


def test_load_tightens_wide_permissions(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = CameraStore(tmp_path / "camera")
    store.save(_read())
    os.chmod(tmp_path / "camera" / "config.ini", 0o644)
    with caplog.at_level(logging.WARNING):
        assert store.load() is not None
    assert _mode(tmp_path / "camera" / "config.ini") == 0o600
    assert "tightening permissions" in caplog.text


def test_load_survives_a_snapshot_it_cannot_tighten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CameraStore(tmp_path / "camera")
    store.save(_read())
    os.chmod(tmp_path / "camera" / "config.ini", 0o644)

    def refuse(_path: Any, _mode: int) -> None:
        raise PermissionError("not the owner")

    monkeypatch.setattr(os, "chmod", refuse)
    assert store.load() is None


def test_load_ignores_a_corrupt_snapshot(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "camera")
    store.save(_read())
    (tmp_path / "camera" / "snapshot.json").write_text("{not json", encoding="utf-8")
    assert store.load() is None


def test_diff_reports_changes_additions_and_removals_in_order() -> None:
    old = parse(b"[Tab3]\nVOLUME=5\nRECLED=1\nGone=1\n")
    new = parse(b"[Tab3]\nVOLUME=4\nRECLED=1\nNew=2\n")
    assert diff(old, new) == [
        Change("Tab3", "VOLUME", "5", "4"),
        Change("Tab3", "New", None, "2"),
        Change("Tab3", "Gone", "1", None),
    ]
    assert diff(new, new) == []


def _fixture_passwords() -> list[str]:
    cfile = parse(CONFIG)
    return [e.value for e in cfile.entries if e.key.endswith("_pw") and e.value]


def test_reprs_hide_password_values() -> None:
    passwords = _fixture_passwords()
    assert passwords
    cfile = parse(CONFIG)
    change = Change("Wifi", "ap_pw", "", cfile.value("Wifi", "ap_pw"))
    text = repr(cfile) + repr(cfile.entries[-1]) + repr(change)
    text += "".join(repr(e) for e in cfile.entries)
    assert not any(pw in text for pw in passwords)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory modes")  # type: ignore[misc]
def test_load_survives_an_unsearchable_directory(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "camera")
    store.save(_read())
    os.chmod(tmp_path / "camera", 0)
    try:
        assert store.load() is None
    finally:
        os.chmod(tmp_path / "camera", 0o700)


def test_save_leaves_no_temp_files(tmp_path: Path) -> None:
    store = CameraStore(tmp_path / "camera")
    store.save(_read())
    store.save(_read())
    assert sorted(p.name for p in (tmp_path / "camera").iterdir()) == [
        "config.ini",
        "snapshot.json",
    ]


def test_failed_save_cleans_up_its_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = CameraStore(tmp_path / "camera")

    def refuse(_src: Any, _dst: Any) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(OSError):
        store.save(_read())
    assert list((tmp_path / "camera").glob("*.tmp")) == []
