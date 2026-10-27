"""applies camera setting changes: fresh read, format gate, backup, patch,
upload, wait for the camera, verify. holds the sync lock throughout so a
scheduled sync never runs against a rebooting camera."""

from __future__ import annotations

import dataclasses
import http.client
import logging
import os
import urllib.request

from blackvuesync_v2.server import camera_config
from blackvuesync_v2.server.camera_config import BackupStore, CameraStore
from blackvuesync_v2.server.camera_schema import (
    FORMAT_KEYS,
    is_secret,
    prepare_changes,
)
from blackvuesync_v2.server.sync_runner import try_hold_sync_lock
from blackvuesync_v2.settings import CameraSettings, Settings, SettingsStore
from blackvuesync_v2.sync import (
    apply_recording_filters,
    get_filenames,
    get_filepath,
    to_recording,
)

logger = logging.getLogger(__name__)

Changes = dict[tuple[str, str], str]


class ApplyRefused(Exception):  # noqa: N818
    """the apply stopped before changing anything on the camera."""

    def __init__(self, status: int, code: str, message: str, **detail: object) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail


@dataclasses.dataclass(frozen=True)
class ApplyResult:
    """what happened after the upload."""

    verified: bool
    came_back: bool
    kept: list[str]
    backup: str
    changed: list[str]


def _list_camera(address: str, timeout: float, affinity_key: str = "") -> list[str]:
    """lists the camera's recording filenames with a bounded timeout."""
    request = urllib.request.Request(f"http://{address}/blackvue_vod.cgi")
    if affinity_key:
        request.add_header("X-Affinity-Key", affinity_key)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        charset = response.info().get_param("charset", "UTF-8")
        return get_filenames([line.decode(charset) for line in response.readlines()])


def pending_recordings(settings: Settings) -> int | None:
    """counts recordings on the camera with no downloaded file; None if unreachable."""
    try:
        names = _list_camera(
            settings.connection.address,
            settings.connection.timeout_seconds,
            settings.sync.affinity_key or "",
        )
    except (OSError, ValueError, http.client.HTTPException):
        return None
    grouping = settings.sync.grouping
    parsed = (to_recording(name, grouping) for name in names if name.endswith(".mp4"))
    recordings = apply_recording_filters(
        [rec for rec in parsed if rec],
        settings.sync.include,
        settings.sync.exclude,
    )
    destination = settings.system.destination
    return sum(
        1
        for rec in recordings
        if not os.path.isfile(get_filepath(destination, rec.group_name, rec.filename))
    )


def _describe(changes: Changes) -> list[str]:
    """names the changed keys; passwords only show that they changed."""
    return [
        f"{section}.{key}" + (" (changed)" if is_secret(section, key) else "")
        for section, key in changes
    ]


def _check_format_gate(
    settings: Settings, changes: Changes, confirm_format: bool, accept_loss: bool
) -> None:
    """refuses format-triggering changes until the user has confirmed them."""
    if not any(name in FORMAT_KEYS for name in changes):
        return
    pending = pending_recordings(settings)
    if pending is None:
        raise ApplyRefused(
            409, "CAMERA_OFFLINE", "could not list the camera's recordings"
        )
    if confirm_format and (pending == 0 or accept_loss):
        return
    message = "this change makes the camera format its card; confirm it"
    if pending:
        message += f" and accept losing {pending} recordings"
    raise ApplyRefused(409, "FORMAT_CONFIRMATION_REQUIRED", message, pending=pending)


def _patch(raw: bytes, changes: Changes) -> bytes:
    try:
        patched = camera_config.patch(raw, changes)
        camera_config.self_check(raw, patched, changes)
    except camera_config.SelfCheckError as error:
        raise ApplyRefused(
            500, "PATCH_CHECK_FAILED", "the patched file failed its self-check"
        ) from error
    except camera_config.PatchError as error:
        raise ApplyRefused(
            422, "INVALID_CHANGES", str(error), errors=[str(error)]
        ) from error
    return patched


def _upload(address: str, patched: bytes, cam: CameraSettings, backup: str) -> None:
    try:
        camera_config.upload(address, patched, cam.read_timeout_seconds)
    except camera_config.UploadError as error:
        raise ApplyRefused(502, "UPLOAD_FAILED", str(error), backup=backup) from error


def _apply(
    store: SettingsStore,
    settings: Settings,
    changes: Changes,
    confirm_format: bool,
    accept_loss: bool,
) -> ApplyResult:
    """runs the pipeline while the caller holds the sync lock."""
    address = settings.connection.address
    cam = settings.camera
    fresh = camera_config.fetch(address, cam.read_timeout_seconds)
    if fresh is None:
        raise ApplyRefused(409, "CAMERA_OFFLINE", "the camera is not reachable")
    _check_format_gate(settings, changes, confirm_format, accept_loss)
    patched = _patch(fresh.config.raw, changes)
    backups = BackupStore(store.path.parent / "camera" / "backups")
    backup = backups.save(fresh.config.raw)
    backups.prune(cam.backup_count)
    _upload(address, patched, cam, backup)
    changed = _describe(changes)
    logger.info("camera settings uploaded: %s", ", ".join(changed))
    back = camera_config.wait_until_back(
        address, cam.verify_timeout_seconds, cam.verify_poll_seconds
    )
    if back is None:
        logger.warning("camera did not come back within the verify window")
        return ApplyResult(False, False, [], backup, changed)
    CameraStore(store.path.parent / "camera").save(back)
    kept = [
        f"{section}.{key}"
        for (section, key), value in changes.items()
        if back.config.value(section, key) != value
    ]
    return ApplyResult(not kept, True, kept, backup, changed)


def apply_changes(
    store: SettingsStore,
    raw_changes: dict[str, str],
    confirm_format: bool,
    accept_loss: bool = False,
) -> ApplyResult:
    """validates and applies changes to the camera, holding the sync lock."""
    settings = store.get()
    changes, errors = prepare_changes(raw_changes)
    if errors:
        raise ApplyRefused(
            422, "INVALID_CHANGES", "some changes are invalid", errors=errors
        )
    if not changes:
        raise ApplyRefused(422, "NO_CHANGES", "nothing to apply")
    with try_hold_sync_lock() as held:
        if not held:
            raise ApplyRefused(
                409, "SYNC_RUNNING", "a sync is running; try again when it finishes"
            )
        return _apply(store, settings, changes, confirm_format, accept_loss)
