"""tests for the camera apply pipeline against a fake camera."""

# ruff: noqa: ARG001, ARG002, ARG005

from __future__ import annotations

import dataclasses
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import camera_apply, camera_config, sync_runner
from blackvuesync_v2.server.auth import hash_password
from blackvuesync_v2.settings import SettingsStore

FIXTURES = Path(__file__).parent / "fixtures" / "camera"
CONFIG = (FIXTURES / "dr900x-plus-config.ini").read_bytes()
VERSION = (FIXTURES / "dr900x-plus-version.bin").read_bytes()


class FakeCamera:
    """config.ini plus upload.cgi; may hide for a while after an upload."""

    def __init__(self) -> None:
        self.config = CONFIG
        self.vod: list[str] = []
        self.offline_reads = 0  # reads to fail right now
        self.reboot_reads = 0  # reads to fail after an upload (reboot)
        self.upload_status = 200
        self.uploads: list[bytes] = []
        self.clamp: dict[tuple[str, str], str] = {}  # values the firmware forces
        self.rewrite_tail = b""  # bytes the firmware appends to the stored file

    def get(self, url: str, timeout: float) -> bytes | None:
        path = url.split("192.0.2.10", 1)[1]
        if self.offline_reads > 0:
            self.offline_reads -= 1
            return None
        if path == "/Config/config.ini":
            return self.config
        if path == "/Config/version.bin":
            return VERSION
        return None

    def post(self, url: str, body: bytes, content_type: str, timeout: float) -> int:
        if self.upload_status != 200:
            raise camera_config.UploadError("boom", status=self.upload_status)
        start = body.index(b"\r\n\r\n", body.index(b'filename="config.ini"')) + 4
        raw = camera_config.stored_by_camera(body[start:]) + self.rewrite_tail
        self.uploads.append(raw)
        new = raw
        for (section, key), value in self.clamp.items():
            new = camera_config.patch(new, {(section, key): value})
        self.config = new
        self.offline_reads = self.reboot_reads
        return 200

    def vod_lines(self) -> list[str]:
        return [f"n:/Record/{name},s:1000000" for name in self.vod]


@pytest.fixture()  # type: ignore[misc]
def camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    fake = FakeCamera()
    monkeypatch.setattr(camera_config, "_get", fake.get)
    monkeypatch.setattr(camera_config, "_post", fake.post)
    monkeypatch.setattr(camera_apply, "_list_camera", lambda *_a: list(fake.vod))
    clock = [0.0]

    def fake_sleep(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(
        camera_config,
        "time",
        SimpleNamespace(sleep=fake_sleep, monotonic=lambda: clock[0]),
    )
    return fake


@pytest.fixture()  # type: ignore[misc]
def store(tmp_path: Path) -> SettingsStore:
    (tmp_path / "rec").mkdir()
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        settings = SettingsStore(tmp_path / "settings.json")
    settings.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(
                s.auth, password_hash=hash_password("pw-1234-test")
            ),
            system=dataclasses.replace(s.system, destination=str(tmp_path / "rec")),
            camera=dataclasses.replace(
                s.camera, verify_poll_seconds=1.0, verify_timeout_seconds=10.0
            ),
        )
    )
    return settings


def test_pending_counts_recordings_without_a_downloaded_file(
    store: SettingsStore, camera: FakeCamera, tmp_path: Path
) -> None:
    camera.vod = [
        "20261027_210000_NF.mp4",
        "20261027_210000_NR.mp4",
        "20261027_210100_NF.mp4",
    ]
    (tmp_path / "rec" / "20261027_210000_NF.mp4").write_bytes(b"x")
    assert camera_apply.pending_recordings(store.get()) == 2
    camera.vod = []
    assert camera_apply.pending_recordings(store.get()) == 0


def test_pending_is_none_when_the_camera_is_unreachable(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object) -> list[str]:
        raise OSError("no route")

    monkeypatch.setattr(camera_apply, "_list_camera", boom)
    assert camera_apply.pending_recordings(store.get()) is None


def test_apply_patches_backs_up_uploads_and_verifies(
    store: SettingsStore, camera: FakeCamera
) -> None:
    result = camera_apply.apply_changes(
        store, {"Tab3.VOLUME": "4", "Cloud.sta2_ssid": "Shed"}, False
    )
    assert result.verified and result.came_back and result.kept == []
    assert result.file_exact
    # the camera stores exactly the framed file, so a second apply sends the same bytes
    assert camera.config == camera_config.framed(camera.uploads[0])
    assert sorted(result.changed) == ["Cloud.sta2_ssid", "Tab3.VOLUME"]
    assert camera_config.parse(camera.config).value("Tab3", "VOLUME") == "4"
    # one blank line more than the fixture: the framing the camera keeps intact
    assert camera.uploads[0].count(b"\n") == CONFIG.count(b"\n") + 1
    backups = camera_config.BackupStore(store.path.parent / "camera" / "backups")
    assert [b.name for b in backups.list()] == [result.backup]
    assert backups.read(result.backup) == CONFIG
    snapshot = camera_config.CameraStore(store.path.parent / "camera").load()
    assert snapshot is not None and snapshot.config.value("Tab3", "VOLUME") == "4"


def test_apply_survives_a_reboot_and_reports_kept_values(
    store: SettingsStore, camera: FakeCamera
) -> None:
    """review focus 2: the camera comes back with a clamped value."""
    camera.reboot_reads = 3
    camera.clamp[("Tab3", "VOLUME")] = "7"
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "9"}, False)
    assert result.came_back and not result.verified and result.kept == ["Tab3.VOLUME"]
    snapshot = camera_config.CameraStore(store.path.parent / "camera").load()
    assert snapshot is not None and snapshot.config.value("Tab3", "VOLUME") == "7"


def test_apply_reports_a_rewritten_file(
    store: SettingsStore, camera: FakeCamera
) -> None:
    # every value matches but the firmware appended to the file
    camera.rewrite_tail = b"junk\r\n"
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert result.verified and result.came_back and result.kept == []
    assert not result.file_exact


def test_apply_reports_a_camera_that_never_returns(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.reboot_reads = 10_000
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert not result.came_back and not result.verified and not result.file_exact


def test_apply_refusals(store: SettingsStore, camera: FakeCamera) -> None:
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab3.VOLUME": "x"}, False)
    assert (e.value.status, e.value.code) == (422, "INVALID_CHANGES")
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {}, False)
    assert e.value.code == "NO_CHANGES"
    camera.offline_reads = 1
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert (e.value.status, e.value.code) == (409, "CAMERA_OFFLINE")
    camera.upload_status = 500
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert (e.value.status, e.value.code) == (502, "UPLOAD_FAILED")
    assert camera_config.BackupStore(store.path.parent / "camera" / "backups").list()


def test_format_keys_need_confirmation_and_accepted_loss(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.vod = ["20261027_210000_NF.mp4"]
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, True, False)
    assert (e.value.status, e.value.code, e.value.detail) == (
        409,
        "FORMAT_CONFIRMATION_REQUIRED",
        {"pending": 1},
    )
    assert camera.uploads == []
    result = camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, True, True)
    assert result.verified and len(camera.uploads) == 1
    camera.vod = []
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab1.TimeZone": "1000"}, False, False)
    assert e.value.detail == {"pending": 0}
    result = camera_apply.apply_changes(store, {"Tab1.TimeZone": "1000"}, True, False)
    assert result.verified


def test_apply_is_refused_while_a_sync_holds_the_lock(
    store: SettingsStore, camera: FakeCamera
) -> None:
    """review focus 3 (inverse): the apply never races a running sync."""
    lock = sync_runner._sync_lock  # pylint: disable=protected-access
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(camera_apply.ApplyRefused) as e:
            camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
        assert (e.value.status, e.value.code) == (409, "SYNC_RUNNING")
    finally:
        lock.release()


def test_scheduled_sync_is_skipped_during_an_apply(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    """review focus 3: trigger_sync answers already_running while an apply holds the lock."""
    seen: dict[str, object] = {}
    original_post = camera.post

    def post_and_probe(url: str, body: bytes, content_type: str, timeout: float) -> int:
        from blackvuesync_v2.server.progress import ProgressPublisher

        seen["during"] = sync_runner.trigger_sync(store.get(), ProgressPublisher())[
            "status"
        ]
        return original_post(url, body, content_type, timeout)

    monkeypatch.setattr(camera_config, "_post", post_and_probe)
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert seen["during"] == "already_running"
    assert not sync_runner._sync_lock.locked()  # pylint: disable=protected-access


def test_passwords_never_reach_the_log(
    store: SettingsStore, camera: FakeCamera, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        camera_apply.apply_changes(
            store, {"Cloud.sta_pw": "NewHome-9999", "Tab3.VOLUME": "3"}, False
        )
    from blackvuesync_v2.server.camera_crypto import encrypt_password

    assert "NewHome-9999" not in caplog.text
    assert encrypt_password("NewHome-9999") not in caplog.text
    assert "Cloud.sta_pw (changed)" in caplog.text and "Tab3.VOLUME" in caplog.text


def test_pending_listing_timeout_yields_none(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    def slow(*_args: object) -> list[str]:
        raise socket.timeout("timed out")

    monkeypatch.setattr(camera_apply, "_list_camera", slow)
    assert camera_apply.pending_recordings(store.get()) is None


def test_self_check_failure_leaks_no_values(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    from blackvuesync_v2.server.camera_crypto import encrypt_password

    def fail(*_args: object) -> None:
        raise camera_config.SelfCheckError("x")

    monkeypatch.setattr(camera_config, "self_check", fail)
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Cloud.sta_pw": "NewHome-9999"}, False)
    assert (e.value.status, e.value.code) == (500, "PATCH_CHECK_FAILED")
    leaked = f"{e.value} {e.value.detail}"
    assert "NewHome-9999" not in leaked
    assert encrypt_password("NewHome-9999") not in leaked
    assert camera.uploads == []


def test_real_self_check_messages_name_keys_only() -> None:
    changes = {("Tab3", "VOLUME"): "9"}
    with pytest.raises(camera_config.SelfCheckError) as e:
        camera_config.self_check(CONFIG, CONFIG, changes)
    assert str(e.value) == "Tab3.VOLUME did not patch as expected"


def test_refused_patch_keeps_the_existing_backup(
    store: SettingsStore, camera: FakeCamera
) -> None:
    store.update(
        lambda s: dataclasses.replace(
            s, camera=dataclasses.replace(s.camera, backup_count=1)
        )
    )
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    backups = camera_config.BackupStore(store.path.parent / "camera" / "backups")
    before = [b.name for b in backups.list()]
    assert len(before) == 1 and backups.read(before[0]) == CONFIG
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab9.Nope": "1"}, False)
    assert e.value.status == 422
    assert [b.name for b in backups.list()] == before
    assert backups.read(before[0]) == CONFIG


def _backup(store: SettingsStore, changes: dict[tuple[str, str], str]) -> str:
    raw = camera_config.patch(CONFIG, changes)
    return camera_config.BackupStore(store.path.parent / "camera" / "backups").save(raw)


def test_restore_uploads_only_the_differences(
    store: SettingsStore, camera: FakeCamera
) -> None:
    current = camera_config.parse(CONFIG).value("Tab3", "VOLUME")
    other = "2" if current != "2" else "3"
    name = _backup(store, {("Tab3", "VOLUME"): other})
    result = camera_apply.restore_backup(store, name, False)
    assert result.verified and result.changed == ["Tab3.VOLUME"]
    assert len(camera.uploads) == 1
    assert camera_config.parse(camera.config).value("Tab3", "VOLUME") == other


def test_restore_honours_the_format_gate(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.vod = ["20261027_210000_NF.mp4"]
    name = _backup(store, {("Tab1", "TimeZone"): "1100"})
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.restore_backup(store, name, True, False)
    assert (e.value.status, e.value.code, e.value.detail) == (
        409,
        "FORMAT_CONFIRMATION_REQUIRED",
        {"pending": 1},
    )
    assert camera.uploads == []
    result = camera_apply.restore_backup(store, name, True, True)
    assert result.verified and len(camera.uploads) == 1


@pytest.mark.parametrize(
    "name", ["config-x.ini", "../settings.json", "", "config-20260101-000000.ini"]
)  # type: ignore[misc]
def test_restore_unknown_or_malformed_name_is_404(
    store: SettingsStore, camera: FakeCamera, name: str
) -> None:
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.restore_backup(store, name, True, True)
    assert (e.value.status, e.value.code) == (404, "NO_SUCH_BACKUP")


def test_restore_identical_backup_is_no_changes(
    store: SettingsStore, camera: FakeCamera
) -> None:
    name = camera_config.BackupStore(store.path.parent / "camera" / "backups").save(
        CONFIG
    )
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.restore_backup(store, name, False)
    assert (e.value.status, e.value.code) == (422, "NO_CHANGES")
    assert camera.uploads == []


def test_restore_skips_read_only_keys(store: SettingsStore, camera: FakeCamera) -> None:
    current = camera_config.parse(CONFIG).value("Tab3", "VOLUME")
    other = "2" if current != "2" else "3"
    name = _backup(
        store,
        {
            ("Cloud", "CloudSettingVersion"): "2020-01-01 00:00:00",
            ("Tab3", "VOLUME"): other,
        },
    )
    result = camera_apply.restore_backup(store, name, False)
    assert result.changed == ["Tab3.VOLUME"]
    new = camera_config.parse(camera.config)
    assert new.value("Cloud", "CloudSettingVersion") == "2026-01-01 00:00:00"


def test_restore_only_read_only_differences_is_no_changes(
    store: SettingsStore, camera: FakeCamera
) -> None:
    name = _backup(store, {("Cloud", "CloudSettingVersion"): "2020-01-01 00:00:00"})
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.restore_backup(store, name, False)
    assert (e.value.status, e.value.code) == (422, "NO_CHANGES")
    assert camera.uploads == []


def test_restore_refused_while_a_sync_runs(
    store: SettingsStore, camera: FakeCamera
) -> None:
    name = _backup(store, {("Tab3", "VOLUME"): "2"})
    lock = sync_runner._sync_lock  # pylint: disable=protected-access
    assert lock.acquire(blocking=False)
    try:
        with pytest.raises(camera_apply.ApplyRefused) as e:
            camera_apply.restore_backup(store, name, False)
    finally:
        lock.release()
    assert (e.value.status, e.value.code) == (409, "SYNC_RUNNING")
    assert camera.uploads == []


def test_restore_refused_when_the_camera_is_offline(
    store: SettingsStore, camera: FakeCamera
) -> None:
    name = _backup(store, {("Tab3", "VOLUME"): "2"})
    camera.offline_reads = 5
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.restore_backup(store, name, False)
    assert (e.value.status, e.value.code) == (409, "CAMERA_OFFLINE")
    assert camera.uploads == []


def test_upload_preserves_every_other_byte(
    store: SettingsStore, camera: FakeCamera
) -> None:
    import re

    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    expected = re.sub(rb"(?m)^(VOLUME=)[^\r\n]*", rb"\g<1>4", CONFIG, count=1)
    assert expected != CONFIG
    assert camera.uploads == [camera_config.framed(expected)]


def test_apply_continues_when_the_upload_answer_is_lost(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = camera.post

    def drop_answer(url: str, body: bytes, content_type: str, timeout: float) -> int:
        original(url, body, content_type, timeout)  # the camera took the file
        raise camera_config.UploadError("upload failed: reset", status=None)

    monkeypatch.setattr(camera_config, "_post", drop_answer)
    camera.reboot_reads = 2
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert len(camera.uploads) == 1
    assert (result.verified, result.came_back, result.upload_confirmed) == (
        True,
        True,
        False,
    )
    assert camera_config.parse(camera.config).value("Tab3", "VOLUME") == "4"


def test_lost_upload_that_never_took_effect_reports_kept(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    def drop(url: str, body: bytes, content_type: str, timeout: float) -> int:
        raise camera_config.UploadError("upload failed: reset", status=None)

    monkeypatch.setattr(camera_config, "_post", drop)
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert result.upload_confirmed is False
    assert result.came_back and not result.verified
    assert result.kept == ["Tab3.VOLUME"]


def test_a_rejected_upload_is_still_a_502(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.upload_status = 500
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert (e.value.status, e.value.code) == (502, "UPLOAD_FAILED")


def test_upload_uses_the_configured_timeout(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.update(
        lambda s: dataclasses.replace(
            s, camera=dataclasses.replace(s.camera, upload_timeout_seconds=17.0)
        )
    )
    seen: list[float] = []
    original = camera.post

    def spy(url: str, body: bytes, content_type: str, timeout: float) -> int:
        seen.append(timeout)
        return original(url, body, content_type, timeout)

    monkeypatch.setattr(camera_config, "_post", spy)
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert seen == [17.0]


def test_offline_camera_uploads_nothing(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.offline_reads = 99
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert (e.value.status, e.value.code) == (409, "CAMERA_OFFLINE")
    assert camera.uploads == []


def test_missing_key_is_422_before_the_format_gate(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_listing(*_args: object) -> list[str]:
        raise AssertionError("the gate must not run first")

    monkeypatch.setattr(camera_apply, "_list_camera", no_listing)
    camera.config = camera.config.replace(b"TimeZone=", b"TimeZoneX=")
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, False)
    assert (e.value.status, e.value.code) == (422, "INVALID_CHANGES")


def test_a_second_apply_while_one_runs_is_apply_running(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, tuple[int, str]] = {}
    original = camera.post

    def post_and_probe(url: str, body: bytes, content_type: str, timeout: float) -> int:
        try:
            camera_apply.apply_changes(store, {"Tab3.VOLUME": "5"}, False)
        except camera_apply.ApplyRefused as refused:
            seen["apply"] = (refused.status, refused.code)
        try:
            camera_apply.restore_backup(store, "config-20260101-000000.ini", False)
        except camera_apply.ApplyRefused as refused:
            seen["restore"] = (refused.status, refused.code)
        return original(url, body, content_type, timeout)

    monkeypatch.setattr(camera_config, "_post", post_and_probe)
    _backup(store, {("Tab3", "VOLUME"): "2"})
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert seen["apply"] == (409, "APPLY_RUNNING")
    # an unknown backup name answers 404 before the lock is tried
    assert seen["restore"] == (404, "NO_SUCH_BACKUP")
    assert sync_runner.lock_holder() == ""


def test_the_scheduler_logs_a_camera_apply_skip(
    store: SettingsStore,
    camera: FakeCamera,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    from blackvuesync_v2.server.progress import ProgressPublisher
    from blackvuesync_v2.server.scheduler import _scheduled_run

    original = camera.post

    def post_and_probe(url: str, body: bytes, content_type: str, timeout: float) -> int:
        with caplog.at_level(logging.INFO):
            _scheduled_run(store, ProgressPublisher())
        return original(url, body, content_type, timeout)

    monkeypatch.setattr(camera_config, "_post", post_and_probe)
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert "a camera settings change is in progress" in caplog.text
