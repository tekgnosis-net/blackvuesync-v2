"""regression tests for truncated downloads, lock fd leaks, progress counts,
early job failures, retention-timer races and serve-mode sync settings."""

from __future__ import annotations

import contextlib
import datetime
import errno
import os
import re
import threading
import time
import types
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import blackvuesync_v2.server.sync_runner as runner
import blackvuesync_v2.sync as _sync
from blackvuesync_v2.metrics import SyncMetrics
from blackvuesync_v2.server.progress import ProgressPublisher

_PAYLOAD = bytes(i % 256 for i in range(7000))
_FILENAME = "20230101_120000_NF.mp4"
_RANGE_RE = re.compile(r"bytes=(\d+)-")
_SHORT_BODY = 3000


class _TruncatingHandler(BaseHTTPRequestHandler):
    """advertises the full length but closes after _SHORT_BODY bytes; serves
    ranged requests in full so a later run can resume."""

    truncate = True

    def do_GET(self) -> None:  # noqa: N802
        rng = self.headers.get("Range")
        if rng and (m := _RANGE_RE.fullmatch(rng.strip())):
            start = int(m.group(1))
            body = _PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Range", f"bytes {start}-{len(_PAYLOAD) - 1}/{len(_PAYLOAD)}"
            )
            self.end_headers()
            self.wfile.write(body[:100] if type(self).truncate else body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD[:_SHORT_BODY])

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        """silences test server logging."""


@contextlib.contextmanager
def _truncating_server() -> Generator[str, None, None]:
    """runs _TruncatingHandler on an ephemeral port; yields the base url."""
    _TruncatingHandler.truncate = True
    server = HTTPServer(("127.0.0.1", 0), _TruncatingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()


def _isolate_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    """isolates sync.py module globals touched by these tests."""
    for name in (
        "dry_run",
        "affinity_key",
        "skip_metadata",
        "cutoff_date",
        "retry_failed_after",
        "today",
        "max_disk_used_percent",
    ):
        monkeypatch.setattr(_sync, name, getattr(_sync, name))
    monkeypatch.setattr(_sync, "dry_run", False)
    monkeypatch.setattr(_sync, "max_disk_used_percent", None)
    monkeypatch.setattr(_sync, "skip_metadata", set())


def _metrics() -> SyncMetrics:
    return SyncMetrics(run_start_monotonic=0.0, run_start_timestamp=0.0)


# ---------------------------------------------------------------------------
# truncated downloads
# ---------------------------------------------------------------------------


def test_truncated_download_keeps_partial_and_counts_network_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    metrics = _metrics()
    with _truncating_server() as url:
        downloaded, _ = _sync.download_file(
            url, _FILENAME, str(tmp_path), None, metrics
        )

    assert downloaded is False
    assert not (tmp_path / _FILENAME).exists()
    assert (tmp_path / f".{_FILENAME}").read_bytes() == _PAYLOAD[:_SHORT_BODY]
    assert metrics.file_download_failures_last_run is not None
    assert metrics.file_download_failures_last_run["network"] == 1
    # network failures are resumable, so no failure marker is written
    assert not (tmp_path / f"{_FILENAME}.failed").exists()


def test_truncated_resume_is_not_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    (tmp_path / f".{_FILENAME}").write_bytes(_PAYLOAD[:_SHORT_BODY])

    with _truncating_server() as url:
        downloaded, _ = _sync.download_file(url, _FILENAME, str(tmp_path), None)

    assert downloaded is False
    assert not (tmp_path / _FILENAME).exists()
    assert (tmp_path / f".{_FILENAME}").stat().st_size == _SHORT_BODY + 100


def test_truncated_download_resumes_to_completion_on_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    with _truncating_server() as url:
        _sync.download_file(url, _FILENAME, str(tmp_path), None)
        _TruncatingHandler.truncate = False
        downloaded, _ = _sync.download_file(url, _FILENAME, str(tmp_path), None)

    assert downloaded is True
    assert (tmp_path / _FILENAME).read_bytes() == _PAYLOAD
    assert not (tmp_path / f".{_FILENAME}").exists()


def test_truncated_download_reports_failed_file_to_publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    monkeypatch.setattr(_sync, "skip_metadata", {"t", "3", "g"})
    recording = _sync.to_recording(_FILENAME, "none")
    assert recording is not None
    pub = ProgressPublisher()
    pub.begin_job(1)

    with _truncating_server() as url:
        _sync.download_recording(url, recording, str(tmp_path), None, pub)

    snap = pub.snapshot()
    assert snap.files_failed == 1
    assert snap.files_completed == 0


# ---------------------------------------------------------------------------
# lock fd
# ---------------------------------------------------------------------------


def test_unlock_closes_lock_fd(tmp_path: Path) -> None:
    fd = _sync.lock(str(tmp_path))
    _sync.unlock(fd)

    with pytest.raises(OSError) as excinfo:
        os.fstat(fd)
    assert excinfo.value.errno == errno.EBADF


def test_lock_can_be_reacquired_after_unlock(tmp_path: Path) -> None:
    _sync.unlock(_sync.lock(str(tmp_path)))
    _sync.unlock(_sync.lock(str(tmp_path)))


# ---------------------------------------------------------------------------
# progress counts
# ---------------------------------------------------------------------------


def _existing_recording(tmp_path: Path) -> _sync.Recording:
    recording = _sync.to_recording(_FILENAME, "none")
    assert recording is not None
    for _artifact, _key, build in _sync._ARTIFACTS:
        (tmp_path / build(recording)).write_bytes(b"x")
    return recording


def test_existing_files_are_skipped_not_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    _existing_recording(tmp_path)
    monkeypatch.setattr(_sync, "get_dashcam_filenames", lambda _url: [_FILENAME])
    pub = ProgressPublisher()

    _sync.sync(
        "unused", str(tmp_path), "none", "date", None, None, publisher=pub, job_id="j"
    )

    snap = pub.snapshot()
    assert snap.state == "complete"
    assert snap.files_skipped == 4
    assert snap.files_failed == 0
    assert snap.files_completed == 0
    assert snap.files_total == 0


def test_files_total_counts_artifacts_not_recordings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    monkeypatch.setattr(_sync, "skip_metadata", {"g"})
    monkeypatch.setattr(
        _sync,
        "get_dashcam_filenames",
        lambda _url: [_FILENAME, "20230101_120100_NF.mp4"],
    )
    totals: list[int] = []
    pub = ProgressPublisher()
    original = pub.set_total

    def _record_total(files_total: int) -> None:
        totals.append(files_total)
        original(files_total)

    monkeypatch.setattr(pub, "set_total", _record_total)
    monkeypatch.setattr(_sync, "download_recording", lambda *_a, **_k: None)

    _sync.sync(
        "unused", str(tmp_path), "none", "date", None, None, publisher=pub, job_id="j"
    )

    assert totals == [6]
    assert pub.snapshot().files_total == 6


def test_skip_file_reduces_total_and_percent_reaches_100() -> None:
    pub = ProgressPublisher()
    pub.begin_job(0)
    pub.set_total(3)
    pub.skip_file()
    pub.skip_file()
    pub.start_file(_FILENAME, "mp4", 10)
    pub.finish_file(success=True)

    snap = pub.snapshot()
    assert (snap.files_total, snap.files_skipped, snap.files_completed) == (1, 2, 1)
    assert snap.percent == 100.0


# ---------------------------------------------------------------------------
# early failures
# ---------------------------------------------------------------------------


def test_listing_failure_ends_job_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)

    def _unavailable(_url: str) -> list[str]:
        raise UserWarning("Dashcam unavailable : test")

    monkeypatch.setattr(_sync, "get_dashcam_filenames", _unavailable)
    pub = ProgressPublisher()

    with pytest.raises(UserWarning):
        _sync.sync(
            "unused",
            str(tmp_path),
            "none",
            "date",
            None,
            None,
            publisher=pub,
            job_id="early",
        )

    snap = pub.snapshot()
    assert snap.job_id == "early"
    assert snap.state == "failed"


def _settings(destination: Path, **sync_overrides: Any) -> types.SimpleNamespace:
    sync_values: dict[str, Any] = {
        "grouping": "none",
        "priority": "date",
        "include": (),
        "exclude": (),
        "retry_failed_after": "1d",
        "skip_metadata": (),
        "affinity_key": None,
    }
    keep = sync_overrides.pop("keep", "")
    sync_values.update(sync_overrides)
    return types.SimpleNamespace(
        connection=types.SimpleNamespace(address="1.2.3.4", timeout_seconds=10.0),
        system=types.SimpleNamespace(destination=str(destination), dry_run=False),
        sync=types.SimpleNamespace(**sync_values),
        retention=types.SimpleNamespace(keep=keep, max_used_disk_percent=90),
        metrics=types.SimpleNamespace(
            file=None,
            pushgateway_url=None,
            job="blackvuesync",
            instance=None,
            state_file=str(destination / "metrics-state.json"),
        ),
        stats=types.SimpleNamespace(retention_days=365),
    )


def test_do_sync_failure_before_sync_ends_job_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)

    def _busy(_destination: str) -> int:
        raise UserWarning("Another instance is already running")

    monkeypatch.setattr(_sync, "lock", _busy)
    pub = ProgressPublisher()

    runner._do_sync(_settings(tmp_path), pub, job_id="lockfail")

    snap = pub.snapshot()
    assert snap.job_id == "lockfail"
    assert snap.state == "failed"


def test_409_during_startup_returns_running_job_id() -> None:
    started = threading.Event()
    proceed = threading.Event()

    def _slow(*_args: Any, **_kwargs: Any) -> None:
        # never begins a publisher job, as during lock/listing
        started.set()
        proceed.wait(timeout=5.0)

    pub = ProgressPublisher()
    try:
        with patch.object(runner, "_do_sync", side_effect=_slow):
            first = runner.trigger_sync(object(), pub)
            started.wait(timeout=2.0)
            second = runner.trigger_sync(object(), pub)
    finally:
        proceed.set()
    for _ in range(50):
        if not runner._sync_lock.locked():
            break
        time.sleep(0.02)

    assert second["status"] == "already_running"
    assert second["job_id"] == first["job_id"]


# ---------------------------------------------------------------------------
# retention timer race
# ---------------------------------------------------------------------------


def test_stale_reset_does_not_idle_a_newer_job() -> None:
    pub = ProgressPublisher()
    old = pub.begin_job(1)
    pub.end_job(success=True)
    pub.begin_job(1)

    # simulates the old timer firing after cancel() lost the race
    pub._reset_to_idle(old)
    assert pub.snapshot().state == "running"

    pub.end_job(success=True)
    pub._reset_to_idle(old)
    assert pub.snapshot().state == "complete"


def test_reset_to_idle_applies_to_its_own_job() -> None:
    pub = ProgressPublisher()
    job = pub.begin_job(1)
    pub.end_job(success=False)

    pub._reset_to_idle(job)

    assert pub.snapshot().state == "idle"


# ---------------------------------------------------------------------------
# serve-mode sync settings
# ---------------------------------------------------------------------------


def _capture_sync_globals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **sync_overrides: Any
) -> dict[str, Any]:
    _isolate_globals(monkeypatch)
    captured: dict[str, Any] = {}

    def _fake_sync(*_args: Any, **_kwargs: Any) -> None:
        for name in (
            "today",
            "cutoff_date",
            "retry_failed_after",
            "skip_metadata",
            "affinity_key",
        ):
            captured[name] = getattr(_sync, name)

    monkeypatch.setattr(_sync, "sync", _fake_sync)
    monkeypatch.setattr(_sync, "today", datetime.date(2000, 1, 1))
    runner._do_sync(
        _settings(tmp_path, **sync_overrides), ProgressPublisher(), job_id="s"
    )
    return captured


def test_do_sync_applies_retention_and_sync_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_sync_globals(
        tmp_path,
        monkeypatch,
        keep="1w",
        retry_failed_after="2h",
        skip_metadata=("t", "g"),
        affinity_key="k1",
    )

    today = datetime.date.today()
    assert captured["today"] == today
    assert captured["cutoff_date"] == today - datetime.timedelta(weeks=1)
    assert captured["retry_failed_after"] == datetime.timedelta(hours=2)
    assert captured["skip_metadata"] == {"t", "g"}
    assert captured["affinity_key"] == "k1"


def test_do_sync_empty_keep_keeps_forever(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_sync, "cutoff_date", datetime.date(2020, 1, 1))

    captured = _capture_sync_globals(tmp_path, monkeypatch, keep="", affinity_key="")

    assert captured["cutoff_date"] is None
    assert captured["affinity_key"] is None


def test_do_sync_invalid_retry_duration_fails_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolate_globals(monkeypatch)
    called = False

    def _fake_sync(*_args: Any, **_kwargs: Any) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(_sync, "sync", _fake_sync)
    pub = ProgressPublisher()

    with contextlib.suppress(Exception):
        runner._do_sync(
            _settings(tmp_path, retry_failed_after="bogus"), pub, job_id="bad"
        )

    assert called is False
    assert pub.snapshot().state == "failed"
