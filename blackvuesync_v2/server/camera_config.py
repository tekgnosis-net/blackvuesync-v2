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
import urllib.request
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


__all__ = [
    "GENERAL_SECTION",
    "CameraFile",
    "CameraRead",
    "CameraStore",
    "Change",
    "ConfigEntry",
    "VersionInfo",
    "diff",
    "fetch",
    "parse",
    "parse_version",
    "safe_text",
]
