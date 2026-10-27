# Camera Settings, Phase C (read/write) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every camera setting editable from Settings → Camera: staged edits, one review-and-apply upload, backups and restore, with a hard gate that settings which format the microSD card are never applied while any recording on the camera is not yet downloaded. Ships as 3.3.0.

**Architecture:** `camera_config.py` gains the write side (line-preserving `patch`, `self_check`, multipart `upload`, `wait_until_back`, `BackupStore`). `camera_schema.py` gains `validate_change`. `routes/api_camera.py` gains `POST /api/camera/config` (the apply pipeline from spec section 5), `GET /api/camera/pending`, and the backups endpoints. `hx_camera.py` renders editable widgets; `settings.js` stages edits across tabs and drives the review dialog. The apply holds the sync lock so a scheduled sync cannot run against a rebooting camera.

**Tech Stack:** Python 3.9+ standard library for all camera I/O (`urllib`, `email`), Flask, Jinja, htmx, Alpine CSP build, Playwright, Behave.

**Spec:** `docs/plans/2026-10-05-sub-project-7-camera-settings-design.md`, sections 4.2 (phase-C rows), 5, 6 (staging, review dialog, apply progress, backups pane), 7 and 8. Phase A plan: `docs/plans/2026-10-05-sub-project-7-camera-settings-plan.md`.

**Verified on the DR900X Plus (2026-10-27, read-only):** `GET /upload.cgi` returns an HTML form: `POST /upload.cgi`, `multipart/form-data`, select `FileType` with values `config`, `version` (sic, also labelled config) and `fwupgrade`, and a file input `FileData`. Port 9771 is open. 1,668 of 2,006 recordings were still on the camera only.

**Deviations from the spec, decided while planning:**

- **No `accept_loss` override.** Kumar's rule: a setting that formats the card is never applied while recordings are pending. The server answers 409 `FORMAT_BLOCKED` with the pending count; the review dialog explains and offers **Sync now** only. `confirm_format` is still required when pending is 0.
- **`ImageSetting` stays format-guarded** (calibration pending).
- **Passwords:** a blank password field in the form means "unchanged"; to clear a home network the user clears its SSID (the server then also clears that network's password). `ap_pw` cannot be emptied.
- **Restore** goes through the same apply pipeline, computing changes as the diff between the backup and a fresh read, so the format gate applies to restores too.
- **Phase-B write tests on the real camera are the release gate** (Task 11), not a prerequisite for implementation: everything is built and tested against the fake camera first.

## Global Constraints

- Python 3.9+; `from __future__ import annotations`; type annotations everywhere; `sync.py`/`metrics.py` stay standard-library only; `cryptography` is imported only by `camera_crypto.py`.
- Comments and docstrings lowercase, third person, concise.
- CSP: no inline scripts or handlers; Alpine CSP build (bare property/method references only); colours from `tokens.css` only; `test/e2e/test_contrast.py` must pass.
- Camera passwords: never logged, never in HTML or JSON except `/api/camera/secret`; apply logs name changed keys and show `(changed)` for passwords; never log `Change`, `CameraFile` or raw values.
- Every threshold is a settings knob with default and clamp: `camera.backup_count` (10, 1-100), `camera.verify_timeout_seconds` (120, 10-600), `camera.verify_poll_seconds` (5, 1-30); documented in `docs/guide/configuration.md`.
- Files under `<settings dir>/camera/` and `camera/backups/`: directories 0700, files 0600. Backup names `config-YYYYMMDD-HHMMSS.ini` in local time.
- **Format gate (hard):** keys `Tab1.TimeSet`, `Tab1.SetTime`, `Tab1.TimeZone`, `Tab1.Daylight`, `Tab1.GpsSync`, `Tab1.ImageSetting`, `Tab1.VideoQuality` may only be uploaded when the server-side pending count is exactly 0 and the request carries `confirm_format: true`. Pending = `.mp4` files the camera lists that have no file at their destination path (grouping honoured).
- Line endings, byte order and unknown keys of `config.ini` are preserved exactly; only the value part of changed lines is rewritten.
- Commit message lines under 80 characters; no Claude attribution or session trailer; never `--no-verify`.

## Review Focus

1. **A changed key appears twice in its section** (duplicate line). Expected: refused (422), nothing uploaded. Task 1.
2. **The camera reboots after the upload and comes back with a different value than sent** (firmware clamps it). Expected: 200 with `kept` naming the key and the snapshot taking the camera's value. Task 5.
3. **A scheduled sync fires during an apply.** Expected: the sync is skipped with one log line; the apply continues. Task 5.
4. **The camera goes offline between the fresh read and the upload.** Expected: 502 `UPLOAD_FAILED`, backup kept, no partial state. Task 5.
5. **Restoring a backup whose time-zone differs from the camera's current one while recordings are pending.** Expected: 409 `FORMAT_BLOCKED`; no upload. Task 6.

---

### Task 1: Line-preserving patch, self-check and multipart upload

**Files:**

- Modify: `blackvuesync_v2/server/camera_config.py`
- Test: `test/test_camera_config.py` (append)

**Interfaces:**

- Consumes: `parse`, `CameraFile`, `_KEY_RE`, `_get`, `fetch`, `CameraRead` (existing).
- Produces:
  - `class PatchError(ValueError)`
  - `patch(raw: bytes, changes: dict[tuple[str, str], str]) -> bytes`
  - `self_check(before: bytes, after: bytes, changes: dict[tuple[str, str], str]) -> None` (raises `PatchError`)
  - `class UploadError(RuntimeError)` with `.status: int | None`
  - `upload(address: str, raw: bytes, timeout: float) -> int` (HTTP status; tests monkeypatch `_post`)
  - `_post(url: str, body: bytes, content_type: str, timeout: float) -> int`
  - `wait_until_back(address: str, timeout: float, poll: float) -> CameraRead | None`

- [ ] **Step 1: Write the failing tests**

Append to `test/test_camera_config.py`:

```python
from blackvuesync_v2.server.camera_config import (
    PatchError,
    UploadError,
    patch,
    self_check,
    upload,
    wait_until_back,
)


def test_patch_rewrites_only_the_changed_values() -> None:
    raw = b"[Tab1]\r\nVOLUME = 5\r\n[Wifi]\r\nap_ssid=Cam\r\nVOLUME=1\r\n"
    out = patch(raw, {("Tab1", "VOLUME"): "4", ("Wifi", "ap_ssid"): "Cam2"})
    assert out == b"[Tab1]\r\nVOLUME = 4\r\n[Wifi]\r\nap_ssid=Cam2\r\nVOLUME=1\r\n"
    assert patch(CONFIG, {}) == CONFIG


def test_patch_keeps_every_other_byte_of_the_fixture() -> None:
    out = patch(CONFIG, {("Tab3", "VOLUME"): "4"})
    assert out.count(b"\n") == CONFIG.count(b"\n")
    assert parse(out).value("Tab3", "VOLUME") == "4"
    assert [e for e in parse(out).entries if e.key != "VOLUME"] == [
        e for e in parse(CONFIG).entries if e.key != "VOLUME"
    ]


def test_patch_refuses_missing_duplicate_and_control_characters() -> None:
    """review focus 1: a duplicated key is ambiguous, so nothing is written."""
    with pytest.raises(PatchError):
        patch(b"[Tab1]\nVOLUME=5\n", {("Tab1", "Missing"): "1"})
    with pytest.raises(PatchError):
        patch(b"[Tab1]\nVOLUME=5\nVOLUME=6\n", {("Tab1", "VOLUME"): "4"})
    with pytest.raises(PatchError):
        patch(b"[Tab1]\nVOLUME=5\n", {("Tab1", "VOLUME"): "4\n[Wifi]\nap_pw=x"})


def test_self_check_catches_a_tampered_patch() -> None:
    before = b"[Tab1]\nVOLUME=5\nRECLED=1\n"
    self_check(before, b"[Tab1]\nVOLUME=4\nRECLED=1\n", {("Tab1", "VOLUME"): "4"})
    with pytest.raises(PatchError):
        self_check(before, b"[Tab1]\nVOLUME=4\nRECLED=0\n", {("Tab1", "VOLUME"): "4"})
    with pytest.raises(PatchError):
        self_check(before, b"[Tab1]\nVOLUME=5\nRECLED=1\n", {("Tab1", "VOLUME"): "4"})


def test_upload_posts_a_multipart_config(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_post(url: str, body: bytes, content_type: str, timeout: float) -> int:
        seen.update(url=url, body=body, content_type=content_type, timeout=timeout)
        return 200

    monkeypatch.setattr(camera_config, "_post", fake_post)
    assert upload("cam", b"[Tab1]\nVOLUME=4\n", 9.0) == 200
    assert seen["url"] == "http://cam/upload.cgi"
    content_type = str(seen["content_type"])
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=", 1)[1].encode()
    body = bytes(seen["body"])  # type: ignore[arg-type]
    assert body.count(b"--" + boundary) == 3  # two parts plus the closing line
    assert b'name="FileType"\r\n\r\nconfig\r\n' in body
    assert b'name="FileData"; filename="config.ini"' in body
    assert b"\r\n\r\n[Tab1]\nVOLUME=4\n\r\n" in body
    assert body.endswith(b"--" + boundary + b"--\r\n")


def test_upload_failures_raise_upload_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise urllib.error.HTTPError("http://cam/upload.cgi", 500, "boom", None, None)

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    with pytest.raises(UploadError) as caught:
        upload("cam", b"x", 1.0)
    assert caught.value.status == 500

    def drop(*_args: Any, **_kwargs: Any) -> Any:
        raise http.client.RemoteDisconnected("closed")

    monkeypatch.setattr(urllib.request, "urlopen", drop)
    with pytest.raises(UploadError) as caught2:
        upload("cam", b"x", 1.0)
    assert caught2.value.status is None


def test_wait_until_back_polls_until_the_camera_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = iter([None, None, CONFIG])
    monkeypatch.setattr(
        camera_config,
        "_get",
        lambda url, timeout: next(answers) if url.endswith("config.ini") else VERSION,
    )
    monkeypatch.setattr(camera_config.time, "sleep", lambda _s: None)
    read = wait_until_back("cam", timeout=10.0, poll=0.01)
    assert read is not None and read.config.raw == CONFIG


def test_wait_until_back_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(camera_config, "_get", lambda url, timeout: None)
    clock = iter([0.0, 0.0, 5.0, 11.0, 12.0])
    monkeypatch.setattr(camera_config.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(camera_config.time, "sleep", lambda _s: None)
    assert wait_until_back("cam", timeout=10.0, poll=1.0) is None
```

Add `import urllib.error` to the test file's imports (it already imports `http.client`, `urllib.request`, `pytest`, `camera_config`, `CONFIG`, `VERSION`).

- [ ] **Step 2: Run them to verify they fail**

Run: `venv/bin/python -m pytest test/test_camera_config.py -q`
Expected: `ImportError: cannot import name 'PatchError'`.

- [ ] **Step 3: Implement**

Add to the imports of `camera_config.py`: `import time`, `import urllib.error`, `import uuid`. Append:

```python
class PatchError(ValueError):
    """the requested change cannot be applied to this file unambiguously."""


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def patch(raw: bytes, changes: dict[tuple[str, str], str]) -> bytes:
    """rewrites the value of each changed key in place; every other byte stays.

    a key that is missing or duplicated in its section raises PatchError, as
    does a value with control characters (which could inject lines).
    """
    for (section, key), value in changes.items():
        if _CONTROL_RE.search(value):
            raise PatchError(f"{section}.{key}: value contains control characters")
    pending = dict(changes)
    seen: dict[tuple[str, str], int] = {}
    out: list[bytes] = []
    section = GENERAL_SECTION
    for line in raw.splitlines(keepends=True):
        text = line.decode("utf-8", errors="surrogateescape")
        body = text.rstrip("\r\n")
        ending = text[len(body) :]
        stripped = body.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1].strip()
            out.append(line)
            continue
        key, sep, _ = body.partition("=")
        name = key.strip()
        if not sep or not _KEY_RE.fullmatch(name):
            out.append(line)
            continue
        seen[(section, name)] = seen.get((section, name), 0) + 1
        if seen[(section, name)] > 1 and (section, name) in changes:
            raise PatchError(f"{section}.{name}: key appears more than once")
        if (section, name) in pending:
            value = pending.pop((section, name))
            # keeps the key text and its spacing up to "=" exactly as written
            prefix = key + "=" + (" " if key.endswith(" ") else "")
            out.append(
                (prefix + value + ending).encode("utf-8", errors="surrogateescape")
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
```

Add the new names to `__all__`: `"PatchError"`, `"UploadError"`, `"patch"`, `"self_check"`, `"upload"`, `"wait_until_back"`.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python -m pytest test/test_camera_config.py -q`
Expected: all pass (previous count + 8).

- [ ] **Step 5: Commit**

```bash
git add blackvuesync_v2/server/camera_config.py test/test_camera_config.py
git commit -m "feat(camera): line-preserving patch, self-check and upload"
```

---

### Task 2: Backup store

**Files:**

- Modify: `blackvuesync_v2/server/camera_config.py`
- Test: `test/test_camera_config.py` (append)

**Interfaces:**

- Produces: `BackupInfo(name: str, created: datetime.datetime, size: int)`; `BackupStore(directory: Path)` with `.save(raw: bytes, now: datetime.datetime | None = None) -> str`, `.prune(keep: int) -> None`, `.list() -> list[BackupInfo]` (newest first), `.read(name: str) -> bytes | None`; `BACKUP_NAME_RE`.

- [ ] **Step 1: Write the failing tests**

```python
from blackvuesync_v2.server.camera_config import BackupStore


def test_backups_are_private_named_by_local_time_and_pruned(tmp_path: Path) -> None:
    store = BackupStore(tmp_path / "camera" / "backups")
    assert store.list() == []
    names = []
    for minute in range(3):
        when = datetime.datetime(2026, 10, 27, 21, minute, 0).astimezone()
        names.append(store.save(b"[Tab1]\nVOLUME=%d\n" % minute, now=when))
    assert names[0] == "config-20261027-210000.ini"
    assert _mode(tmp_path / "camera" / "backups") == 0o700
    assert _mode(tmp_path / "camera" / "backups" / names[0]) == 0o600
    assert [b.name for b in store.list()] == list(reversed(names))
    assert store.list()[0].size == len(b"[Tab1]\nVOLUME=2\n")
    store.prune(keep=2)
    assert [b.name for b in store.list()] == [names[2], names[1]]
    assert store.read(names[1]) == b"[Tab1]\nVOLUME=1\n"
    assert store.read(names[0]) is None


def test_backup_read_rejects_names_outside_the_pattern(tmp_path: Path) -> None:
    store = BackupStore(tmp_path / "backups")
    store.save(b"x")
    for bad in ("../settings.json", "config-20261027-210000.ini/../x", "evil.ini"):
        assert store.read(bad) is None


def test_backup_save_twice_in_one_second_keeps_both(tmp_path: Path) -> None:
    store = BackupStore(tmp_path / "backups")
    when = datetime.datetime(2026, 10, 27, 21, 0, 0).astimezone()
    first = store.save(b"a", now=when)
    second = store.save(b"b", now=when)
    assert first != second and store.read(second) == b"b"
```

- [ ] **Step 2: Run to verify they fail** -- `ImportError: cannot import name 'BackupStore'`.

- [ ] **Step 3: Implement** (append to `camera_config.py`):

```python
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
        return sorted(infos, key=lambda b: b.name, reverse=True)

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
```

Add `"BackupInfo"`, `"BackupStore"`, `"BACKUP_NAME_RE"` to `__all__`.

- [ ] **Step 4: Run** `venv/bin/python -m pytest test/test_camera_config.py -q` -- all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(camera): backup store for config.ini uploads"`

---

### Task 3: Change validation and password encoding in the schema

**Files:**

- Modify: `blackvuesync_v2/server/camera_schema.py`
- Test: `test/test_camera_schema.py` (append)

**Interfaces:**

- Produces: `FORMAT_KEYS: frozenset[tuple[str, str]]`; `validate_change(section: str, key: str, value: str) -> list[str]`; `prepare_changes(raw_changes: dict[str, str]) -> tuple[dict[tuple[str, str], str], list[str]]` which validates every `"Section.key": value`, encrypts secrets via `camera_crypto.encrypt_password`, and returns `(changes, errors)`.

- [ ] **Step 1: Tests**

```python
from blackvuesync_v2.server.camera_crypto import decrypt_password
from blackvuesync_v2.server.camera_schema import FORMAT_KEYS, prepare_changes, validate_change


def test_format_keys_are_the_time_and_image_settings() -> None:
    assert FORMAT_KEYS == frozenset(
        ("Tab1", k)
        for k in ("TimeSet", "SetTime", "TimeZone", "Daylight", "GpsSync", "ImageSetting", "VideoQuality")
    )


def test_validate_change_by_widget() -> None:
    assert validate_change("Tab1", "VideoQuality", "3") == []
    assert validate_change("Tab1", "VideoQuality", "9") == ["Tab1.VideoQuality: not one of 0, 1, 2, 3"]
    assert validate_change("Tab3", "RECLED", "2") == ["Tab3.RECLED: must be 0 or 1"]
    assert validate_change("Tab3", "VOLUME", "4") == []
    assert validate_change("Tab3", "VOLUME", "abc") == ["Tab3.VOLUME: must be a whole number"]
    assert validate_change("Tab3", "ScheduledRebootTime", "24") == ["Tab3.ScheduledRebootTime: must be an hour from 0 to 23"]
    assert validate_change("Tab1", "TimeZone", "930") == []
    assert validate_change("Tab1", "TimeZone", "1070") == ["Tab1.TimeZone: must be a UTC offset like 1000 or -1100"]
    assert validate_change("Tab1", "RecordTime", "2") == ["Tab1.RecordTime: read-only"]
    assert validate_change("Cloud", "sta_ssid", "x" * 33) == ["Cloud.sta_ssid: at most 32 bytes"]
    assert validate_change("Cloud", "sta_ssid", "Caf\u00e9") == []
    assert validate_change("Tab3", "userString", "a" * 21) == ["Tab3.userString: at most 20 characters"]
    assert validate_change("Wifi", "ap_pw", "short") == ["Wifi.ap_pw: 8 to 32 characters"]
    assert validate_change("Cloud", "sta2_pw", "") == []
    assert validate_change("Wifi", "ap_pw", "") == ["Wifi.ap_pw: 8 to 32 characters"]
    assert validate_change("Tab1", "VOLUME", "4\n") == ["Tab1.VOLUME: control characters are not allowed"]
    assert validate_change("Fancy", "Mode", "2") == []  # unknown keys: text, control chars only


def test_prepare_changes_encrypts_passwords_and_collects_errors() -> None:
    changes, errors = prepare_changes({"Tab3.VOLUME": "4", "Cloud.sta_pw": "DemoHome-123"})
    assert errors == []
    assert changes[("Tab3", "VOLUME")] == "4"
    assert decrypt_password(changes[("Cloud", "sta_pw")]) == "DemoHome-123"
    assert changes[("Cloud", "sta_pw")] != "DemoHome-123"
    _changes, errors = prepare_changes({"Tab3.VOLUME": "x", "nodot": "1"})
    assert errors == ["Tab3.VOLUME: must be a whole number", "nodot: expected Section.key"]
```

- [ ] **Step 2: Run to verify they fail** -- `ImportError`.

- [ ] **Step 3: Implement** (append to `camera_schema.py`; add `from blackvuesync_v2.server.camera_crypto import encrypt_password` and `import re` if missing):

```python
FORMAT_KEYS: frozenset[tuple[str, str]] = frozenset(
    (f.section, f.key) for f in _FIELDS if f.formats_card
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_INT_RE = re.compile(r"-?[0-9]{1,6}")
_OFFSET_RE = re.compile(r"-?[0-9]{1,4}")
_TEXT_LIMITS = {("Tab3", "userString"): 20}


def validate_change(section: str, key: str, value: str) -> list[str]:
    """returns the problems with setting section.key to value (empty = ok)."""
    name = f"{section}.{key}"
    if _CONTROL_RE.search(value):
        return [f"{name}: control characters are not allowed"]
    field = field_for(section, key)
    if field.read_only:
        return [f"{name}: read-only"]
    if field.secret:
        if value == "" and key != "ap_pw":
            return []
        return [] if 8 <= len(value) <= 32 else [f"{name}: 8 to 32 characters"]
    if field.key == "TimeZone":
        return [] if format_utc_offset(value) != value or value == "0" else [
            f"{name}: must be a UTC offset like 1000 or -1100"
        ]
    if field.widget == "select":
        allowed = [raw for raw, _label in field.options]
        return [] if value in allowed else [f"{name}: not one of {', '.join(allowed)}"]
    if field.widget == "toggle":
        return [] if value in ("0", "1") else [f"{name}: must be 0 or 1"]
    if field.widget == "hour":
        return [] if value.isdigit() and value.isascii() and 0 <= int(value) <= 23 else [
            f"{name}: must be an hour from 0 to 23"
        ]
    if field.widget == "number":
        return [] if _INT_RE.fullmatch(value) else [f"{name}: must be a whole number"]
    if key.endswith("_ssid") and len(value.encode("utf-8")) > 32:
        return [f"{name}: at most 32 bytes"]
    limit = _TEXT_LIMITS.get((section, key))
    if limit is not None and len(value) > limit:
        return [f"{name}: at most {limit} characters"]
    return []


def prepare_changes(
    raw_changes: dict[str, str],
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """validates "Section.key" -> value pairs and encodes passwords for the file."""
    changes: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for full_key, value in raw_changes.items():
        section, dot, key = full_key.partition(".")
        if not dot or not section or not key:
            errors.append(f"{full_key}: expected Section.key")
            continue
        problems = validate_change(section, key, str(value))
        if problems:
            errors.extend(problems)
            continue
        if is_secret(section, key) and value != "":
            changes[(section, key)] = encrypt_password(str(value))
        else:
            changes[(section, key)] = str(value)
    return changes, errors
```

Note: `format_utc_offset` returns the input unchanged when it is not a valid offset, which is why the TimeZone check compares against the input; `"0"` formats to `UTC+00:00`, so it is accepted explicitly. Add the three names to `__all__`.

- [ ] **Step 4: Run** `venv/bin/python -m pytest test/test_camera_schema.py -q` -- all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(camera): validate setting changes and encode passwords"`

---

### Task 4: Apply knobs in the `camera` settings section

**Files:** `blackvuesync_v2/settings.py`, `blackvuesync_v2/server/settings_form.py`, `docs/guide/configuration.md`, `CLAUDE.md`; tests in `test/test_settings.py`, `test/test_settings_form.py`.

- [ ] **Step 1: Tests** (append):

```python
def test_camera_apply_knobs_defaults_and_bounds() -> None:
    from blackvuesync_v2.settings import CameraSettings

    cam = CameraSettings()
    assert (cam.backup_count, cam.verify_timeout_seconds, cam.verify_poll_seconds) == (10, 120.0, 5.0)
    assert CameraSettings(backup_count=0).validate() == ["camera.backup_count must be between 1 and 100"]
    assert CameraSettings(verify_timeout_seconds=601).validate() == [
        "camera.verify_timeout_seconds must be between 10 and 600"
    ]
    assert CameraSettings(verify_poll_seconds=0.5).validate() == [
        "camera.verify_poll_seconds must be between 1 and 30"
    ]
```

and in `test_settings_form.py` extend `test_camera_section_has_the_read_timeout_field` to assert the section now has four specs named `read_timeout_seconds`, `backup_count`, `verify_timeout_seconds`, `verify_poll_seconds`, all `number`/`number`.

- [ ] **Step 2: Run to verify they fail.**

- [ ] **Step 3: Implement.** In `CameraSettings` add after `read_timeout_seconds`:

```python
    # copies of the camera's file kept before each upload
    backup_count: int = 10
    # how long an apply waits for the camera after an upload (it may reboot)
    verify_timeout_seconds: float = 120.0
    # seconds between read-back attempts while waiting
    verify_poll_seconds: float = 5.0
```

and in `_validate_values`:

```python
        if not 1 <= self.backup_count <= 100:
            errors.append("camera.backup_count must be between 1 and 100")
        if not 10 <= self.verify_timeout_seconds <= 600:
            errors.append("camera.verify_timeout_seconds must be between 10 and 600")
        if not 1 <= self.verify_poll_seconds <= 30:
            errors.append("camera.verify_poll_seconds must be between 1 and 30")
```

In `settings_form.py` add three `FieldSpec`s to the `"camera"` tuple: `("backup_count", "Backups to keep", "number", "number", help="copies of the camera's settings file kept before each change (1-100)")`, `("verify_timeout_seconds", "Verify timeout (seconds)", "number", "number", help="how long to wait for the camera after a change; it may reboot (10-600)")`, `("verify_poll_seconds", "Verify poll interval (seconds)", "number", "number")`.

Docs: add the three rows to the `### Camera access` table in `configuration.md` and to the `camera` row of the CLAUDE.md settings table.

- [ ] **Step 4: Run** the settings suites. **Step 5: Commit** `git commit -m "feat(settings): backup and verify knobs for camera changes"`

---

### Task 5: Pending count, sync-lock sharing and the apply pipeline

**Files:**

- Create: `blackvuesync_v2/server/camera_apply.py`
- Modify: `blackvuesync_v2/server/sync_runner.py` (expose `try_hold_sync_lock`), `blackvuesync_v2/server/routes/api_camera.py` (`GET /pending`, `POST /config`)
- Test: `test/test_camera_apply.py` (new), `test/test_routes_api_camera.py` (append)

**Interfaces:**

- `sync_runner.try_hold_sync_lock() -> contextlib.AbstractContextManager[bool]`: yields True while holding `_sync_lock` (non-blocking); False when a sync holds it.
- `camera_apply.pending_recordings(settings) -> int | None`: `.mp4` names from `get_dashcam_filenames(f"http://{address}")` filtered with `apply_recording_filters`, mapped through `to_recording`/`get_group_name`/`get_filepath`; counts missing files; None when the camera is unreachable.
- `camera_apply.ApplyResult(verified: bool, came_back: bool, kept: list[str], backup: str, changed: list[str])`
- `camera_apply.ApplyRefused(Exception)` with `.code: str`, `.status: int`, `.detail: dict`.
- `camera_apply.apply_changes(store: SettingsStore, raw_changes: dict[str, str], confirm_format: bool) -> ApplyResult`.
- Routes: `GET /api/camera/pending` → `{"pending": n}` or 409 `CAMERA_OFFLINE`; `POST /api/camera/config` → 200 `ApplyResult` as JSON, or the 409/422/500/502 codes below.

- [ ] **Step 1: Tests.** Create `test/test_camera_apply.py`:

```python
"""tests for the camera apply pipeline against a fake camera."""

from __future__ import annotations

import dataclasses
import os
import threading
from pathlib import Path
from typing import Any
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
        self.offline_reads = 0  # reads to fail after an upload (reboot)
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
        return 200

    def vod_lines(self) -> list[str]:
        return [f"n:/Record/{name},s:1000000" for name in self.vod]


@pytest.fixture()
def camera(monkeypatch: pytest.MonkeyPatch) -> FakeCamera:
    fake = FakeCamera()
    monkeypatch.setattr(camera_config, "_get", fake.get)
    monkeypatch.setattr(camera_config, "_post", fake.post)
    monkeypatch.setattr(camera_apply, "get_dashcam_filenames", lambda base_url: list(fake.vod))
    monkeypatch.setattr(camera_config.time, "sleep", lambda _s: None)
    return fake


@pytest.fixture()
def store(tmp_path: Path) -> SettingsStore:
    (tmp_path / "rec").mkdir()
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.10"}, clear=False):
        settings = SettingsStore(tmp_path / "settings.json")
    settings.update(
        lambda s: dataclasses.replace(
            s,
            auth=dataclasses.replace(s.auth, password_hash=hash_password("pw-1234-test")),
            system=dataclasses.replace(s.system, destination=str(tmp_path / "rec")),
            camera=dataclasses.replace(s.camera, verify_poll_seconds=1.0, verify_timeout_seconds=10.0),
        )
    )
    return settings


def test_pending_counts_recordings_without_a_downloaded_file(
    store: SettingsStore, camera: FakeCamera, tmp_path: Path
) -> None:
    camera.vod = ["20261027_210000_NF.mp4", "20261027_210000_NR.mp4", "20261027_210100_NF.mp4"]
    (tmp_path / "rec" / "20261027_210000_NF.mp4").write_bytes(b"x")
    assert camera_apply.pending_recordings(store.get()) == 2
    camera.vod = []
    assert camera_apply.pending_recordings(store.get()) == 0


def test_pending_is_none_when_the_camera_is_unreachable(
    store: SettingsStore, camera: FakeCamera, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(_base_url: str) -> list[str]:
        raise RuntimeError("no route")

    monkeypatch.setattr(camera_apply, "get_dashcam_filenames", boom)
    assert camera_apply.pending_recordings(store.get()) is None


def test_apply_patches_backs_up_uploads_and_verifies(store: SettingsStore, camera: FakeCamera) -> None:
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "4", "Cloud.sta2_ssid": "Shed"}, False)
    assert result.verified and result.came_back and result.kept == []
    assert sorted(result.changed) == ["Cloud.sta2_ssid", "Tab3.VOLUME"]
    assert camera_config.parse(camera.config).value("Tab3", "VOLUME") == "4"
    assert camera.uploads[0].count(b"\n") == CONFIG.count(b"\n")
    backups = camera_config.BackupStore(store.path.parent / "camera" / "backups")
    assert [b.name for b in backups.list()] == [result.backup]
    assert backups.read(result.backup) == CONFIG
    snapshot = camera_config.CameraStore(store.path.parent / "camera").load()
    assert snapshot is not None and snapshot.config.value("Tab3", "VOLUME") == "4"


def test_apply_survives_a_reboot_and_reports_kept_values(store: SettingsStore, camera: FakeCamera) -> None:
    """review focus 2: the camera comes back with a clamped value."""
    camera.offline_reads = 3
    camera.clamp[("Tab3", "VOLUME")] = "7"
    result = camera_apply.apply_changes(store, {"Tab3.VOLUME": "9"}, False)
    assert result.came_back and not result.verified and result.kept == ["Tab3.VOLUME"]
    snapshot = camera_config.CameraStore(store.path.parent / "camera").load()
    assert snapshot is not None and snapshot.config.value("Tab3", "VOLUME") == "7"


def test_apply_reports_a_camera_that_never_returns(store: SettingsStore, camera: FakeCamera) -> None:
    camera.offline_reads = 1000
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


def test_format_keys_need_confirmation_and_zero_pending(store: SettingsStore, camera: FakeCamera) -> None:
    camera.vod = ["20261027_210000_NF.mp4"]
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, True)
    assert (e.value.status, e.value.code, e.value.detail) == (409, "FORMAT_BLOCKED", {"pending": 1})
    assert camera.uploads == []
    camera.vod = []
    with pytest.raises(camera_apply.ApplyRefused) as e:
        camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, False)
    assert e.value.code == "FORMAT_CONFIRMATION_REQUIRED"
    result = camera_apply.apply_changes(store, {"Tab1.TimeZone": "1100"}, True)
    assert result.verified


def test_apply_is_refused_while_a_sync_holds_the_lock(store: SettingsStore, camera: FakeCamera) -> None:
    """review focus 3 (inverse): the apply never races a running sync."""
    assert sync_runner._sync_lock.acquire(blocking=False)  # pylint: disable=protected-access
    try:
        with pytest.raises(camera_apply.ApplyRefused) as e:
            camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
        assert (e.value.status, e.value.code) == (409, "SYNC_RUNNING")
    finally:
        sync_runner._sync_lock.release()  # pylint: disable=protected-access


def test_scheduled_sync_is_skipped_during_an_apply(store: SettingsStore, camera: FakeCamera) -> None:
    """review focus 3: trigger_sync answers already_running while an apply holds the lock."""
    seen: dict[str, object] = {}
    original_post = camera.post

    def post_and_probe(url: str, body: bytes, content_type: str, timeout: float) -> int:
        from blackvuesync_v2.server.progress import ProgressPublisher

        seen["during"] = sync_runner.trigger_sync(store.get(), ProgressPublisher())["status"]
        return original_post(url, body, content_type, timeout)

    camera.post = post_and_probe  # type: ignore[method-assign]
    camera_apply.apply_changes(store, {"Tab3.VOLUME": "4"}, False)
    assert seen["during"] == "already_running"
    assert not sync_runner._sync_lock.locked()  # pylint: disable=protected-access


def test_passwords_never_reach_the_log(
    store: SettingsStore, camera: FakeCamera, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    with caplog.at_level(logging.DEBUG):
        camera_apply.apply_changes(store, {"Cloud.sta_pw": "NewHome-9999", "Tab3.VOLUME": "3"}, False)
    assert "NewHome-9999" not in caplog.text
    assert "Cloud.sta_pw (changed)" in caplog.text and "Tab3.VOLUME" in caplog.text
```

Append to `test/test_routes_api_camera.py` route tests: `GET /api/camera/pending` → `{"pending": 0}` with an empty vod (monkeypatch `camera_apply.get_dashcam_filenames`), 409 `CAMERA_OFFLINE` when it raises; `POST /api/camera/config` with JSON `{"changes": {"Tab3.VOLUME": "4"}}` → 200 and `verified` true (FakeCamera from `test_camera_apply` imported, plus `_post` monkeypatched); a missing/invalid body → 422 `INVALID_CHANGES`; `FORMAT_BLOCKED` → 409 with `pending`; an unauthenticated POST → 401; a POST without the CSRF token in a non-testing app → 400 (follow `test_server_hardening.py`'s CSRF pattern).

- [ ] **Step 2: Run to verify they fail** -- `ImportError: ... camera_apply`.

- [ ] **Step 3: Implement.** In `sync_runner.py` add:

```python
@contextlib.contextmanager
def try_hold_sync_lock() -> Iterator[bool]:
    """holds the sync lock for the block when free; yields False when a sync runs."""
    held = _sync_lock.acquire(blocking=False)
    try:
        yield held
    finally:
        if held:
            _sync_lock.release()
```

(`import contextlib`, `from typing import Iterator`.) Create `blackvuesync_v2/server/camera_apply.py`:

```python
"""applies camera setting changes: fresh read, format gate, backup, patch,
upload, wait for the camera, verify. holds the sync lock throughout so a
scheduled sync never runs against a rebooting camera."""

from __future__ import annotations

import dataclasses
import logging
import os

from blackvuesync_v2.server import camera_config
from blackvuesync_v2.server.camera_config import BackupStore, CameraStore
from blackvuesync_v2.server.camera_schema import FORMAT_KEYS, is_secret, prepare_changes
from blackvuesync_v2.server.sync_runner import try_hold_sync_lock
from blackvuesync_v2.settings import Settings, SettingsStore
from blackvuesync_v2.sync import (
    apply_recording_filters,
    get_dashcam_filenames,
    get_filepath,
    get_group_name,
    to_recording,
)

logger = logging.getLogger(__name__)


class ApplyRefused(Exception):
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


def pending_recordings(settings: Settings) -> int | None:
    """counts recordings on the camera with no downloaded file; None if unreachable."""
    try:
        names = get_dashcam_filenames(f"http://{settings.connection.address}")
    except Exception:  # pylint: disable=broad-exception-caught
        return None
    grouping = settings.sync.grouping
    recordings = [r for r in (to_recording(n, grouping) for n in names if n.endswith(".mp4")) if r]
    recordings = apply_recording_filters(recordings, settings.sync.include, settings.sync.exclude)
    missing = 0
    for rec in recordings:
        group = get_group_name(rec.datetime, grouping)
        if not os.path.isfile(get_filepath(settings.system.destination, group, rec.filename)):
            missing += 1
    return missing


def _describe(changes: dict[tuple[str, str], str]) -> list[str]:
    return [f"{s}.{k}" + (" (changed)" if is_secret(s, k) else "") for s, k in changes]


def apply_changes(
    store: SettingsStore, raw_changes: dict[str, str], confirm_format: bool
) -> ApplyResult:
    """runs the apply pipeline from the design spec, section 5."""
    settings = store.get()
    changes, errors = prepare_changes(raw_changes)
    if errors:
        raise ApplyRefused(422, "INVALID_CHANGES", "some changes are invalid", errors=errors)
    if not changes:
        raise ApplyRefused(422, "NO_CHANGES", "nothing to apply")
    address = settings.connection.address
    cam = settings.camera
    with try_hold_sync_lock() as held:
        if not held:
            raise ApplyRefused(409, "SYNC_RUNNING", "a sync is running; try again when it finishes")
        fresh = camera_config.fetch(address, cam.read_timeout_seconds)
        if fresh is None:
            raise ApplyRefused(409, "CAMERA_OFFLINE", "the camera is not reachable")
        if any(name in FORMAT_KEYS for name in changes):
            pending = pending_recordings(settings)
            if pending is None:
                raise ApplyRefused(409, "CAMERA_OFFLINE", "could not list the camera's recordings")
            if pending > 0:
                raise ApplyRefused(
                    409, "FORMAT_BLOCKED",
                    "this change makes the camera format its card; recordings are not downloaded yet",
                    pending=pending,
                )
            if not confirm_format:
                raise ApplyRefused(409, "FORMAT_CONFIRMATION_REQUIRED", "confirm the card format first")
        backups = BackupStore(store.path.parent / "camera" / "backups")
        backup = backups.save(fresh.config.raw)
        backups.prune(cam.backup_count)
        try:
            patched = camera_config.patch(fresh.config.raw, changes)
            camera_config.self_check(fresh.config.raw, patched, changes)
        except camera_config.PatchError as error:
            raise ApplyRefused(422, "INVALID_CHANGES", str(error), errors=[str(error)]) from error
        try:
            camera_config.upload(address, patched, cam.read_timeout_seconds)
        except camera_config.UploadError as error:
            raise ApplyRefused(502, "UPLOAD_FAILED", str(error), backup=backup) from error
        logger.info("camera settings uploaded: %s", ", ".join(_describe(changes)))
        back = camera_config.wait_until_back(address, cam.verify_timeout_seconds, cam.verify_poll_seconds)
        if back is None:
            return ApplyResult(False, False, [], backup, _describe(changes))
        CameraStore(store.path.parent / "camera").save(back)
        kept = [f"{s}.{k}" for (s, k), v in changes.items() if back.config.value(s, k) != v]
        return ApplyResult(not kept, True, kept, backup, _describe(changes))
```

Routes in `api_camera.py`:

```python
@api_camera_bp.route("/pending", methods=["GET"])
@login_required
def pending() -> Response:
    """counts recordings still only on the camera (the format gate)."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    count = pending_recordings(store.get())
    if count is None:
        return _json({"code": "CAMERA_OFFLINE", "error": "the camera is not reachable"}, 409)
    return _json({"pending": count})


@api_camera_bp.route("/config", methods=["POST"])
@login_required
def apply() -> Response:
    """applies staged changes to the camera (design spec section 5)."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    body = request.get_json(silent=True) or {}
    raw = body.get("changes")
    if not isinstance(raw, dict) or not all(isinstance(v, str) for v in raw.values()):
        return _json({"code": "INVALID_CHANGES", "error": "changes must map Section.key to text"}, 422)
    try:
        result = apply_changes(store, raw, bool(body.get("confirm_format")))
    except ApplyRefused as refused:
        return _json({"code": refused.code, "error": str(refused), **refused.detail}, refused.status)
    return _json(dataclasses.asdict(result))
```

- [ ] **Step 4: Run** `venv/bin/python -m pytest test/test_camera_apply.py test/test_routes_api_camera.py test/test_sync_runner.py -q` -- all pass.
- [ ] **Step 5: Commit** `git commit -m "feat(camera): apply pipeline with format gate, backup and verify"`

---

### Task 6: Backups API and restore

**Files:** `blackvuesync_v2/server/routes/api_camera.py`, `blackvuesync_v2/server/camera_apply.py`; tests in `test/test_routes_api_camera.py`.

**Interfaces:** `camera_apply.restore_backup(store, name, confirm_format) -> ApplyResult` (changes = diff of fresh read vs backup, then `apply_changes` semantics via a shared `_apply(store, changes, confirm_format)` extracted from `apply_changes`); routes `GET /api/camera/backups` → `[{"name","created","size"}]`; `POST /api/camera/backups/<name>/restore` body `{"confirm_format": bool}` → `ApplyResult` or 404 `NO_SUCH_BACKUP` / the apply codes.

- [ ] **Step 1: Tests:** list returns newest first with ISO `created`; restore of a backup differing only in `VOLUME` uploads once and verifies; restore whose backup has a different `TimeZone` while `vod` is non-empty → 409 `FORMAT_BLOCKED` and no upload (review focus 5); unknown or malformed name → 404; restore identical to the current file → 422 `NO_CHANGES`.
- [ ] **Step 2: fail. Step 3: implement** by refactoring `apply_changes` into `_apply(store, changes: dict[tuple[str,str],str], confirm_format)` (everything after `prepare_changes`) and adding:

```python
def restore_backup(store: SettingsStore, name: str, confirm_format: bool) -> ApplyResult:
    """uploads a backup by applying its differences to the camera's current file."""
    raw = BackupStore(store.path.parent / "camera" / "backups").read(name)
    if raw is None:
        raise ApplyRefused(404, "NO_SUCH_BACKUP", "no such backup")
    settings = store.get()
    fresh = camera_config.fetch(settings.connection.address, settings.camera.read_timeout_seconds)
    if fresh is None:
        raise ApplyRefused(409, "CAMERA_OFFLINE", "the camera is not reachable")
    changes = {
        (c.section, c.key): c.after
        for c in camera_config.diff(fresh.config, camera_config.parse(raw))
        if c.after is not None and c.before is not None
    }
    if not changes:
        raise ApplyRefused(422, "NO_CHANGES", "the camera already has these settings")
    return _apply(store, changes, confirm_format)
```

- [ ] **Step 4: Run. Step 5: Commit** `git commit -m "feat(camera): list and restore config.ini backups"`

---

### Task 7: Editable panes, staging and the review dialog

**Files:** `blackvuesync_v2/server/templates/_partials/camera_panes.html`, `blackvuesync_v2/server/routes/hx_camera.py`, `blackvuesync_v2/server/static/js/settings.js`, `blackvuesync_v2/server/static/css/settings.css`, `blackvuesync_v2/server/templates/settings.html` (Backups nav item + review `<dialog>`), `blackvuesync_v2/server/camera_schema.py` (expose `widget` and `options` on `FieldView`); tests `test/test_routes_hx_camera.py`, `test/test_camera_schema.py`.

**Behaviour (spec section 6):**

- `FieldView` gains `widget: str` and `options: tuple[tuple[str, str], ...]`; `_view` copies them from the descriptor.
- The `rows` macro renders per widget: `select` → `<select data-camera-key="…" data-original="…">`; `toggle` → checkbox; `number`/`hour` → `<input type="number">`; `text` → text input (`maxlength` 20 for `userString`); `password` → `<input type="password" placeholder="unchanged" autocomplete="new-password">` plus the existing reveal button reading the current value into the input when pressed; read-only keys render as today. Every editable element carries `data-camera-key` and `data-original` (passwords: `data-original=""`).
- When `online` is false, inputs are rendered `disabled` and the banner stays.
- A sixth pane `data-pane="camera-backups"` lists `GET /api/camera/backups` with a **Restore** button per row (confirm via the same review dialog, which calls the restore endpoint).
- A sticky bar `#camera-apply-bar` (hidden until there are changes): "N changes", **Discard**, **Review & apply**.
- `settings.js` additions (CSP-clean, delegated `input`/`change` listeners on `#camera-panes`): `cameraChanges()` collects `{key: value}` where value differs from `data-original` (password inputs: non-empty only); `discardCameraChanges()` resets inputs; `openCameraReview()` fills `<dialog id="camera-review">` with a Setting/From/To table (passwords show "changed"), a warning when any key is in the format set (badge text "erases camera recordings"), a Wi-Fi warning when any key starts with `Wifi.` or `Cloud.`, then calls `GET /api/camera/pending` and, if pending > 0 and a format key is staged, disables **Apply** and shows "N recordings are not downloaded yet. Run Sync now first." with a link to the dashboard; otherwise a checkbox "I understand the camera will erase its recordings" gates **Apply** for format keys; `applyCamera()` POSTs with the CSRF token, shows the progress steps text, then the result (`verified`, `kept`, `came_back`), and triggers an htmx refresh of `#camera-panes`; `beforeunload` warns while changes exist; an `htmx:afterSwap` of the panes clears staged state.
- Keep the Alpine component free of per-element expressions (methods only).

- [ ] **Step 1: Route and schema tests:** `/hx/camera/panes` contains `<select data-camera-key="Tab1.VideoQuality"` with `data-original="0"`, a checkbox for `Tab3.RECLED`, a number input for `Tab3.VOLUME` with `value="5"`, a password input for `Cloud.sta_pw` with empty value and `placeholder="unchanged"`, no `data-camera-key` for `Tab1.RecordTime`, and `id="camera-apply-bar"`; offline → every `data-camera-key` element has `disabled`; `/settings` has `data-section-nav="camera-backups"` and `id="camera-review"`; the fixture's ciphertexts and plaintexts are absent.
- [ ] **Step 2: fail. Step 3: implement** per the behaviour list. **Step 4: run** the hx/camera tests and `test_settings_page.py`. **Step 5: Commit** `git commit -m "feat(camera): editable panes, staged changes and review dialog"`

---

### Task 8: Browser tests

**Files:** `test/e2e/conftest.py` (fake camera gains `do_POST` for `/upload.cgi` and a `vod` list served at `/blackvue_vod.cgi`), `test/e2e/test_camera_settings.py` (append).

- [ ] **Step 1:** Extend `FakeCameraServer`: `self.config` bytes, `self.vod: list[str]`, `do_POST` parses the multipart body with `email.message_from_bytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)` and stores the `FileData` part's payload as `self.config`; `do_GET` of `/blackvue_vod.cgi` returns `"\n".join(f"n:/Record/{n},s:1" for n in self.vod)`.
- [ ] **Step 2: Tests:**
  - `test_stage_review_apply`: change `Tab3.VOLUME` on System and `Cloud.sta2_ssid` on Cloud; the apply bar shows "2 changes"; **Review & apply** lists both rows; **Apply** → result text contains "Applied and verified"; after the refresh the System pane shows the new value; `fake_camera.config` contains `VOLUME=4`.
  - `test_format_key_is_blocked_while_recordings_are_pending`: `fake_camera.vod = ["20261027_210000_NF.mp4"]`; change `Tab1.TimeZone`; the review dialog shows "1 recording" and the Apply button is disabled; no upload happened.
  - `test_format_key_needs_the_checkbox_when_nothing_is_pending`: Apply disabled until the checkbox is ticked, then enabled.
  - `test_discard_resets_fields_and_hides_the_bar`.
  - `test_backups_pane_lists_and_restores`: after one apply, the Backups pane shows one row; **Restore** → dialog → Apply → `fake_camera.config` equals the original fixture.
- [ ] **Step 3:** Run `venv/bin/python -m pytest test/e2e/test_camera_settings.py test/e2e/test_contrast.py -m e2e -q`, then the WebKit Docker run from the phase-A plan (Task 9, Step 5) for the same files. Fix any contrast finding with tokens only.
- [ ] **Step 4: Commit** `git commit -m "test(camera): browser tests for staged apply, format gate and restore"`

---

### Task 9: Behave scenario

**Files:** `features/mock_dashcam/server.py` (routes `GET /Config/config.ini`, `GET /Config/version.bin`, `POST /upload.cgi`, `POST /mock/config` to seed), `features/camera_settings.feature`, `features/steps/camera_settings_steps.py`.

Read `features/CLAUDE.md` first. Scenario:

```gherkin
Feature: Camera settings

  Scenario: Apply one camera setting
    Given the dashcam config has "VOLUME" set to "5"
    When the camera setting "Tab3.VOLUME" is applied as "4"
    Then the dashcam config has "VOLUME" set to "4"
    Then the dashcam config differs only in "VOLUME"
```

Steps call `camera_config.fetch`, `camera_schema.prepare_changes`, `camera_config.patch`, `camera_config.self_check` and `camera_config.upload` against `context.mock_dashcam_address`, keeping the pre-upload bytes on `context` for the "differs only" assertion (`camera_config.diff`). The mock stores the uploaded bytes per affinity key like recordings.

- [ ] Run `venv/bin/behave features/camera_settings.feature`, then the whole `behave`. Commit `git commit -m "test(camera): behave scenario for a config upload"`.

---

### Task 10: Documentation

**Files:** `docs/guide/camera-settings.md` (editing, review dialog, the SD-card rule, backups and restore, recovery with the phone app), `docs/guide/troubleshooting.md` (camera did not come back; camera kept a value; FORMAT_BLOCKED), `docs/api.md` (`GET /api/camera/pending`, `POST /api/camera/config`, backups endpoints, every error code), `docs/guide/configuration.md` (already has the knobs from Task 4; link to the guide), `CHANGELOG.md` (Unreleased → Added), `CLAUDE.md` (`camera_apply.py`, `try_hold_sync_lock`, the format gate), `scripts/screenshots.py` (demo dashcam accepts `POST /upload.cgi`; capture the review dialog as `camera-review.png`).

- [ ] Write the docs; `venv/bin/mkdocs build --strict -q`; regenerate screenshots; commit `git commit -m "docs(camera): editing guide, api reference and screenshots"`.

---

### Task 11: Release gate on the real camera, then 3.3.0

Run by Kumar or with his explicit OK for each step, car parked at home, from the NAS. **Precondition for every step: `GET /api/camera/pending` on the deployed app returns 0.**

1. Deploy the PR build (image tag from the PR's `main` merge, or `:latest` after merge) on the NAS.
2. **No-op upload:** `POST /api/camera/config` with a change that sets `Tab3.VOLUME` to its current value is refused (`NO_CHANGES`), so use the Behave-style path: from the NAS run the upload of the camera's own unchanged file with `camera_config.upload` and confirm `GET /Config/config.ini` is byte-identical afterwards, noting whether the camera rebooted (port 80 gap) and whether any recording disappeared (vod count before/after).
3. **VOLUME 5 → 4 → 5** through Settings → Camera; each apply must report "Applied and verified".
4. Only then: merge, `release.py prepare 3.3.0`, screenshots, release PR, `release.py tag 3.3.0`, verify images.

If step 2 shows the camera formats its card on any upload, stop: every key becomes format-gated and the guide is updated before release.
