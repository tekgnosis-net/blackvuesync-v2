"""reads the dashcam's Config/config.ini and version.bin without rewriting them.

parsing only locates `key=value` lines; nothing is re-serialized, so a later
patch can change single values and leave every other byte as it was.
standard library only.
"""

from __future__ import annotations

import configparser
import dataclasses
import datetime
import http.client
import json
import logging
import os
import re
import stat
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

GENERAL_SECTION = "General"  # keys before any [section] header (legacy firmware)
_KEY_RE = re.compile(r"[A-Za-z0-9_.-]+")


@dataclasses.dataclass(frozen=True)
class ConfigEntry:
    """one `key=value` line of config.ini."""

    section: str
    key: str
    value: str = dataclasses.field(repr=False)  # may be a password


@dataclasses.dataclass(frozen=True)
class CameraFile:
    """config.ini bytes plus the entries found in them, in file order."""

    raw: bytes = dataclasses.field(repr=False)
    entries: tuple[ConfigEntry, ...] = dataclasses.field(repr=False)

    def value(self, section: str, key: str) -> str | None:
        """returns the first value of section.key, or None when absent."""
        for entry in self.entries:
            if entry.section == section and entry.key == key:
                return entry.value
        return None

    def sections(self) -> list[str]:
        """returns the section names in order of first appearance."""
        seen: list[str] = []
        for entry in self.entries:
            if entry.section not in seen:
                seen.append(entry.section)
        return seen


def parse(raw: bytes) -> CameraFile:
    """indexes config.ini; undecodable bytes survive via surrogateescape."""
    text = raw.decode("utf-8", errors="surrogateescape")
    section = GENERAL_SECTION
    entries: list[ConfigEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
        elif "=" in stripped and not stripped.startswith((";", "#")):
            key, _, value = stripped.partition("=")
            key_stripped = key.strip()
            if _KEY_RE.fullmatch(key_stripped):
                entries.append(ConfigEntry(section, key_stripped, value.strip()))
    return CameraFile(raw=raw, entries=tuple(entries))


def safe_text(value: str) -> str:
    """returns value with undecodable bytes as U+FFFD, safe for html and json."""
    return value.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


@dataclasses.dataclass(frozen=True)
class VersionInfo:
    """the camera model and firmware version from version.bin."""

    model: str | None
    firmware: str | None

    @property
    def description(self) -> str:
        """returns e.g. "DR900X Plus · fw 1.015" for display."""
        if self.model and self.firmware:
            return f"{self.model} · fw {self.firmware}"
        return self.model or "BlackVue camera"


def parse_version(raw: bytes) -> VersionInfo:
    """reads model and firmware from version.bin.

    current firmware writes an ini ([firmware] version=, model=); older
    firmware writes a bare string such as "DR900X-2CH 1.012", kept as the model.
    """
    decoded = raw.decode("utf-8", errors="replace")
    text = "".join(c for c in decoded if c.isprintable() or c in "\r\n").strip()
    if not text:
        return VersionInfo(model=None, firmware=None)
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return VersionInfo(model=text, firmware=None)
    if not parser.has_section("firmware"):
        return VersionInfo(model=text, firmware=None)
    return VersionInfo(
        model=parser.get("firmware", "model", fallback=None),
        firmware=parser.get("firmware", "version", fallback=None),
    )


@dataclasses.dataclass(frozen=True)
class CameraRead:
    """one successful read of the camera."""

    config: CameraFile
    version: VersionInfo
    read_at: datetime.datetime  # utc


def _get(url: str, timeout: float) -> bytes | None:
    """GETs url; None on any network, protocol, or url failure."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            body: bytes = resp.read()
            return body
    except (OSError, ValueError, http.client.HTTPException):
        return None


def fetch(address: str, timeout: float) -> CameraRead | None:
    """reads config.ini and version.bin; None when no settings could be read."""
    if not address:
        return None
    raw = _get(f"http://{address}/Config/config.ini", timeout)
    if raw is None:
        return None
    config = parse(raw)
    if not config.entries:
        return None  # an error page or empty body, not a settings file
    version_raw = _get(f"http://{address}/Config/version.bin", timeout) or b""
    return CameraRead(
        config=config,
        version=parse_version(version_raw),
        read_at=datetime.datetime.now(datetime.timezone.utc),
    )


@dataclasses.dataclass(frozen=True)
class Change:
    """one key whose value differs between two reads."""

    section: str
    key: str
    before: str | None = dataclasses.field(repr=False)
    after: str | None = dataclasses.field(repr=False)


def _first_values(cfile: CameraFile) -> dict[tuple[str, str], str]:
    values: dict[tuple[str, str], str] = {}
    for entry in cfile.entries:
        values.setdefault((entry.section, entry.key), entry.value)
    return values


def diff(old: CameraFile, new: CameraFile) -> list[Change]:
    """lists keys that changed or appeared (new file order), then disappeared."""
    before = _first_values(old)
    after = _first_values(new)
    changes = [
        Change(section, key, before.get((section, key)), value)
        for (section, key), value in after.items()
        if before.get((section, key)) != value
    ]
    changes.extend(
        Change(section, key, value, None)
        for (section, key), value in before.items()
        if (section, key) not in after
    )
    return changes


def _write_private(path: Path, data: bytes) -> None:
    """writes data atomically with mode 0600."""
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def _tighten(path: Path, mode: int) -> None:
    """narrows permissions wider than mode, with a warning."""
    current = stat.S_IMODE(path.stat().st_mode)
    if current & ~mode:
        logger.warning(
            "tightening permissions of %s from %o to %o", path, current, mode
        )
        os.chmod(path, mode)


class CameraStore:
    """keeps the last successful read; it holds wi-fi passwords, so 0700/0600."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    @property
    def directory(self) -> Path:
        """returns the snapshot directory."""
        return self._dir

    def save(self, read: CameraRead) -> None:
        """replaces the snapshot with read."""
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        _write_private(self._dir / "config.ini", read.config.raw)
        meta = {
            "read_at": read.read_at.isoformat(),
            "model": read.version.model,
            "firmware": read.version.firmware,
        }
        _write_private(self._dir / "snapshot.json", json.dumps(meta).encode("utf-8"))

    def load(self) -> CameraRead | None:
        """returns the snapshot, or None when there is none or it is unreadable."""
        config_path = self._dir / "config.ini"
        meta_path = self._dir / "snapshot.json"
        try:
            if not (config_path.is_file() and meta_path.is_file()):
                return None
            _tighten(self._dir, 0o700)
            _tighten(config_path, 0o600)
            _tighten(meta_path, 0o600)
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            read_at = datetime.datetime.fromisoformat(meta["read_at"])
            raw = config_path.read_bytes()
        except (OSError, ValueError, KeyError, TypeError):
            logger.warning("ignoring an unreadable camera snapshot in %s", self._dir)
            return None
        return CameraRead(
            config=parse(raw),
            version=VersionInfo(meta.get("model"), meta.get("firmware")),
            read_at=read_at,
        )


class PatchError(ValueError):
    """the requested change cannot be applied to this file unambiguously."""


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def patch(raw: bytes, changes: dict[tuple[str, str], str]) -> bytes:
    """rewrites the value of each changed key in place; every other byte stays.

    a key that is missing or duplicated in its section raises PatchError, as
    does a value with control characters (which could inject lines).
    """
    for (section, key), value in changes.items():
        if _CONTROL_RE.search(value):
            raise PatchError(f"{section}.{key}: value contains control characters")
    pending = dict(changes)
    seen: set[tuple[str, str]] = set()
    out: list[bytes] = []
    section = GENERAL_SECTION
    for line in raw.splitlines(keepends=True):
        body = line.decode("utf-8", errors="surrogateescape").rstrip("\r\n")
        if body.strip().startswith("[") and body.strip().endswith("]"):
            section = body.strip()[1:-1].strip()
            out.append(line)
            continue
        key, sep, _ = body.partition("=")
        name = key.strip()
        if not sep or not _KEY_RE.fullmatch(name):
            out.append(line)
            continue
        if (section, name) in seen and (section, name) in changes:
            raise PatchError(f"{section}.{name}: key appears more than once")
        seen.add((section, name))
        if (section, name) in pending:
            value = pending.pop((section, name))
            # keeps the key text and its spacing up to "=" exactly as written
            head = key + "=" + (" " if key.endswith(" ") else "") + value
            out.append(
                head.encode("utf-8", errors="surrogateescape")
                + line[len(body.encode("utf-8", errors="surrogateescape")) :]
            )
        else:
            out.append(line)
    if pending:
        missing = ", ".join(f"{s}.{k}" for s, k in pending)
        raise PatchError(f"not in the camera's file: {missing}")
    return b"".join(out)


def self_check(
    before: bytes, after: bytes, changes: dict[tuple[str, str], str]
) -> None:
    """asserts that after differs from before only in the changed keys."""
    old = _first_values(parse(before))
    new = _first_values(parse(after))
    if set(old) != set(new):
        raise PatchError("patched file has a different set of keys")
    for name, value in new.items():
        expected = changes.get(name, old[name])
        if value != expected:
            raise PatchError(f"{name[0]}.{name[1]} is {value!r}, expected {expected!r}")


class UploadError(RuntimeError):
    """the camera did not accept the upload; status is None for transport errors."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _post(url: str, body: bytes, content_type: str, timeout: float) -> int:
    """POSTs body and returns the HTTP status; raises UploadError otherwise."""
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type, "Content-Length": str(len(body))},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310
            resp.read()
            status: int = resp.status
            return status
    except urllib.error.HTTPError as error:
        raise UploadError(f"camera answered {error.code}", status=error.code) from error
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise UploadError(f"upload failed: {error}") from error


def upload(address: str, raw: bytes, timeout: float) -> int:
    """uploads raw as the camera's config.ini (upload.cgi, FileType=config)."""
    boundary = "----blackvuesync-" + uuid.uuid4().hex
    bound = boundary.encode()
    body = b"".join(
        [
            b"--" + bound + b"\r\n",
            b'Content-Disposition: form-data; name="FileType"\r\n\r\nconfig\r\n',
            b"--" + bound + b"\r\n",
            b'Content-Disposition: form-data; name="FileData"; filename="config.ini"\r\n',
            b"Content-Type: application/octet-stream\r\n\r\n",
            raw,
            b"\r\n--" + bound + b"--\r\n",
        ]
    )
    return _post(
        f"http://{address}/upload.cgi",
        body,
        f"multipart/form-data; boundary={boundary}",
        timeout,
    )


def wait_until_back(address: str, timeout: float, poll: float) -> CameraRead | None:
    """polls config.ini until the camera answers or timeout seconds pass."""
    deadline = time.monotonic() + timeout
    while True:
        read = fetch(address, min(poll, 5.0) if poll > 0 else 1.0)
        if read is not None:
            return read
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)


BACKUP_NAME_RE = re.compile(r"config-\d{8}-\d{6}(?:-\d+)?\.ini")


@dataclasses.dataclass(frozen=True)
class BackupInfo:
    """one saved copy of the camera's file."""

    name: str
    created: datetime.datetime
    size: int


class BackupStore:
    """keeps copies of config.ini taken before each upload (0700/0600)."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def save(self, raw: bytes, now: datetime.datetime | None = None) -> str:
        """stores raw under a local-time name; returns the name."""
        self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self._dir, 0o700)
        stamp = (now or datetime.datetime.now()).astimezone().strftime("%Y%m%d-%H%M%S")
        name = f"config-{stamp}.ini"
        suffix = 1
        while (self._dir / name).exists():
            name = f"config-{stamp}-{suffix}.ini"
            suffix += 1
        _write_private(self._dir / name, raw)
        return name

    def list(self) -> list[BackupInfo]:
        """returns the backups, newest first."""
        if not self._dir.is_dir():
            return []
        infos = []
        for path in self._dir.iterdir():
            if BACKUP_NAME_RE.fullmatch(path.name) and path.is_file():
                stat_result = path.stat()
                infos.append(
                    BackupInfo(
                        name=path.name,
                        created=datetime.datetime.fromtimestamp(
                            stat_result.st_mtime, datetime.timezone.utc
                        ),
                        size=stat_result.st_size,
                    )
                )
        return sorted(infos, key=lambda b: (b.created, b.name), reverse=True)

    def prune(self, keep: int) -> None:
        """deletes all but the newest keep backups."""
        for info in self.list()[max(keep, 0) :]:
            (self._dir / info.name).unlink(missing_ok=True)

    def read(self, name: str) -> bytes | None:
        """returns a backup's bytes; None for an unknown or malformed name."""
        if not BACKUP_NAME_RE.fullmatch(name):
            return None
        path = self._dir / name
        return path.read_bytes() if path.is_file() else None


__all__ = [
    "GENERAL_SECTION",
    "BACKUP_NAME_RE",
    "BackupInfo",
    "BackupStore",
    "CameraFile",
    "CameraRead",
    "CameraStore",
    "Change",
    "PatchError",
    "ConfigEntry",
    "UploadError",
    "VersionInfo",
    "diff",
    "fetch",
    "parse",
    "parse_version",
    "patch",
    "safe_text",
    "self_check",
    "upload",
    "wait_until_back",
]
