# Camera Settings, Phase A (read-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show every dashcam `config.ini` setting read-only in a Camera group of the Settings page, with Wi-Fi passwords masked and revealable, and stop `/api/dashcam/info` from returning passwords. Ships as 3.2.0.

**Architecture:** Three new server modules:

- `camera_crypto.py`: the BlackVue password encoding.
- `camera_config.py`: reads `config.ini` and `version.bin`, keeps a 0600 snapshot, and diffs reads.
- `camera_schema.py`: labels and display rules for every key.

`routes/api_camera.py` exposes JSON (`/api/camera/config`, `/api/camera/secret`). `routes/hx_camera.py` renders the panes as an htmx fragment, loaded into the existing Settings page under a new **Camera** nav heading. A new `camera` app-settings section holds the read timeout.

**Tech Stack:**

- Python 3.9+ and Flask, with the Jinja templates.
- htmx and Alpine (CSP build), as already vendored.
- `cryptography~=50.0` (new; AES only).
- pytest and Playwright for tests.

**Spec:** `docs/plans/2026-10-05-sub-project-7-camera-settings-design.md` (phase A
in section 9). Phases B (tests on the camera) and C (writes) are not in this plan;
section "After this plan" lists phase B. The phase-C plan is written once phase B
has revealed the `upload.cgi` format.

**Deviations from the spec, decided while planning:**

- **No `POST /api/camera/refresh`.** `GET /api/camera/config` always reads the
  camera live, so the UI's **Refresh** just repeats it. Every read also reports
  `changed` against the previous snapshot.
- **The `camera` app-settings section arrives in phase A,** holding only
  `read_timeout_seconds`. Kumar's rule is that every threshold is a knob, and the
  panes need a read timeout. Phase C adds the backup and verify knobs to the same
  section.
- **`/api/camera/secret` reads the snapshot only** (spec 4.2 says live, else
  snapshot). The panes render from the snapshot just saved, so the eye reveals
  what the panes show; if the snapshot cannot be saved, reveal serves the
  previous snapshot.
- **Phase-A panes are read-only label/value rows, not disabled inputs.** Phase C
  replaces them with editable widgets.
- **Unknown sections, if any firmware has them,** go into the System tab's
  "Other" group as `Section.key`, so no pane is unreachable from the nav.

## Global Constraints

- Python 3.9+ (`from __future__ import annotations` in every module); type annotations everywhere.
- `sync.py` and `metrics.py` stay standard-library only; `cryptography` is imported only by `blackvuesync_v2/server/camera_crypto.py`.
- Comments and docstrings: lowercase, third person ("returns", not "return"), concise.
- CSP: no inline scripts or event handlers. Alpine CSP build: directives are bare property or method references, never expressions.
- Colours come only from `static/css/tokens.css` variables. Text must pass `test/e2e/test_contrast.py` (4.5:1) in light and dark.
- Camera passwords (`Wifi.ap_pw`, `Cloud.sta_pw`, `Cloud.sta2_pw`, `Cloud.sta3_pw`):
  - never logged;
  - never in HTML or JSON except `/api/camera/secret`;
  - never committed, except the invented ones in `test/fixtures/camera/`.
- Snapshot files under `<settings dir>/camera/`: directory `0700`, files `0600`.
- Times shown in the UI are local time (`datetime.astimezone()`); stored times are UTC.
- Commit message lines under 80 characters. **No Claude attribution or session trailer** in commits or PR bodies.
- Never `--no-verify`. Pre-commit runs black, ruff, mypy, pylint, markdownlint and gitlint.
- Masked password text everywhere: `••••••••` (8 × U+2022).

## Review Focus

1. **Non-UTF-8 bytes in an SSID or user text** (firmware writing Latin-1). The panes and JSON must still render, showing U+FFFD; the raw bytes are untouched. Tested in Tasks 2 and 8.
2. **The camera answers `config.ini` with something that is not an INI** (an HTML error page or an empty body). This must take the offline path, not render an empty settings page. Tested in Task 2.
3. **The settings directory is not writable for the snapshot.** The live settings still show and a warning is logged; no 500. Tested in Task 7.
4. **A password value that does not decrypt to printable text** (another firmware or key). The reveal reports "can't be shown" and never displays garbage. Tested in Tasks 1 and 7.
5. **Keys or sections a future firmware adds.** They appear under "Other" and are never dropped. Tested in Task 4.

---

### Task 1: Password encoding and the `cryptography` dependency

**Files:**

- Create: `blackvuesync_v2/server/camera_crypto.py`
- Modify: `pyproject.toml` (dependencies), `Dockerfile` (builder `uv pip install` list), `.pre-commit-config.yaml` (mypy `additional_dependencies`)
- Test: `test/test_camera_crypto.py`

**Interfaces:**

- Consumes: `test/fixtures/camera/dr900x-plus-config.ini` (exists; plaintexts listed in `test/fixtures/camera/README.md`).
- Produces:
  - `is_encrypted(value: str) -> bool`
  - `decrypt_password(value: str) -> str` (raises `UndecodablePasswordError`)
  - `encrypt_password(text: str) -> str` (raises `ValueError` over 32 bytes)
  - `class UndecodablePasswordError(ValueError)`

- [ ] **Step 1: Add the dependency**

In `pyproject.toml`, `[project] dependencies`, add after `"APScheduler~=3.10",`:

```toml
    "cryptography~=50.0",
```

In `Dockerfile`, extend the builder install line so it reads:

```dockerfile
        "argon2-cffi>=23.1,<26.0" "APScheduler~=3.10" "cryptography~=50.0"
```

In `.pre-commit-config.yaml`, in the mypy hook's `additional_dependencies` list, add after `- APScheduler~=3.10`:

```yaml
          - cryptography~=50.0
```

Run: `venv/bin/pip install -e ".[dev]"`. Expected: `Successfully installed cryptography-50.0.x` (plus `cffi`). Musl wheels for amd64 and arm64 were checked to exist (`cp311-abi3-musllinux_1_2`), and the package requires Python ≥ 3.9.

- [ ] **Step 2: Write the failing tests**

Create `test/test_camera_crypto.py`:

```python
"""tests for the blackvue wi-fi password encoding."""

from __future__ import annotations

from pathlib import Path

import pytest

from blackvuesync_v2.server.camera_crypto import (
    UndecodablePasswordError,
    decrypt_password,
    encrypt_password,
    is_encrypted,
)

FIXTURE = Path(__file__).parent / "fixtures" / "camera" / "dr900x-plus-config.ini"

# the invented plaintexts behind the fixture's encrypted values (fixture README)
PLAINTEXTS = {
    "ap_pw": "demo-cam",
    "sta_pw": "DemoHome-123",
    "sta2_pw": "DemoGarage-2",
    "sta3_pw": "DemoPhone-33",
}


def _fixture_passwords() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in PLAINTEXTS:
            values[key] = value
    return values


def test_fixture_passwords_decrypt_to_known_plaintexts() -> None:
    stored = _fixture_passwords()
    assert set(stored) == set(PLAINTEXTS)
    for key, text in PLAINTEXTS.items():
        assert decrypt_password(stored[key]) == text


def test_encrypt_reproduces_the_camera_encoding() -> None:
    stored = _fixture_passwords()
    for key, text in PLAINTEXTS.items():
        assert encrypt_password(text) == stored[key]


def test_round_trip_unicode_and_empty() -> None:
    for text in ("", "pässwörd-ü", "x" * 32):
        assert decrypt_password(encrypt_password(text)) == text


def test_plain_text_values_pass_through() -> None:
    assert decrypt_password("hunter22") == "hunter22"
    assert decrypt_password("") == ""


def test_is_encrypted_only_for_64_hex_characters() -> None:
    assert is_encrypted("A" * 64)
    assert is_encrypted("0f" * 32)
    assert not is_encrypted("A" * 63)
    assert not is_encrypted("G" * 64)
    assert not is_encrypted("")


def test_password_longer_than_32_bytes_is_refused() -> None:
    with pytest.raises(ValueError):
        encrypt_password("x" * 33)
    with pytest.raises(ValueError):
        encrypt_password("ü" * 17)  # 34 bytes as utf-8


def test_value_that_does_not_decrypt_to_text_is_reported() -> None:
    """review focus 4: another firmware's key yields bytes, not text."""
    with pytest.raises(UndecodablePasswordError):
        decrypt_password("00" * 32)
```

- [ ] **Step 3: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_camera_crypto.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'blackvuesync_v2.server.camera_crypto'`.

- [ ] **Step 4: Implement**

Create `blackvuesync_v2/server/camera_crypto.py`:

```python
"""blackvue wi-fi password encoding used in the camera's config.ini.

the camera stores ap_pw and sta*_pw as uppercase hex of the password,
zero-padded to 32 bytes and encrypted with aes-128-cbc under a fixed key and
iv built into the blackvue app. the key and iv were published by the 2023 cve
research (github.com/eyJhb/blackvue-cve-2023, software/wifi-decrypt), so the
encoding hides nothing from anyone who has read it; the app still treats the
values as secrets. a value that is not 64 hex characters is plain text.
"""

from __future__ import annotations

import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_KEY = bytes.fromhex("331248f9789959003729fa90cf16882f")
_IV = bytes.fromhex("82931267f734a879b3c4c4b15fda4be7")
_BLOCK = 32  # the camera pads every password to 32 bytes
_ENCRYPTED_RE = re.compile(r"[0-9A-Fa-f]{64}")


class UndecodablePasswordError(ValueError):
    """the stored value looks encrypted but does not decrypt to text."""


def _cipher() -> Cipher[modes.CBC]:
    return Cipher(algorithms.AES(_KEY), modes.CBC(_IV))


def is_encrypted(value: str) -> bool:
    """returns whether a stored value has the camera's encrypted shape."""
    return bool(_ENCRYPTED_RE.fullmatch(value))


def decrypt_password(value: str) -> str:
    """returns the password text; plain-text values come back unchanged."""
    if not is_encrypted(value):
        return value
    decryptor = _cipher().decryptor()
    plain = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
    try:
        text = plain.rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError as error:
        raise UndecodablePasswordError("value does not decrypt to text") from error
    if not text.isprintable():
        raise UndecodablePasswordError("value does not decrypt to text")
    return text


def encrypt_password(text: str) -> str:
    """returns the camera's stored form of a password of at most 32 bytes."""
    raw = text.encode("utf-8")
    if len(raw) > _BLOCK:
        raise ValueError(f"password is {len(raw)} bytes; the camera allows {_BLOCK}")
    encryptor = _cipher().encryptor()
    sealed = encryptor.update(raw.ljust(_BLOCK, b"\0")) + encryptor.finalize()
    return sealed.hex().upper()


__all__ = [
    "UndecodablePasswordError",
    "decrypt_password",
    "encrypt_password",
    "is_encrypted",
]
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_camera_crypto.py -v`
Expected: 7 passed. (`test_encrypt_reproduces_the_camera_encoding` passing proves the key and IV constants are byte-exact.)

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml Dockerfile .pre-commit-config.yaml \
  blackvuesync_v2/server/camera_crypto.py test/test_camera_crypto.py
git commit -m "feat(camera): decode and encode blackvue wi-fi passwords"
```

---

### Task 2: Reading `config.ini` and `version.bin`

**Files:**

- Create: `blackvuesync_v2/server/camera_config.py`, `test/fixtures/camera/dr900x-plus-version.bin`
- Test: `test/test_camera_config.py`

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces (used by Tasks 3, 4, 6, 7, 8):
  - `GENERAL_SECTION: str`
  - `ConfigEntry(section: str, key: str, value: str)`
  - `CameraFile(raw: bytes, entries: tuple[ConfigEntry, ...])`, with `.value(section, key) -> str | None` and `.sections() -> list[str]`
  - `parse(raw: bytes) -> CameraFile`
  - `safe_text(value: str) -> str`
  - `VersionInfo(model: str | None, firmware: str | None)`, with property `.description -> str`
  - `parse_version(raw: bytes) -> VersionInfo`
  - `CameraRead(config: CameraFile, version: VersionInfo, read_at: datetime.datetime)`
  - `fetch(address: str, timeout: float) -> CameraRead | None`
  - internal `_get(url: str, timeout: float) -> bytes | None` (tests monkeypatch it)

- [ ] **Step 1: Add the version.bin fixture**

```bash
printf '[firmware]\nversion = 1.015\nmodel = DR900X Plus\nlanguage = English\n[config]\nversion = 1.071\n[revision]\nrev = 2230\n' \
  > test/fixtures/camera/dr900x-plus-version.bin
```

Then add a line to `test/fixtures/camera/README.md`, after the first paragraph:

```markdown
`dr900x-plus-version.bin` is the same camera's `Config/version.bin` (no personal
data in it).
```

- [ ] **Step 2: Write the failing tests**

Create `test/test_camera_config.py`:

```python
"""tests for reading the dashcam's config.ini and version.bin."""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any

import pytest

from blackvuesync_v2.server import camera_config
from blackvuesync_v2.server.camera_config import (
    GENERAL_SECTION,
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
    for body in (b"<html><body>404</body></html>", b""):
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
```

- [ ] **Step 3: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_camera_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'camera_config'`.

- [ ] **Step 4: Implement**

Create `blackvuesync_v2/server/camera_config.py`:

```python
"""reads the dashcam's Config/config.ini and version.bin without rewriting them.

parsing only locates `key=value` lines; nothing is re-serialized, so a later
patch can change single values and leave every other byte as it was.
standard library only.
"""

from __future__ import annotations

import configparser
import dataclasses
import datetime
import urllib.request

GENERAL_SECTION = "General"  # keys before any [section] header (legacy firmware)


@dataclasses.dataclass(frozen=True)
class ConfigEntry:
    """one `key=value` line of config.ini."""

    section: str
    key: str
    value: str


@dataclasses.dataclass(frozen=True)
class CameraFile:
    """config.ini bytes plus the entries found in them, in file order."""

    raw: bytes
    entries: tuple[ConfigEntry, ...]

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
            entries.append(ConfigEntry(section, key.strip(), value.strip()))
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
    """GETs url; None on any network or url failure (the camera is http-only)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            body: bytes = resp.read()
            return body
    except (OSError, ValueError):
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
```

(`__all__` is added in Task 3 once the module is complete.)

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_camera_config.py -v`
Expected: 12 passed.

- [ ] **Step 6: Commit**

```bash
git add blackvuesync_v2/server/camera_config.py test/test_camera_config.py \
  test/fixtures/camera
git commit -m "feat(camera): read config.ini and version.bin without rewriting"
```

---

### Task 3: Snapshot store and change detection

**Files:**

- Modify: `blackvuesync_v2/server/camera_config.py` (append)
- Test: `test/test_camera_config.py` (append)

**Interfaces:**

- Consumes: `CameraFile`, `CameraRead`, `VersionInfo`, `parse` (Task 2).
- Produces (used by Task 7):
  - `Change(section: str, key: str, before: str | None, after: str | None)`
  - `diff(old: CameraFile, new: CameraFile) -> list[Change]`
  - `CameraStore(directory: pathlib.Path)`, with `.save(read: CameraRead) -> None`, `.load() -> CameraRead | None` and property `.directory`

- [ ] **Step 1: Write the failing tests**

Append to `test/test_camera_config.py`:

```python
import datetime
import logging
import os
import stat

from blackvuesync_v2.server.camera_config import (
    CameraRead,
    CameraStore,
    Change,
    VersionInfo,
    diff,
)


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
```

Move the new imports to the top of the file with the others (`ruff` enforces it at commit).

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_camera_config.py -v`
Expected: FAIL with `ImportError: cannot import name 'CameraStore'`.

- [ ] **Step 3: Implement**

Add to the imports of `camera_config.py`:

```python
import json
import logging
import os
import stat
from pathlib import Path
```

and below the imports:

```python
logger = logging.getLogger(__name__)
```

Append:

```python
@dataclasses.dataclass(frozen=True)
class Change:
    """one key whose value differs between two reads."""

    section: str
    key: str
    before: str | None
    after: str | None


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
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


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
        if not (config_path.is_file() and meta_path.is_file()):
            return None
        _tighten(self._dir, 0o700)
        _tighten(config_path, 0o600)
        _tighten(meta_path, 0o600)
        try:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_camera_config.py -v`
Expected: 16 passed.

- [ ] **Step 5: Commit**

```bash
git add blackvuesync_v2/server/camera_config.py test/test_camera_config.py
git commit -m "feat(camera): private snapshot of the last read and change diff"
```

---

### Task 4: Key schema and display rules

**Files:**

- Create: `blackvuesync_v2/server/camera_schema.py`
- Test: `test/test_camera_schema.py`

**Interfaces:**

- Consumes: `CameraFile`, `parse`, `safe_text`, `GENERAL_SECTION` (Task 2).
- Produces (used by Tasks 6, 7, 8):
  - `CameraField`, `CameraTab(name, label, section)`
  - `TABS: tuple[CameraTab, ...]`, `FIELDS: dict[tuple[str, str], CameraField]`
  - `field_for(section, key) -> CameraField`, `is_secret(section, key) -> bool`
  - `format_utc_offset(raw: str) -> str`, `display_value(field, raw) -> str`
  - `MASK = "••••••••"`
  - `FieldView(key, label, value, raw, secret, has_value, formats_card, help)`
  - `TabView(name, label, section, fields, other, not_fitted)`
  - `build_tabs(cfile: CameraFile) -> list[TabView]`, which always returns exactly the five tabs in `TABS` order

- [ ] **Step 1: Write the failing tests**

Create `test/test_camera_schema.py`:

```python
"""tests for the camera key descriptors and display rules."""

from __future__ import annotations

from pathlib import Path

from blackvuesync_v2.server.camera_config import parse
from blackvuesync_v2.server.camera_schema import (
    FIELDS,
    MASK,
    TABS,
    build_tabs,
    display_value,
    field_for,
    format_utc_offset,
    is_secret,
)

CONFIG = (
    Path(__file__).parent / "fixtures" / "camera" / "dr900x-plus-config.ini"
).read_bytes()


def test_tabs_match_the_blackvue_app() -> None:
    assert [(t.name, t.label, t.section) for t in TABS] == [
        ("basic", "Basic", "Tab1"),
        ("sensitivity", "Sensitivity", "Tab2"),
        ("system", "System", "Tab3"),
        ("wifi", "Wi-Fi", "Wifi"),
        ("cloud", "Cloud", "Cloud"),
    ]


def test_every_dr900x_plus_key_is_described() -> None:
    cfile = parse(CONFIG)
    missing = [
        f"{e.section}.{e.key}" for e in cfile.entries if (e.section, e.key) not in FIELDS
    ]
    assert missing == []
    assert len(FIELDS) == 85


def test_secret_and_format_flags() -> None:
    assert {f"{s}.{k}" for (s, k), f in FIELDS.items() if f.secret} == {
        "Wifi.ap_pw",
        "Cloud.sta_pw",
        "Cloud.sta2_pw",
        "Cloud.sta3_pw",
    }
    assert {k for (_s, k), f in FIELDS.items() if f.formats_card} == {
        "TimeSet",
        "SetTime",
        "TimeZone",
        "Daylight",
        "GpsSync",
        "ImageSetting",
        "VideoQuality",
    }
    assert is_secret("Cloud", "sta_pw")
    assert not is_secret("Cloud", "sta_ssid")
    assert not is_secret("Tab3", "userString")


def test_utc_offsets() -> None:
    assert format_utc_offset("1000") == "UTC+10:00"
    assert format_utc_offset("-1100") == "UTC-11:00"
    assert format_utc_offset("930") == "UTC+09:30"
    assert format_utc_offset("0") == "UTC+00:00"
    assert format_utc_offset("abc") == "abc"
    assert format_utc_offset("1070") == "1070"


def test_display_values() -> None:
    assert display_value(field_for("Tab1", "TimeZone"), "1000") == "UTC+10:00"
    assert display_value(field_for("Tab1", "VideoQuality"), "0") == "Highest (Extreme)"
    assert display_value(field_for("Tab1", "VideoQuality"), "9") == "9"
    assert display_value(field_for("Tab3", "LowvoltageVolt"), "1190") == "11.9 V"
    assert display_value(field_for("Tab3", "LowvoltageTime"), "0") == "Off"
    assert display_value(field_for("Tab3", "LowvoltageTime"), "12") == "12 h"
    assert display_value(field_for("Tab3", "ScheduledRebootTime"), "3") == "03:00"
    assert display_value(field_for("Tab3", "RECLED"), "1") == "On"
    assert display_value(field_for("Wifi", "WiFiBand"), "0") == "5 GHz"
    assert display_value(field_for("Cloud", "sta_pw"), "ABC") == MASK
    assert display_value(field_for("Cloud", "sta3_pw"), "") == ""


def test_build_tabs_groups_the_fixture() -> None:
    tabs = build_tabs(parse(CONFIG))
    assert [t.name for t in tabs] == [t.name for t in TABS]
    by_name = {t.name: t for t in tabs}
    basic = {f.key: f for f in by_name["basic"].fields}
    assert basic["Tab1.TimeZone"].value == "UTC+10:00"
    assert basic["Tab1.TimeZone"].raw == "1000"
    assert basic["Tab1.TimeZone"].formats_card
    system = by_name["system"]
    assert len(system.not_fitted) == 11
    assert all(f.key.startswith("Tab3.Dsm") for f in system.not_fitted)
    cloud = {f.key: f for f in by_name["cloud"].fields}
    password = cloud["Cloud.sta_pw"]
    assert (password.value, password.raw, password.secret) == (MASK, None, True)
    assert password.has_value
    rendered = repr(tabs)
    assert "1E1BC3E1" not in rendered and "DemoHome-123" not in rendered
    for tab in tabs:
        assert tab.other == ()


def test_unknown_keys_and_sections_are_kept_under_other() -> None:
    """review focus 5: nothing a new firmware adds is dropped."""
    cfile = parse(b"[Tab1]\nVOLUME2=7\n[Fancy]\nMode=2\nLevel=1\n")
    tabs = {t.name: t for t in build_tabs(cfile)}
    assert [(f.key, f.label, f.value) for f in tabs["basic"].other] == [
        ("Tab1.VOLUME2", "VOLUME2", "7")
    ]
    assert [f.key for f in tabs["system"].other] == ["Fancy.Mode", "Fancy.Level"]
    assert tabs["system"].other[0].label == "Fancy.Mode"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_camera_schema.py -v`
Expected: FAIL with `ModuleNotFoundError: ... camera_schema`.

- [ ] **Step 3: Implement**

Create `blackvuesync_v2/server/camera_schema.py`:

```python
"""describes the dashcam's config.ini keys for the camera settings panes.

labels follow the blackvue dr900x plus manual; value codes come from kumar's
camera, the manual and the dr900s config table (design spec, appendix a). a key
without a descriptor is still shown, as a plain field in an "Other" group.
"""

from __future__ import annotations

import dataclasses

from blackvuesync_v2.server.camera_config import CameraFile, safe_text

MASK = "•" * 8


@dataclasses.dataclass(frozen=True)
class CameraField:
    """how one config.ini key is labelled and displayed."""

    section: str
    key: str
    label: str
    widget: str = "number"  # select, toggle, number, text, password, hour, readonly
    options: tuple[tuple[str, str], ...] = ()  # (raw value, label)
    unit: str = ""
    scale: float = 1.0
    secret: bool = False
    formats_card: bool = False  # the manual: changing it formats the microsd card
    read_only: bool = False
    not_fitted: bool = False  # hardware absent on the dr900x plus
    help: str = ""


@dataclasses.dataclass(frozen=True)
class CameraTab:
    """one camera settings tab and the config.ini section behind it."""

    name: str
    label: str
    section: str


TABS: tuple[CameraTab, ...] = (
    CameraTab("basic", "Basic", "Tab1"),
    CameraTab("sensitivity", "Sensitivity", "Tab2"),
    CameraTab("system", "System", "Tab3"),
    CameraTab("wifi", "Wi-Fi", "Wifi"),
    CameraTab("cloud", "Cloud", "Cloud"),
)


def _t(section: str, key: str, label: str, **kw: object) -> CameraField:
    return CameraField(section, key, label, "toggle", **kw)  # type: ignore[arg-type]


def _n(section: str, key: str, label: str, **kw: object) -> CameraField:
    return CameraField(section, key, label, "number", **kw)  # type: ignore[arg-type]


def _s(
    section: str, key: str, label: str, options: tuple[tuple[str, str], ...], **kw: object
) -> CameraField:
    return CameraField(section, key, label, "select", options, **kw)  # type: ignore[arg-type]


_SENSOR_AXES = (("1", "up and down"), ("2", "side to side"), ("3", "front and back"))
_VOICES = (
    ("STARTVOICE", "power on"),
    ("NORMALSTARTVOICE", "starting normal recording"),
    ("EVENTSTARTVOICE", "starting event recording"),
    ("CHANGERECORDMODEVOICE", "changing recording mode"),
    ("ENDVOICE", "power off"),
    ("SPEEDALERTVOICE", "speed alert"),
    ("ACCELERATIONVOICE", "rapid acceleration"),
    ("HARSHBRAKINGVOICE", "harsh braking"),
    ("SHARPTURNVOICE", "sharp turn"),
    ("CHANGECONFIGVOICE", "settings changed"),
    ("CLOUDVOICE", "cloud related"),
    ("PARKINGEVENTVOICE", "impact detected in parking mode"),
)
_DSM = (
    ("DsmDetectBox", "detection box"),
    ("DsmDrowsy", "drowsiness"),
    ("DsmDistracted", "distraction"),
    ("DsmUndetected", "driver not detected"),
    ("DsmCalling", "phone call"),
    ("DsmMaskOff", "mask off"),
    ("DsmSmoking", "smoking"),
    ("DsmParkingMode", "parking mode"),
    ("DsmLed", "LED"),
    ("DsmSensitivity", "sensitivity"),
    ("DsmVolume", "volume"),
)

_FIELDS: tuple[CameraField, ...] = (
    # basic ([Tab1])
    _s("Tab1", "TimeSet", "Time source", (("0", "Sync with GPS"), ("1", "Manual")), formats_card=True),
    CameraField("Tab1", "SetTime", "Manual time", "text", formats_card=True, help="24-hour HHMM, used when the time source is manual"),
    CameraField("Tab1", "TimeZone", "Time zone", "select", formats_card=True),
    _t("Tab1", "Daylight", "Daylight saving time", formats_card=True),
    _t("Tab1", "GpsSync", "Sync time with GPS", formats_card=True),
    _n("Tab1", "ImageSetting", "Image setting", formats_card=True, help="0 is the highest setting"),
    _s("Tab1", "VideoQuality", "Image quality", (("0", "Highest (Extreme)"), ("1", "Highest"), ("2", "High"), ("3", "Normal")), formats_card=True),
    _t("Tab1", "NormalRecord", "Normal recording"),
    _n("Tab1", "AutoParking", "Parking mode", help="value codes not confirmed yet"),
    _t("Tab1", "RearParkingMode", "Rear camera recording in parking mode"),
    _t("Tab1", "VoiceRecord", "Voice recording"),
    _t("Tab1", "DateDisplay", "Date and time display"),
    _s("Tab1", "SpeedUnit", "Speed unit", (("0", "km/h"), ("1", "MPH"), ("2", "Off"))),
    CameraField("Tab1", "RecordTime", "Video segment length", "readonly", (("1", "1 minute"),), read_only=True),
    _t("Tab1", "LockEvent", "Lock event files"),
    _t("Tab1", "OverwriteLock", "Overwrite locked event files when full"),
    _s("Tab1", "FrontRotate", "Front camera rotation", (("0", "Off"), ("1", "180°"))),
    _s("Tab1", "RearRotate", "Rear camera orientation", (("0", "Default"), ("1", "Rotate 180°"), ("2", "Mirror"))),
    _t("Tab1", "UseGpsInfo", "GPS location recording"),
    # sensitivity ([Tab2])
    *(_n("Tab2", f"NORMALSENSOR{n}", f"G-sensor, normal mode: {axis}") for n, axis in _SENSOR_AXES),
    *(_n("Tab2", f"PARKINGSENSOR{n}", f"G-sensor, parking mode: {axis}") for n, axis in _SENSOR_AXES),
    _n("Tab2", "MOTIONSENSOR", "Motion detection, parking mode"),
    _n("Tab2", "FrontMotionRegion", "Front motion detection regions", help="65535 means all regions"),
    _n("Tab2", "RearMotionRegion", "Rear motion detection regions", help="65535 means all regions"),
    # system ([Tab3])
    _t("Tab3", "RECLED", "Recording status LED"),
    _t("Tab3", "NORMALLED", "Front security LED, normal mode"),
    _t("Tab3", "PARKINGLED", "Front security LED, parking mode"),
    _t("Tab3", "RearLED", "Rear security LED"),
    _t("Tab3", "LTELED", "LTE LED, parking mode"),
    _t("Tab3", "WifiLED", "Wi-Fi LED, parking mode"),
    _t("Tab3", "BTLED", "Bluetooth LED, parking mode"),
    _s("Tab3", "PSENSOR", "Proximity sensor", (("0", "Voice recording on/off"), ("1", "Manual recording"), ("2", "Off"))),
    *(_t("Tab3", key, f"Voice guidance: {what}") for key, what in _VOICES),
    _n("Tab3", "VOLUME", "Volume"),
    _t("Tab3", "ScheduledReboot", "Scheduled reboot"),
    CameraField("Tab3", "ScheduledRebootTime", "Scheduled reboot time", "hour"),
    _n("Tab3", "EventSpeedUnit", "Speed alert unit", help="value codes not confirmed yet"),
    _n("Tab3", "AlertLimit", "Speed alert limit"),
    _t("Tab3", "Battery", "Battery protection", help="hardwired installations only"),
    _n("Tab3", "LowvoltageTime", "Low-voltage cut-off timer", unit="h", options=(("0", "Off"),), help="hours until power-off: up to 12 on 12 V, 48 on 24 V"),
    _n("Tab3", "LowvoltageVolt", "Low-voltage cut-off, 12 V", unit="V", scale=0.01),
    _n("Tab3", "LowvoltageVoltHeavy", "Low-voltage cut-off, 24 V", unit="V", scale=0.01),
    CameraField("Tab3", "userString", "User text overlay", "text", help="up to 20 characters"),
    _n("Tab3", "AccelLimit", "Rapid acceleration alert threshold"),
    _n("Tab3", "HarshLimit", "Harsh braking alert threshold"),
    _n("Tab3", "SharpLimit", "Sharp turn alert threshold"),
    _t("Tab3", "BTPair", "Bluetooth pairing"),
    *(_n("Tab3", key, f"Driver monitoring: {what}", not_fitted=True) for key, what in _DSM),
    # wi-fi ([Wifi])
    CameraField("Wifi", "ap_ssid", "Camera hotspot name", "text"),
    CameraField("Wifi", "ap_pw", "Camera hotspot password", "password", secret=True),
    _s("Wifi", "WiFiBand", "Wi-Fi band", (("0", "5 GHz"), ("1", "2.4 GHz"))),
    _t("Wifi", "WifiSleepMode", "Wi-Fi auto turn off", help="turns Wi-Fi off after 10 minutes without use"),
    # cloud ([Cloud])
    _t("Cloud", "CloudService", "Cloud service"),
    *(
        field
        for n, suffix in ((1, ""), (2, "2"), (3, "3"))
        for field in (
            CameraField("Cloud", f"sta{suffix}_ssid", f"Home network {n} name", "text"),
            CameraField("Cloud", f"sta{suffix}_pw", f"Home network {n} password", "password", secret=True),
        )
    ),
    CameraField("Cloud", "CloudSettingVersion", "Cloud settings version", "readonly", read_only=True),
)

FIELDS: dict[tuple[str, str], CameraField] = {(f.section, f.key): f for f in _FIELDS}


def field_for(section: str, key: str) -> CameraField:
    """returns the descriptor for a key, or a plain text field for unknown keys."""
    return FIELDS.get((section, key)) or CameraField(section, key, key, "text")


def is_secret(section: str, key: str) -> bool:
    """returns whether a key holds a password."""
    return field_for(section, key).secret


def format_utc_offset(raw: str) -> str:
    """renders a signed HHMM offset ("1000", "-1100", "930") as "UTC+10:00"."""
    text = raw.strip()
    sign = "-" if text.startswith("-") else "+"
    digits = text.lstrip("+-")
    if not digits.isdigit() or len(digits) > 4:
        return raw
    hours, minutes = divmod(int(digits), 100)
    if minutes >= 60:
        return raw
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def display_value(field: CameraField, raw: str) -> str:
    """renders a raw value for reading; passwords are masked."""
    if field.secret:
        return MASK if raw else ""
    if field.widget == "toggle":
        return {"0": "Off", "1": "On"}.get(raw, raw)
    if field.key == "TimeZone":
        return format_utc_offset(raw)
    if field.widget == "hour" and raw.isdigit():
        return f"{int(raw):02d}:00"
    labels = dict(field.options)
    if raw in labels:
        return labels[raw]
    number = raw.lstrip("-")
    if field.scale != 1.0 and number.isdigit():
        return f"{int(raw) * field.scale:g} {field.unit}".strip()
    if field.unit and raw:
        return f"{raw} {field.unit}"
    return raw


@dataclasses.dataclass(frozen=True)
class FieldView:
    """one key ready for display; raw is None for passwords."""

    key: str  # "Section.key"
    label: str
    value: str
    raw: str | None
    secret: bool
    has_value: bool
    formats_card: bool
    help: str


@dataclasses.dataclass(frozen=True)
class TabView:
    """one tab's keys: described, unknown ("Other") and hardware not fitted."""

    name: str
    label: str
    section: str
    fields: tuple[FieldView, ...]
    other: tuple[FieldView, ...]
    not_fitted: tuple[FieldView, ...]


def _view(section: str, key: str, raw: str, known: bool) -> FieldView:
    field = field_for(section, key)
    label = field.label if known or section in {t.section for t in TABS} else f"{section}.{key}"
    return FieldView(
        key=f"{section}.{key}",
        label=label,
        value=safe_text(display_value(field, raw)),
        raw=None if field.secret else safe_text(raw),
        secret=field.secret,
        has_value=bool(raw),
        formats_card=field.formats_card,
        help=field.help,
    )


def build_tabs(cfile: CameraFile) -> list[TabView]:
    """groups the file's keys into the five tabs; unknown sections go to System."""
    tab_sections = {t.section for t in TABS}
    groups: dict[str, dict[str, list[FieldView]]] = {
        t.section: {"fields": [], "other": [], "not_fitted": []} for t in TABS
    }
    seen: set[tuple[str, str]] = set()
    for entry in cfile.entries:
        if (entry.section, entry.key) in seen:
            continue  # the first occurrence is what the camera uses
        seen.add((entry.section, entry.key))
        known = (entry.section, entry.key) in FIELDS
        view = _view(entry.section, entry.key, entry.value, known)
        target = entry.section if entry.section in tab_sections else "Tab3"
        if known and field_for(entry.section, entry.key).not_fitted:
            groups[target]["not_fitted"].append(view)
        elif known:
            groups[target]["fields"].append(view)
        else:
            groups[target]["other"].append(view)
    return [
        TabView(
            name=t.name,
            label=t.label,
            section=t.section,
            fields=tuple(groups[t.section]["fields"]),
            other=tuple(groups[t.section]["other"]),
            not_fitted=tuple(groups[t.section]["not_fitted"]),
        )
        for t in TABS
    ]


__all__ = [
    "FIELDS",
    "MASK",
    "TABS",
    "CameraField",
    "CameraTab",
    "FieldView",
    "TabView",
    "build_tabs",
    "display_value",
    "field_for",
    "format_utc_offset",
    "is_secret",
]
```

Black reflows the long `_FIELDS` lines at commit; that is expected. Pylint may flag `too-many-instance-attributes` on `CameraField`/`FieldView`. Add `# pylint: disable=too-many-instance-attributes` as the first line of each class body, the way `RecordingEntry` does in `viewer_index.py`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_camera_schema.py -v`
Expected: 8 passed. If `len(FIELDS) == 85` fails, compare against the fixture keys printed by `test_every_dr900x_plus_key_is_described`.

- [ ] **Step 5: Commit**

```bash
git add blackvuesync_v2/server/camera_schema.py test/test_camera_schema.py
git commit -m "feat(camera): descriptors and display rules for all 85 keys"
```

---

### Task 5: `camera` app-settings section (read timeout)

**Files:**

- Modify: `blackvuesync_v2/settings.py` (new section class, `Settings` field, `validate`, `_SECTION_FIELDS`), `blackvuesync_v2/server/settings_form.py` (specs and label), `docs/guide/configuration.md`, `CLAUDE.md` (settings table)
- Test: `test/test_settings.py`, `test/test_settings_form.py` (append)

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces: `CameraSettings(read_timeout_seconds: float = 3.0)` and `Settings.camera` (used by Task 7).

- [ ] **Step 1: Write the failing tests**

Append to `test/test_settings.py`:

```python
def test_camera_section_defaults_and_bounds() -> None:
    from blackvuesync_v2.settings import CameraSettings, Settings, _settings_from_dict

    assert Settings().camera.read_timeout_seconds == 3.0
    assert CameraSettings().validate() == []
    for bad in (0.4, 30.5):
        assert CameraSettings(read_timeout_seconds=bad).validate() == [
            "camera.read_timeout_seconds must be between 0.5 and 30"
        ]
    # files written before the section existed load the default
    assert _settings_from_dict({"version": 2}).camera.read_timeout_seconds == 3.0
```

Append to `test/test_settings_form.py`:

```python
def test_camera_section_has_the_read_timeout_field() -> None:
    from blackvuesync_v2.server.settings_form import SECTION_FIELD_SPECS, SECTION_LABELS

    assert SECTION_LABELS["camera"] == "Camera access"
    (spec,) = SECTION_FIELD_SPECS["camera"]
    assert (spec.name, spec.widget, spec.data_type) == (
        "read_timeout_seconds",
        "number",
        "number",
    )
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_settings.py -k camera test/test_settings_form.py -k camera -v`
Expected: FAIL (`ImportError: cannot import name 'CameraSettings'`, `KeyError: 'camera'`).

- [ ] **Step 3: Implement**

In `blackvuesync_v2/settings.py`, after `class ViewerSettings`:

```python
@dataclass(frozen=True)
class CameraSettings(_Section):
    """access to the dashcam's own settings (config.ini)."""

    TIER: ClassVar[PropagationTier] = "immediate"

    # seconds each camera settings read waits; short, so an absent car does not
    # stall the settings page
    read_timeout_seconds: float = 3.0

    def _validate_values(self) -> list[str]:
        """validates camera settings; returns a list of error strings."""
        errors: list[str] = []
        if not 0.5 <= self.read_timeout_seconds <= 30:
            errors.append("camera.read_timeout_seconds must be between 0.5 and 30")
        return errors
```

In `class Settings`, after the `viewer` field:

```python
    camera: CameraSettings = field(default_factory=CameraSettings)
```

In `Settings.validate`, after `errors.extend(self.viewer.validate())`:

```python
        errors.extend(self.camera.validate())
```

In `_SECTION_FIELDS`, after `"viewer": ViewerSettings,`:

```python
    "camera": CameraSettings,
```

In `blackvuesync_v2/server/settings_form.py`, in `SECTION_FIELD_SPECS` after the `"viewer"` entry:

```python
    "camera": (
        FieldSpec(
            "read_timeout_seconds",
            "Camera read timeout (seconds)",
            "number",
            "number",
            help="how long the Camera panes wait before showing the last-known settings",
        ),
    ),
```

and in `SECTION_LABELS` after `"viewer": "Viewer",`:

```python
    "camera": "Camera access",
```

- [ ] **Step 4: Run the tests, then the whole settings suites**

Run: `venv/bin/python -m pytest test/test_settings.py test/test_settings_form.py test/test_settings_page.py test/test_routes_api_settings.py -q`
Expected: all pass. The `test_build_sections_pairs_values_and_tier` dict needs no change, because `build_sections` reads missing sections as empty.

- [ ] **Step 5: Document the knob**

In `docs/guide/configuration.md`, after the `### Viewer` table, add:

```markdown
### Camera access

| Field | Default | Description |
| --- | --- | --- |
| `read_timeout_seconds` | `3` | How long the Camera settings panes wait for the dashcam (0.5-30). When it doesn't answer in time, they show the last-known settings. |
```

In `CLAUDE.md`:

- In the settings table, add a row after `viewer`: `| camera | immediate | read_timeout_seconds |`.
- Change "Settings are organized into eleven frozen-dataclass sections" to "twelve".

- [ ] **Step 6: Commit**

```bash
git add blackvuesync_v2/settings.py blackvuesync_v2/server/settings_form.py \
  test/test_settings.py test/test_settings_form.py docs/guide/configuration.md CLAUDE.md
git commit -m "feat(settings): camera access section with a read timeout knob"
```

---

### Task 6: Stop `/api/dashcam/info` and the dashboard card returning passwords

**Files:**

- Modify: `blackvuesync_v2/server/routes/api_dashcam.py`
- Test: `test/test_routes_api_dashcam.py`

**Interfaces:**

- Consumes: `is_secret` (Task 4), `parse_version` (Task 2).
- Produces: `_compute_dashcam_info` keeps its return shape. Secret values become `"***"`, and `firmware` becomes `VersionInfo.description`.

- [ ] **Step 1: Write the failing tests**

Append to `test/test_routes_api_dashcam.py` (it already defines `_fake_response`, `logged_in_client`):

```python
FIXTURES = Path(__file__).parent / "fixtures" / "camera"


def _camera_urlopen(url: str, *_args: Any, **_kwargs: Any) -> Any:
    name = "dr900x-plus-config.ini" if url.endswith("config.ini") else "dr900x-plus-version.bin"
    return _fake_response((FIXTURES / name).read_bytes())


def test_info_masks_passwords_and_describes_the_model(
    logged_in_client: Any,
) -> None:
    client, _store = logged_in_client
    with patch("urllib.request.urlopen", _camera_urlopen):
        body = client.get("/api/dashcam/info").get_data(as_text=True)
    data = json.loads(body)
    assert data["firmware"] == "DR900X Plus · fw 1.015"
    assert data["config"]["Cloud"]["sta_pw"] == "***"
    assert data["config"]["Wifi"]["ap_pw"] == "***"
    assert data["config"]["Cloud"]["sta_ssid"] == "DemoHome"
    assert "1E1BC3E1" not in body
    assert "14F26B26" not in body


def test_info_card_never_contains_a_password(logged_in_client: Any) -> None:
    client, _store = logged_in_client
    with patch("urllib.request.urlopen", _camera_urlopen):
        html = client.get("/hx/dashcam-info-card").get_data(as_text=True)
    assert "DR900X Plus · fw 1.015" in html
    for line in (FIXTURES / "dr900x-plus-config.ini").read_text().splitlines():
        if "_pw=" in line:
            assert line.split("=", 1)[1] not in html
```

Replace the existing `test_parse_version_bin_strips_control_chars` with:

```python
    def test_firmware_string_strips_control_chars(self) -> None:
        from blackvuesync_v2.server.camera_config import parse_version

        assert parse_version(b"DR900X-2.013\x00\x01").description == "DR900X-2.013"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_routes_api_dashcam.py -v`
Expected: the two new tests FAIL (the passwords are present; the firmware string is the whole version.bin text).

- [ ] **Step 3: Implement**

In `blackvuesync_v2/server/routes/api_dashcam.py`:

- Delete `_parse_version_bin`.
- Add the imports:

```python
from blackvuesync_v2.server.camera_config import parse_version
from blackvuesync_v2.server.camera_schema import is_secret
```

- Add above `_config_preview`:

```python
_MASKED = "***"


def _mask_secrets(config: dict[str, dict[str, str]]) -> dict[str, dict[str, str]]:
    """hides wi-fi passwords; config.ini encrypts them with a public key."""
    return {
        section: {
            key: _MASKED if value and is_secret(section, key) else value
            for key, value in keys.items()
        }
        for section, keys in config.items()
    }
```

In `_compute_dashcam_info`, replace the final block from `config = ...` to the
`return` with:

```python
    config = _mask_secrets(_parse_config_ini(config_raw)) if config_raw else {}
    firmware = (
        parse_version(firmware_raw.encode("utf-8")).description
        if firmware_raw
        else None
    )
    return {
        _KEY_AVAILABLE: True,
        "address": address,
        "firmware": firmware,
        "config": config,
        "setting_count": sum(len(keys) for keys in config.values()),
    }
```

Update the module docstring's last sentence to: "secret values (wi-fi passwords) are masked; writes live in the camera settings routes."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_routes_api_dashcam.py test/test_routes_hx_dashboard.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add blackvuesync_v2/server/routes/api_dashcam.py test/test_routes_api_dashcam.py
git commit -m "fix(dashcam): mask wi-fi passwords in dashcam info and its card"
```

---

### Task 7: Camera JSON API (`/api/camera/config`, `/api/camera/secret`)

**Files:**

- Create: `blackvuesync_v2/server/routes/api_camera.py`
- Modify: `blackvuesync_v2/server/__init__.py` (register the blueprint)
- Test: `test/test_routes_api_camera.py`

**Interfaces:**

- Consumes: `fetch`, `diff`, `CameraStore`, `CameraRead`, `Change`, `safe_text` (Tasks 2-3); `build_tabs`, `field_for`, `is_secret`, `MASK` (Task 4); `decrypt_password`, `UndecodablePasswordError` (Task 1); `Settings.camera` (Task 5).
- Produces (used by Task 8):
  - `CameraState(online: bool, read: CameraRead | None, changed: tuple[Change, ...])`
  - `camera_store(store: SettingsStore) -> CameraStore`
  - `camera_state(store: SettingsStore) -> CameraState`
  - `change_views(changes) -> list[dict[str, str]]`
  - `state_body(state: CameraState) -> dict[str, object]`
  - routes `GET /api/camera/config` and `GET /api/camera/secret?key=Section.key`

- [ ] **Step 1: Write the failing tests**

Create `test/test_routes_api_camera.py`:

```python
"""tests for the /api/camera json endpoints."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.server import camera_config, create_app
from blackvuesync_v2.server.auth import SESSION_VERSION_KEY, hash_password, session_version
from blackvuesync_v2.settings import SettingsStore

FIXTURES = Path(__file__).parent / "fixtures" / "camera"
CONFIG = (FIXTURES / "dr900x-plus-config.ini").read_bytes()
VERSION = (FIXTURES / "dr900x-plus-version.bin").read_bytes()


class FakeCamera:
    """stands in for camera_config._get; files maps a path to bytes or None."""

    def __init__(self) -> None:
        self.files: dict[str, bytes | None] = {
            "/Config/config.ini": CONFIG,
            "/Config/version.bin": VERSION,
        }

    def get(self, url: str, timeout: float) -> bytes | None:
        assert timeout == 3.0
        return self.files.get(url.split("192.0.2.10", 1)[1])


@pytest.fixture()
def camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    fake = FakeCamera()
    monkeypatch.setattr(camera_config, "_get", fake.get)
    return fake


@pytest.fixture()
def client(tmp_path: Path):  # type: ignore[no-untyped-def]
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password("pw-1234-test")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=pw_hash)
        )
    )
    app = create_app(store, testing=True)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(pw_hash)
    return c


def test_config_returns_tabs_with_masked_passwords(client: Any, camera: FakeCamera) -> None:
    resp = client.get("/api/camera/config")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    data = json.loads(text)
    assert (data["available"], data["online"]) == (True, True)
    assert data["model"] == "DR900X Plus" and data["firmware"] == "1.015"
    tabs = {t["name"]: t for t in data["tabs"]}
    cloud = {f["key"]: f for f in tabs["cloud"]["fields"]}
    assert cloud["Cloud.sta_pw"]["value"] == "•" * 8
    assert cloud["Cloud.sta_pw"]["raw"] is None
    assert "1E1BC3E1" not in text and "DemoHome-123" not in text
    assert data["changed"] == []


def test_config_reports_changes_since_the_last_read(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    camera.files["/Config/config.ini"] = CONFIG.replace(b"VOLUME=5", b"VOLUME=4").replace(
        b"sta_ssid=DemoHome", b"sta_ssid=NewHome"
    )
    data = client.get("/api/camera/config").get_json()
    assert data["changed"] == [
        {"key": "Tab3.VOLUME", "label": "Volume", "from": "5", "to": "4"},
        {"key": "Cloud.sta_ssid", "label": "Home network 1 name", "from": "DemoHome", "to": "NewHome"},
    ]


def test_password_changes_are_reported_without_values(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    camera.files["/Config/config.ini"] = CONFIG.replace(b"ap_pw=", b"ap_pw=0")
    data = client.get("/api/camera/config").get_json()
    assert data["changed"] == [
        {"key": "Wifi.ap_pw", "label": "Camera hotspot password", "from": "changed", "to": "changed"}
    ]


def test_offline_serves_the_snapshot(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    camera.files = {}
    data = client.get("/api/camera/config").get_json()
    assert (data["available"], data["online"]) == (True, False)
    assert data["changed"] == []


def test_never_reached_reports_unavailable(client: Any, camera: FakeCamera) -> None:
    camera.files = {}
    assert client.get("/api/camera/config").get_json() == {"available": False, "online": False}


def test_unwritable_snapshot_still_serves_live_settings(
    client: Any, camera: FakeCamera, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """review focus 3: a read-only settings directory is not a 500."""
    (tmp_path / "camera").write_text("not a directory", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        data = client.get("/api/camera/config").get_json()
    assert data["online"] is True
    assert "could not save the camera snapshot" in caplog.text


def test_secret_returns_one_decrypted_password(client: Any, camera: FakeCamera) -> None:
    client.get("/api/camera/config")
    resp = client.get("/api/camera/secret?key=Cloud.sta_pw")
    assert resp.status_code == 200
    assert resp.get_json() == {"key": "Cloud.sta_pw", "value": "DemoHome-123"}
    assert resp.headers["Cache-Control"] == "no-store"


def test_secret_refuses_other_keys_and_no_snapshot(client: Any, camera: FakeCamera) -> None:
    assert client.get("/api/camera/secret?key=Cloud.sta_pw").status_code == 404
    client.get("/api/camera/config")
    for key in ("Cloud.sta_ssid", "Tab1.TimeZone", "", "Cloud", "Wifi.ap_pw/x"):
        assert client.get(f"/api/camera/secret?key={key}").status_code == 404


def test_secret_that_does_not_decrypt_is_a_422(client: Any, camera: FakeCamera) -> None:
    """review focus 4: never show garbage as a password."""
    camera.files["/Config/config.ini"] = CONFIG.replace(
        b"ap_pw=", b"ap_pw=" + b"00" * 32 + b"\nIgnored="
    )
    client.get("/api/camera/config")
    resp = client.get("/api/camera/secret?key=Wifi.ap_pw")
    assert resp.status_code == 422
    assert resp.get_json()["code"] == "UNDECODABLE_PASSWORD"


def test_camera_api_requires_login(tmp_path: Path, camera: FakeCamera) -> None:
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    store.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=hash_password("pw-1234-test"))
        )
    )
    anon = create_app(store, testing=True).test_client()
    assert anon.get("/api/camera/config").status_code == 401
    assert anon.get("/api/camera/secret?key=Cloud.sta_pw").status_code == 401
```

(In `test_secret_that_does_not_decrypt_is_a_422`, the replacement makes `ap_pw` exactly 64 zeros and pushes the original value onto an ignored line.)

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_routes_api_camera.py -v`
Expected: FAIL with 404s (the routes don't exist yet).

- [ ] **Step 3: Implement**

Create `blackvuesync_v2/server/routes/api_camera.py`:

```python
"""api camera routes: the dashcam's own settings (config.ini), read-only.

every read goes to the camera with camera.read_timeout_seconds; a successful
read replaces the private snapshot and reports what changed since the
previous one. when the camera is away the snapshot is served instead.
"""

from __future__ import annotations

import dataclasses
import json
import logging

from flask import Blueprint, Response, abort, current_app, request

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.camera_config import (
    CameraRead,
    CameraStore,
    Change,
    diff,
    fetch,
    safe_text,
)
from blackvuesync_v2.server.camera_crypto import (
    UndecodablePasswordError,
    decrypt_password,
)
from blackvuesync_v2.server.camera_schema import build_tabs, field_for, is_secret
from blackvuesync_v2.settings import Settings, SettingsStore

logger = logging.getLogger(__name__)

api_camera_bp = Blueprint("api_camera_bp", __name__, url_prefix="/api/camera")

_MIME_JSON = "application/json"
_CHANGED = "changed"  # stands in for a password in change reports


@dataclasses.dataclass(frozen=True)
class CameraState:
    """the camera's settings as last seen, and whether this read was live."""

    online: bool
    read: CameraRead | None
    changed: tuple[Change, ...]


def camera_store(store: SettingsStore) -> CameraStore:
    """returns the snapshot store next to settings.json."""
    return CameraStore(store.path.parent / "camera")


def _read_timeout(settings: Settings) -> float:
    return min(max(settings.camera.read_timeout_seconds, 0.5), 30.0)


def camera_state(store: SettingsStore) -> CameraState:
    """reads the camera live; falls back to the snapshot when it is away."""
    settings = store.get()
    snapshots = camera_store(store)
    previous = snapshots.load()
    live = fetch(settings.connection.address, _read_timeout(settings))
    if live is None:
        return CameraState(online=False, read=previous, changed=())
    try:
        snapshots.save(live)
    except OSError as error:
        logger.warning("could not save the camera snapshot: %s", error)
    changed = tuple(diff(previous.config, live.config)) if previous else ()
    return CameraState(online=True, read=live, changed=changed)


def change_views(changes: tuple[Change, ...]) -> list[dict[str, str]]:
    """describes changes for display; passwords show only that they changed."""
    views: list[dict[str, str]] = []
    for change in changes:
        secret = is_secret(change.section, change.key)
        views.append(
            {
                "key": f"{change.section}.{change.key}",
                "label": field_for(change.section, change.key).label,
                "from": _CHANGED if secret else safe_text(change.before or ""),
                "to": _CHANGED if secret else safe_text(change.after or ""),
            }
        )
    return views


def state_body(state: CameraState) -> dict[str, object]:
    """returns the json body for a camera state; passwords stay masked."""
    if state.read is None:
        return {"available": False, "online": False}
    read = state.read
    return {
        "available": True,
        "online": state.online,
        "read_at": read.read_at.isoformat(),
        "model": read.version.model,
        "firmware": read.version.firmware,
        "tabs": [dataclasses.asdict(tab) for tab in build_tabs(read.config)],
        "changed": change_views(state.changed),
    }


def _json(body: object, status: int = 200) -> Response:
    return Response(json.dumps(body), status=status, mimetype=_MIME_JSON)


@api_camera_bp.route("/config", methods=["GET"])
@login_required
def config() -> Response:
    """returns the camera settings by tab, read live when the camera answers."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    return _json(state_body(camera_state(store)))


@api_camera_bp.route("/secret", methods=["GET"])
@login_required
def secret() -> Response:
    """returns one decrypted password from the snapshot (the eye icon)."""
    section, _, key = request.args.get("key", "").partition(".")
    if not is_secret(section, key):
        abort(404)
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    read = camera_store(store).load()
    stored = read.config.value(section, key) if read else None
    if stored is None:
        abort(404)
    try:
        value = decrypt_password(stored)
    except UndecodablePasswordError:
        return _json(
            {"code": "UNDECODABLE_PASSWORD", "error": "this password can't be shown"},
            status=422,
        )
    resp = _json({"key": f"{section}.{key}", "value": value})
    resp.headers["Cache-Control"] = "no-store"
    return resp


__all__ = [
    "CameraState",
    "api_camera_bp",
    "camera_state",
    "camera_store",
    "change_views",
    "state_body",
]
```

In `blackvuesync_v2/server/__init__.py`, add the import next to the other route imports:

```python
    from blackvuesync_v2.server.routes.api_camera import api_camera_bp
```

and register it after `app.register_blueprint(api_auth_bp)`:

```python
    app.register_blueprint(api_camera_bp)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_routes_api_camera.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add blackvuesync_v2/server/routes/api_camera.py blackvuesync_v2/server/__init__.py \
  test/test_routes_api_camera.py
git commit -m "feat(camera): read-only camera settings api with password reveal"
```

---

### Task 8: Camera panes in the Settings page

**Files:**

- Create: `blackvuesync_v2/server/routes/hx_camera.py`, `blackvuesync_v2/server/templates/_partials/camera_panes.html`
- Modify: `blackvuesync_v2/server/__init__.py`, `blackvuesync_v2/server/routes/ui.py` (`settings`), `blackvuesync_v2/server/templates/settings.html`, `blackvuesync_v2/server/static/js/settings.js`, `blackvuesync_v2/server/static/css/settings.css`, `blackvuesync_v2/server/templates/_partials/dashcam_info_card.html`, `blackvuesync_v2/server/static/css/dashboard.css`
- Test: `test/test_routes_hx_camera.py`

**Interfaces:**

- Consumes: `camera_state`, `change_views` (Task 7); `build_tabs`, `TABS` (Task 4).
- Produces:
  - Nav items `data-section-nav="camera-<tab.name>"`, with panes `data-pane="camera-<tab.name>"` inside `#camera-panes`.
  - Reveal buttons `[data-reveal="<Section.key>"]`, each paired with `[data-secret-value="<Section.key>"]`.
  - Field rows `.camera-field`. Task 9's browser tests rely on these selectors.

- [ ] **Step 1: Write the failing tests**

Create `test/test_routes_hx_camera.py`:

```python
"""tests for the /hx/camera/panes fragment and the settings page nav."""

from __future__ import annotations

from typing import Any

from test_routes_api_camera import CONFIG, FakeCamera, camera, client  # noqa: F401

MASK = "•" * 8


def test_panes_render_every_tab_read_only(client: Any, camera: FakeCamera) -> None:
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    for name in ("basic", "sensitivity", "system", "wifi", "cloud"):
        assert f'data-pane="camera-{name}"' in html
    assert "DR900X Plus · fw 1.015" in html
    assert "UTC+10:00" in html and "Highest (Extreme)" in html
    assert "erases camera recordings" in html
    assert 'data-reveal="Cloud.sta_pw"' in html and MASK in html
    assert "1E1BC3E1" not in html and "DemoHome-123" not in html
    assert "Driver monitoring" in html  # collapsed group, still listed


def test_panes_show_the_offline_banner(client: Any, camera: FakeCamera) -> None:
    client.get("/hx/camera/panes")
    camera.files = {}
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "Camera offline: showing settings read at" in html


def test_panes_before_the_first_read(client: Any, camera: FakeCamera) -> None:
    camera.files = {}
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "hasn't been reached yet" in html


def test_panes_list_changes_on_the_camera(client: Any, camera: FakeCamera) -> None:
    client.get("/hx/camera/panes")
    camera.files["/Config/config.ini"] = CONFIG.replace(b"VOLUME=5", b"VOLUME=4")
    html = client.get("/hx/camera/panes").get_data(as_text=True)
    assert "changed on the camera" in html and "Tab3.VOLUME" in html


def test_non_utf8_ssid_renders(client: Any, camera: FakeCamera) -> None:
    """review focus 1: a latin-1 byte must not break the fragment."""
    camera.files["/Config/config.ini"] = CONFIG.replace(b"DemoHome", b"Caf\xe9")
    resp = client.get("/hx/camera/panes")
    assert resp.status_code == 200
    assert "Caf�" in resp.get_data(as_text=True)


def test_dashboard_card_links_to_the_camera_settings(
    client: Any, camera: FakeCamera, monkeypatch: Any
) -> None:
    import urllib.request

    def offline(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("car away")

    monkeypatch.setattr(urllib.request, "urlopen", offline)
    html = client.get("/hx/dashcam-info-card").get_data(as_text=True)
    assert 'href="/settings#camera-basic"' in html


def test_settings_page_has_the_camera_nav(client: Any, camera: FakeCamera) -> None:
    html = client.get("/settings").get_data(as_text=True)
    assert 'data-section-nav="camera-basic"' in html
    assert 'id="camera-panes"' in html and 'hx-get="/hx/camera/panes"' in html
    assert "Camera access" in html  # the app-settings section from task 5
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_routes_hx_camera.py -v`
Expected: FAIL (404 for `/hx/camera/panes`; nav missing).

- [ ] **Step 3: Implement the fragment route**

Create `blackvuesync_v2/server/routes/hx_camera.py`:

```python
"""htmx fragment for the camera settings panes (read-only in this release)."""

from __future__ import annotations

from flask import Blueprint, Response, current_app, render_template

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.camera_schema import TABS, build_tabs
from blackvuesync_v2.server.routes.api_camera import camera_state, change_views
from blackvuesync_v2.settings import SettingsStore

hx_camera_bp = Blueprint("hx_camera_bp", __name__, url_prefix="/hx/camera")


@hx_camera_bp.route("/panes", methods=["GET"])
@login_required
def panes() -> Response:
    """renders one pane per camera tab from a live read, else the snapshot."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    state = camera_state(store)
    read = state.read
    html = render_template(
        "_partials/camera_panes.html",
        tabs=build_tabs(read.config) if read else [],
        placeholder_tabs=TABS,
        online=state.online,
        description=read.version.description if read else "",
        read_at=read.read_at.astimezone().strftime("%Y-%m-%d %H:%M") if read else "",
        changed=change_views(state.changed),
    )
    return Response(html, mimetype="text/html")
```

Register it in `blackvuesync_v2/server/__init__.py` (import with the others; `app.register_blueprint(hx_camera_bp)` after `hx_dashboard_bp`).

- [ ] **Step 4: Implement the fragment template**

Create `blackvuesync_v2/server/templates/_partials/camera_panes.html`:

```html
{# camera settings panes, one per tab; read-only until editing ships. the
   refresh button re-reads the camera and swaps this whole fragment. #}
{% macro rows(fields) %}
  <dl class="camera-fields">
    {% for f in fields %}
      <div class="camera-field">
        <dt>
          {{ f.label }}
          {% if f.formats_card %}<span class="badge badge-format">erases camera recordings</span>{% endif %}
        </dt>
        <dd>
          {% if f.secret %}
            <span class="camera-secret" data-secret-value="{{ f.key }}">{{ f.value }}</span>
            {% if f.has_value %}
              <button type="button" class="camera-reveal" data-reveal="{{ f.key }}"
                      aria-label="Show password" aria-pressed="false" title="Show password">
                <svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d="M12 5C6.5 5 2.7 9.1 1.5 12c1.2 2.9 5 7 10.5 7s9.3-4.1 10.5-7C21.3 9.1 17.5 5 12 5zm0 11.5A4.5 4.5 0 1 1 12 7.5a4.5 4.5 0 0 1 0 9zm0-7a2.5 2.5 0 1 0 0 5 2.5 2.5 0 0 0 0-5z"/></svg>
              </button>
            {% endif %}
          {% else %}
            {{ f.value }}
          {% endif %}
          <span class="camera-raw">{{ f.key }}{% if f.raw is not none %} = {{ f.raw }}{% endif %}</span>
          {% if f.help %}<span class="field-help">{{ f.help }}</span>{% endif %}
        </dd>
      </div>
    {% endfor %}
  </dl>
{% endmacro %}

{% if tabs %}
  {% for tab in tabs %}
    <section class="settings-pane camera-pane" data-pane="camera-{{ tab.name }}">
      <header class="settings-pane-header">
        <h2 class="settings-pane-title">Camera &rsaquo; {{ tab.label }}</h2>
        <button type="button" class="button button-secondary button-sm"
                hx-get="/hx/camera/panes" hx-target="#camera-panes" hx-swap="innerHTML">Refresh</button>
      </header>
      <p class="camera-status">{{ description }} · {% if online %}online{% else %}offline{% endif %}</p>
      {% if not online %}
        <div class="alert alert-warning">Camera offline: showing settings read at {{ read_at }}.</div>
      {% endif %}
      {% if changed %}
        <div class="alert alert-info">
          {{ changed|length }} setting{{ "s" if changed|length != 1 }} changed on the camera since the last read:
          {% for c in changed %}{{ c.label }} ({{ c.key }}) {{ c["from"] }} → {{ c["to"] }}{{ "; " if not loop.last }}{% endfor %}
        </div>
      {% endif %}
      {{ rows(tab.fields) }}
      {% if tab.other %}
        <details class="camera-group" open><summary>Other</summary>{{ rows(tab.other) }}</details>
      {% endif %}
      {% if tab.not_fitted %}
        <details class="camera-group"><summary>Other (not fitted on this camera)</summary>{{ rows(tab.not_fitted) }}</details>
      {% endif %}
      <p class="field-help">Editing camera settings arrives in a later release.</p>
    </section>
  {% endfor %}
{% else %}
  {% for tab in placeholder_tabs %}
    <section class="settings-pane camera-pane" data-pane="camera-{{ tab.name }}">
      <header class="settings-pane-header">
        <h2 class="settings-pane-title">Camera &rsaquo; {{ tab.label }}</h2>
        <button type="button" class="button button-secondary button-sm"
                hx-get="/hx/camera/panes" hx-target="#camera-panes" hx-swap="innerHTML">Refresh</button>
      </header>
      <p class="camera-status">The camera hasn't been reached yet. Its settings appear here once it's online.</p>
    </section>
  {% endfor %}
{% endif %}
```

- [ ] **Step 5: Wire the Settings page**

In `blackvuesync_v2/server/routes/ui.py`, import `TABS` and pass it:

```python
from blackvuesync_v2.server.camera_schema import TABS as CAMERA_TABS
```

```python
    return render_template(
        "settings.html",
        page="settings",
        sections=build_sections(settings_dict),
        camera_tabs=CAMERA_TABS,
    )
```

In `blackvuesync_v2/server/templates/settings.html`, change the nav and panes to:

```html
  <nav class="settings-nav">
    <div class="settings-nav-heading">App</div>
    {% for s in sections %}
      <button type="button" class="settings-nav-item" data-section-nav="{{ s.name }}"
              @click="select">{{ s.label }}</button>
    {% endfor %}
    <div class="settings-nav-heading">Camera</div>
    {% for t in camera_tabs %}
      <button type="button" class="settings-nav-item" data-section-nav="camera-{{ t.name }}"
              @click="select">{{ t.label }}</button>
    {% endfor %}
  </nav>
```

and, as the last child of `<div class="settings-panes">` (after the `{% endfor %}` of the app sections):

```html
    <div id="camera-panes" hx-get="/hx/camera/panes" hx-trigger="load" hx-swap="innerHTML">
      {% for t in camera_tabs %}
        <section class="settings-pane camera-pane" data-pane="camera-{{ t.name }}">
          <header class="settings-pane-header">
            <h2 class="settings-pane-title">Camera &rsaquo; {{ t.label }}</h2>
          </header>
          <p class="camera-status">Reading the camera…</p>
        </section>
      {% endfor %}
    </div>
```

Confirm htmx is loaded on the Settings page: `grep -n htmx blackvuesync_v2/server/templates/base.html` must show the `htmx.min.js` script tag. It's in `base.html`, so every page has it.

- [ ] **Step 6: Reveal and re-activate in `settings.js`**

In `blackvuesync_v2/server/static/js/settings.js`, add near the top, after `TOAST_MS`:

```js
const MASK = "•".repeat(8);
```

In the `settingsPage` component:

- Add a `current: "",` property before `init()`.
- Extend `init()` and `activate()`, and add `toggleSecret`:

```js
    init() {
      // without js, all panes show (one scroll); js-nav switches to single-pane.
      this.$root.classList.add("js-nav");
      // a link such as /settings#camera-basic opens that section
      const wanted = location.hash.slice(1);
      const known =
        wanted && this.$root.querySelector(`[data-section-nav="${CSS.escape(wanted)}"]`);
      this.activate(known ? wanted : this.$root.dataset.initial || "");
      // the camera panes arrive and refresh via htmx; reveal buttons are
      // delegated, and a swap re-applies the active pane.
      this.$root.addEventListener("click", (ev) => {
        const button = ev.target.closest("[data-reveal]");
        if (button) this.toggleSecret(button);
      });
      document.body.addEventListener("htmx:afterSwap", (ev) => {
        if (ev.target.id === "camera-panes") this.activate(this.current);
      });
    },

    activate(section) {
      this.current = section;
      this.$root.querySelectorAll("[data-pane]").forEach((p) => {
        p.classList.toggle("is-active", p.dataset.pane === section);
      });
      this.$root.querySelectorAll("[data-section-nav]").forEach((n) => {
        n.classList.toggle("active", n.dataset.sectionNav === section);
      });
    },

    // shows or re-masks one camera password; the value is fetched per click
    // and dropped again when hidden.
    async toggleSecret(button) {
      const key = button.dataset.reveal;
      const target = this.$root.querySelector(`[data-secret-value="${key}"]`);
      if (!target) return;
      if (button.getAttribute("aria-pressed") === "true") {
        target.textContent = MASK;
        button.setAttribute("aria-pressed", "false");
        button.setAttribute("aria-label", "Show password");
        button.title = "Show password";
        return;
      }
      let resp;
      try {
        resp = await fetch("/api/camera/secret?key=" + encodeURIComponent(key), {
          headers: { Accept: "application/json" },
        });
      } catch {
        target.textContent = "can't be shown right now";
        return;
      }
      const data = await readJson(resp);
      if (isAuthFailure(resp, data)) {
        redirectToLogin();
        return;
      }
      if (resp.status !== 200 || !data) {
        target.textContent = data?.error || "can't be shown";
        return;
      }
      target.textContent = data.value;
      button.setAttribute("aria-pressed", "true");
      button.setAttribute("aria-label", "Hide password");
      button.title = "Hide password";
    },
```

(The existing `activate` body is replaced by the version above, which only adds `this.current = section;`.)

- [ ] **Step 7: Link from the dashboard card**

In `blackvuesync_v2/server/templates/_partials/dashcam_info_card.html`, add as
the last child of the card `<div>` (after the `{% endif %}` that closes the
state branches):

```html
  <a class="camera-link" href="/settings#camera-basic">View camera settings</a>
```

Append to `blackvuesync_v2/server/static/css/dashboard.css` (plain links have
no app colour, so the token must be explicit for the contrast audit):

```css
/* link from the dashcam info card to settings > camera */
.camera-link { display: inline-block; margin-top: var(--space-2); font-size: var(--text-footnote);
  color: var(--color-accent-text); }
```

- [ ] **Step 8: Styles**

Append to `blackvuesync_v2/server/static/css/settings.css`:

```css
/* nav group headings (App / Camera) */
.settings-nav-heading { font-size: var(--text-caption1); font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.04em; color: var(--color-label-secondary); padding: var(--space-3) var(--space-3) var(--space-1); }

/* camera settings panes (read-only rows) */
.camera-status { font-size: var(--text-footnote); color: var(--color-label-secondary); margin-bottom: var(--space-3); }
.camera-fields { display: grid; gap: var(--space-2); }
.camera-field { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: var(--space-4);
  padding: var(--space-2) 0; border-bottom: 1px solid var(--color-separator); }
.camera-field dt { color: var(--color-label); }
.camera-field dd { color: var(--color-label); overflow-wrap: anywhere; }
.camera-raw { display: block; font-size: var(--text-caption1); color: var(--color-label-secondary); }
.camera-reveal { display: inline-flex; align-items: center; justify-content: center; min-width: 32px; min-height: 32px;
  margin-left: var(--space-1); border: 0; border-radius: var(--radius-sm); background: transparent;
  color: var(--color-accent-text); cursor: pointer; vertical-align: middle; }
.camera-reveal:focus-visible { outline: 2px solid var(--color-accent); outline-offset: 2px; }
.camera-reveal[aria-pressed="true"] { background: var(--color-fill); }
.camera-reveal svg { width: 18px; height: 18px; fill: currentColor; }
.badge-format { display: inline-block; margin-left: var(--space-1); padding: 0 var(--space-2);
  border-radius: var(--radius-sm); font-size: var(--text-caption1); font-weight: 600;
  background: color-mix(in srgb, var(--color-error) 16%, transparent); color: var(--color-error-text); }
.camera-group { margin-top: var(--space-3); }
.camera-group > summary { cursor: pointer; color: var(--color-label-secondary); }
@media (max-width: 720px) { .camera-field { grid-template-columns: 1fr; gap: var(--space-1); } }
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_routes_hx_camera.py test/test_settings_page.py test/test_routes_ui.py -v`
Expected: all pass. `test_routes_hx_camera.py` imports fixtures from `test_routes_api_camera`. This works because pytest's rootdir import mode puts `test/` on `sys.path`; ruff's F401/F811 are silenced by the `noqa`.

- [ ] **Step 10: Commit**

```bash
git add blackvuesync_v2/server/templates/_partials/dashcam_info_card.html \
  blackvuesync_v2/server/static/css/dashboard.css \
  blackvuesync_v2/server/routes/hx_camera.py blackvuesync_v2/server/__init__.py \
  blackvuesync_v2/server/routes/ui.py blackvuesync_v2/server/templates/settings.html \
  blackvuesync_v2/server/templates/_partials/camera_panes.html \
  blackvuesync_v2/server/static/js/settings.js blackvuesync_v2/server/static/css/settings.css \
  test/test_routes_hx_camera.py
git commit -m "feat(camera): read-only camera panes in the settings page"
```

---

### Task 9: Browser tests (reveal, offline, contrast)

**Files:**

- Modify: `test/e2e/conftest.py` (fake camera fixture), `test/e2e/test_contrast.py` (audit the camera panes with real content)
- Create: `test/e2e/test_camera_settings.py`

**Interfaces:**

- Consumes: `live_server` (existing; `.app`, `.url`); the selectors from Task 8.
- Produces: the `fake_camera` fixture, yielding a `FakeCameraServer` with `.address: str` and `.stop()`.

- [ ] **Step 1: Add the fake camera fixture**

Append to `test/e2e/conftest.py`:

```python
import http.server
from pathlib import Path as _Path

_CAMERA_FIXTURES = _Path(__file__).parent.parent / "fixtures" / "camera"


class FakeCameraServer:
    """serves the fixture config.ini and version.bin like a dashcam."""

    def __init__(self) -> None:
        files = {
            "/Config/config.ini": (_CAMERA_FIXTURES / "dr900x-plus-config.ini").read_bytes(),
            "/Config/version.bin": (_CAMERA_FIXTURES / "dr900x-plus-version.bin").read_bytes(),
        }

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = files.get(self.path.split("?")[0])
                self.send_response(200 if body is not None else 404)
                self.send_header("Content-Length", str(len(body or b"")))
                self.end_headers()
                self.wfile.write(body or b"")

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
```

(`dataclasses`, `threading`, `Any` and `pytest` are already imported in that file; add any that are missing.)

- [ ] **Step 2: Write the browser tests**

Create `test/e2e/test_camera_settings.py`:

```python
"""browser tests for the read-only camera settings panes."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("playwright.sync_api")

from playwright.sync_api import Page, expect  # noqa: E402

pytestmark = pytest.mark.e2e

MASK = "•" * 8


def _open_camera_tab(page: Page, base: str, tab: str) -> None:
    page.goto(f"{base}/login")
    page.fill('input[name="username"]', "admin")
    page.fill('input[name="password"]', "pw-1234-test")
    page.click('button[type="submit"]')
    page.goto(f"{base}/settings")
    expect(page.locator("#camera-panes .camera-field").first).to_be_attached()
    page.locator(f'[data-section-nav="camera-{tab}"]').click()


def test_reveal_shows_and_hides_one_password(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "cloud")
    value = page.locator('[data-secret-value="Cloud.sta_pw"]')
    button = page.locator('[data-reveal="Cloud.sta_pw"]')
    expect(value).to_have_text(MASK)
    expect(button).to_have_attribute("aria-label", "Show password")
    button.click()
    expect(value).to_have_text("DemoHome-123")
    expect(button).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-secret-value="Cloud.sta2_pw"]')).to_have_text(MASK)
    button.click()
    expect(value).to_have_text(MASK)
    expect(button).to_have_attribute("aria-label", "Show password")


def test_basic_tab_shows_labels_and_the_format_warning(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "basic")
    pane = page.locator('[data-pane="camera-basic"]')
    expect(pane).to_be_visible()
    expect(pane).to_contain_text("UTC+10:00")
    expect(pane).to_contain_text("Highest (Extreme)")
    expect(pane.locator(".badge-format").first).to_be_visible()


def test_settings_hash_opens_the_camera_tab(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "basic")
    page.goto(f"{live_server.url}/settings#camera-wifi")
    expect(page.locator('[data-section-nav="camera-wifi"]')).to_have_class("settings-nav-item active")
    expect(page.locator('[data-pane="camera-wifi"]')).to_be_visible()


def test_refresh_after_the_camera_leaves_shows_the_snapshot(
    live_server: Any, fake_camera: Any, page: Page
) -> None:
    _open_camera_tab(page, live_server.url, "system")
    fake_camera.stop()
    page.locator('[data-pane="camera-system"] button', has_text="Refresh").click()
    pane = page.locator('[data-pane="camera-system"]')
    expect(pane).to_contain_text("Camera offline: showing settings read at")
    expect(pane).to_be_visible()  # the active tab survives the swap
```

- [ ] **Step 3: Make the contrast audit cover real camera content**

In `test/e2e/test_contrast.py`, change the test signature to take the fixture, and wait for the camera panes on `/settings`:

```python
@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_all_text_meets_wcag_aa(
    live_server: Any, fake_camera: Any, browser: Browser, scheme: str
) -> None:
```

and inside the page loop, after `page.wait_for_timeout(500)`:

```python
        if path == "/settings":
            page.locator("#camera-panes .camera-field").first.wait_for(state="attached")
```

- [ ] **Step 4: Run the browser tests**

Run: `venv/bin/python -m pytest test/e2e/test_camera_settings.py test/e2e/test_contrast.py test/e2e/test_settings_active.py -m e2e -v`
Expected: all pass. If the contrast audit reports a camera element, fix its colour in `settings.css` with the `-text` or label tokens; never lower `MIN_RATIO`.

- [ ] **Step 5: WebKit (Safari engine), run locally**

```bash
docker run --rm --ipc=host -v "$PWD":/src:ro mcr.microsoft.com/playwright/python:v1.63.0-noble bash -c '
set -e; cp -r /src /tmp/app && cd /tmp/app && rm -rf venv .git
python -m venv /tmp/v >/dev/null && . /tmp/v/bin/activate
PIP_DEFAULT_TIMEOUT=60 pip install -q --retries 10 -e ".[dev]" "playwright==1.63.0" >/dev/null 2>&1
PLAYWRIGHT_BROWSERS_PATH=/ms-playwright python -m pytest test/e2e/test_camera_settings.py test/e2e/test_contrast.py -m e2e --browser webkit -q -p no:cacheprovider'
```

Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add test/e2e/conftest.py test/e2e/test_camera_settings.py test/e2e/test_contrast.py
git commit -m "test(camera): browser tests for reveal, offline and contrast"
```

---

### Task 10: Screenshots and documentation

**Files:**

- Modify: `scripts/screenshots.py`, `docs/features.md`, `docs/api.md`, `mkdocs.yml`, `CLAUDE.md`, `CHANGELOG.md`
- Create: `docs/guide/camera-settings.md`, `docs/assets/screenshots/camera-settings.png` (generated)

**Interfaces:**

- Consumes: the fixtures in `test/fixtures/camera/` (invented data only).

- [ ] **Step 1: Demo camera serves the fixture; capture the Camera pane**

In `scripts/screenshots.py`:

- Replace the `CONFIG_INI = """..."""` constant with:

```python
CAMERA_FIXTURES = REPO / "test" / "fixtures" / "camera"
CONFIG_INI = (CAMERA_FIXTURES / "dr900x-plus-config.ini").read_text(encoding="utf-8")
VERSION_BIN = (CAMERA_FIXTURES / "dr900x-plus-version.bin").read_bytes()
```

- In `do_GET`, serve `VERSION_BIN` for `/Config/version.bin` (instead of the inline `b"DR900X-2CH 1.012\x00"`).
- After the existing settings screenshot, add:

```python
        page.locator('[data-section-nav="camera-cloud"]').click()
        page.locator("#camera-panes .camera-field").first.wait_for(state="attached")
        shot(page, "camera-settings")
```

using the `shot(page, name)` helper defined in `capture()` (it waits 600 ms, then writes the png). Run: `venv/bin/python scripts/screenshots.py`. Expected: it writes `camera-settings.png`, with masked passwords and the fixture's demo SSIDs.

- [ ] **Step 2: User guide page**

Create `docs/guide/camera-settings.md`:

```markdown
# Camera settings

**Settings → Camera** shows every setting stored on the dashcam itself, grouped
the way the BlackVue app groups them: **Basic**, **Sensitivity**, **System**,
**Wi-Fi** and **Cloud**. This release shows them read-only; changing them from
here arrives in a later release.

![Camera settings](../assets/screenshots/camera-settings.png)

## Reading the camera

The panes read the camera's `Config/config.ini` each time they open, and when
you press **Refresh**. They wait up to `camera.read_timeout_seconds` (see
[Configuration](configuration.md#camera-access)). When the car is away, they show
the settings from the last successful read and say when that was.

If you change a setting in the BlackVue app and press **Refresh**, the panes
list what changed on the camera since the last read.

## Wi-Fi passwords

The camera hotspot password and the three home-network passwords are masked.
Select the eye icon to show one; select it again to hide it. The camera stores
these passwords encrypted with a key built into the BlackVue app. That key is
public, so the app treats them like any other secret: masked by default, never
logged, never sent unless you ask to see one.

If authentication is off (`auth.mode` `none`), anyone who can open the app can
reveal them. That is no wider than the camera itself, which serves its settings
file to anyone on your network.

## Settings that erase recordings

Settings marked **erases camera recordings** (the time settings and image
quality) make the camera format its microSD card when they are changed: the
BlackVue manual says it deletes all recordings on the card, locked events
included. Make sure a sync has finished before changing them, here or in the
BlackVue app.
```

In `mkdocs.yml`, add under `Getting started`, after `Configuration`:

```yaml
      - Camera settings: guide/camera-settings.md
```

- [ ] **Step 3: Reference and project docs**

`docs/api.md`:

- In `GET /api/dashcam/info`, change "this endpoint never writes to the camera" to: "this endpoint never writes to the camera, and Wi-Fi passwords in `config` are always `"***"`". Note that `firmware` is now `"<model> · fw <version>"`.
- Add a section after the Dashcam API:

````markdown
## Camera API Endpoints

### `GET /api/camera/config`

Reads the camera's `config.ini` live (timeout `camera.read_timeout_seconds`);
when the camera is unreachable, serves the last snapshot with `online: false`.
Passwords are masked (`value` is eight bullets, `raw` is `null`).

```json
{"available": true, "online": true, "read_at": "2026-10-05T01:02:03+00:00",
 "model": "DR900X Plus", "firmware": "1.015",
 "tabs": [{"name": "basic", "label": "Basic", "section": "Tab1",
           "fields": [{"key": "Tab1.TimeZone", "label": "Time zone", "value": "UTC+10:00",
                       "raw": "1000", "secret": false, "has_value": true,
                       "formats_card": true, "help": ""}],
           "other": [], "not_fitted": []}],
 "changed": [{"key": "Tab3.VOLUME", "label": "Volume", "from": "5", "to": "4"}]}
```

Before the first successful read: `{"available": false, "online": false}`.

### `GET /api/camera/secret?key=<Section.key>`

Returns one decrypted password from the snapshot: `{"key": "Cloud.sta_pw",
"value": "..."}` with `Cache-Control: no-store`. 404 for any key that is not a
password, or when there is no snapshot; 422 `UNDECODABLE_PASSWORD` when the
stored value does not decrypt to text.

### `GET /hx/camera/panes`

The htmx fragment behind **Settings → Camera** (one pane per tab).
````

`docs/features.md`: add a section after "Settings":

```markdown
## Camera settings

Every setting stored on the dashcam, shown under **Settings → Camera** in the
same groups as the BlackVue app, with Wi-Fi passwords masked behind an eye icon.
See [Camera settings](guide/camera-settings.md).
```

`CLAUDE.md`, in "Server Package":

- Add bullets for `camera_crypto.py`, `camera_config.py`, `camera_schema.py`, `routes/api_camera.py` and `routes/hx_camera.py`, one line each, from the module docstrings.
- Change "Registers the sixteen blueprints" to "eighteen".
- In "Test Structure", add `test/test_camera_*.py`, `test/test_routes_api_camera.py`, `test/test_routes_hx_camera.py`, `test/e2e/test_camera_settings.py` and `test/fixtures/camera/` (sanitized; never a real camera file).

`CHANGELOG.md`, under a new `## Unreleased` above `## 3.1.1`:

```markdown
## Unreleased

### Added

* **Camera settings** (Settings → Camera): every setting stored on the dashcam,
  read-only, in the BlackVue app's groups (Basic, Sensitivity, System, Wi-Fi,
  Cloud). Wi-Fi passwords are masked and can be revealed one at a time;
  settings that make the camera format its card are marked. **Refresh** lists
  what changed on the camera since the last read. See
  [Camera settings](https://tekgnosis-net.github.io/blackvuesync-v2/guide/camera-settings/).
* New `camera.read_timeout_seconds` setting (default 3).

### Security

* The dashboard's dashcam info card and `/api/dashcam/info` returned the
  camera's Wi-Fi passwords (encrypted with a public key, so readable). They are
  now masked.
```

- [ ] **Step 4: Build the docs and commit**

Run: `venv/bin/mkdocs build --strict -q; echo "exit $?"; rm -rf site`
Expected: `exit 0`.

```bash
git add scripts/screenshots.py docs CLAUDE.md CHANGELOG.md mkdocs.yml
git commit -m "docs(camera): guide, api reference and screenshots"
```

---

### Task 11: Verify, open the PR and release 3.2.0

**Files:** none new.

- [ ] **Step 1: Full suites**

```bash
venv/bin/python -m pytest test --ignore=test/e2e -q
venv/bin/python -m pytest test/e2e -m e2e -q
venv/bin/behave -f progress
venv/bin/pre-commit run --all-files
```

Expected: all pass. A mypy failure about `cryptography` means the hook's `additional_dependencies` change (Task 1) is missing.

- [ ] **Step 2: Docker image smoke test**

```bash
docker build -t bvs-camera-test . && docker run --rm --entrypoint /opt/venv/bin/python bvs-camera-test \
  -c "import cryptography, blackvuesync_v2.server.camera_crypto as c; print(cryptography.__version__, c.decrypt_password('hunter22'))"
```

Expected: `50.0.x hunter22`.

- [ ] **Step 3: Push and open the PR**

```bash
git push -u origin feat/camera-settings
gh pr create --repo tekgnosis-net/blackvuesync-v2 --base main --head feat/camera-settings \
  --title "feat: read-only camera settings and masked dashcam passwords" --body-file <body.md>
```

Write the PR body to a scratch file with four headings: **Summary** (the three
modules, the panes and the masking fix), **Spec and plan** (both paths), **Review
focus** (the five items above and the tests that pin them) and **Verification**
(the counts from Steps 1-2). **No session link or Claude attribution.** Wait for every check to pass (`gh pr checks <n> --watch`), then squash-merge via `gh api -X PUT repos/tekgnosis-net/blackvuesync-v2/pulls/<n>/merge -f merge_method=squash`.

- [ ] **Step 4: Release**

```bash
git checkout main && git pull --ff-only
venv/bin/python scripts/release.py prepare 3.2.0
venv/bin/python scripts/screenshots.py   # the footer and header show the version
git add docs/assets/screenshots && git commit -m "docs: regenerate screenshots for 3.2.0" && git push
# after the release PR's checks pass and it is merged:
venv/bin/python scripts/release.py tag 3.2.0
```

Then verify: the GitHub release `v3.2.0` exists; and `docker buildx imagetools inspect ghcr.io/tekgnosis-net/blackvuesync-v2:3.2.0` (and `:3.2`, `:3`) shows the merge commit for amd64 and arm64.

---

## After this plan: phase B (tests on the camera, each with Kumar's OK)

Run with the car parked at home, after a completed sync. They go from the NAS or
the deployed 3.2.0 (the development workstation cannot reach the camera):

0. **Calibration.** Change one setting at a time in the BlackVue app, press
   **Refresh** in Settings → Camera, and record the reported key and old/new
   codes for: `VideoQuality` and `ImageSetting` (confirm 0-indexed order),
   `AutoParking`, the sensitivity and volume ranges, `EventSpeedUnit`,
   `AlertLimit` units, `Accel/Harsh/SharpLimit`, and one half-hour time zone.
1. **`GET /upload.cgi`** (read-only): record the response; an HTML form reveals
   the field name.
2. **No-op upload** of the camera's own unchanged file: record the HTTP response,
   whether it reboots, and that the read-back is byte-identical.
3. **`VOLUME` 5 → 4 → 5**, verified each way.
4. **Port 9771** (connect; `ping`; `get time`; one `restart`), for phase D.

The findings go into `docs/reference/blackvue-camera-config.md`. Then the phase-C
plan (writes, the format guard, backups and restore) is written against the
observed upload format.
