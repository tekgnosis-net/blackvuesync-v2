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
        raw = body[start : body.rindex(b"\r\n--")]
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
    assert sorted(result.changed) == ["Cloud.sta2_ssid", "Tab3.VOLUME"]
    assert camera_config.parse(camera.config).value("Tab3", "VOLUME") == "4"
    assert camera.uploads[0].count(b"\n") == CONFIG.count(b"\n")
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


def test_apply_reports_a_camera_that_never_returns(
    store: SettingsStore, camera: FakeCamera
) -> None:
    camera.reboot_reads = 10_000
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert not result.came_back and not result.verified


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
    assert "NewHome-9999" not in caplog.text
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
    with sync_runner.try_hold_sync_lock() as held:
        assert held
        with pytest.raises(camera_apply.ApplyRefused) as e:
            camera_apply.restore_backup(store, name, False)
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
