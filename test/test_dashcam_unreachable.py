"""tests for the dashcam-not-reachable state: exception, runner log, stats row."""

from __future__ import annotations

import errno
import http.client
import logging
import socket
import types
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import pytest

import blackvuesync_v2.server.sync_runner as runner
import blackvuesync_v2.sync as _sync
from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.server.stats_store import StatsStore
from blackvuesync_v2.sync import DashcamUnavailableError


def _raise(error: BaseException) -> Any:
    def fake_urlopen(*_args: Any, **_kwargs: Any) -> Any:
        raise error

    return fake_urlopen


def test_unreachable_errors_raise_the_specific_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """car away: host down/unreachable, timeouts and dropped connections."""
    for error in (
        urllib.error.URLError(OSError(errno.EHOSTUNREACH, "No route to host")),
        urllib.error.URLError(OSError(errno.EHOSTDOWN, "Host is down")),
        urllib.error.URLError(TimeoutError("timed out")),
        socket.timeout("timed out"),
        http.client.RemoteDisconnected("closed"),
    ):
        monkeypatch.setattr(urllib.request, "urlopen", _raise(error))
        with pytest.raises(DashcamUnavailableError) as caught:
            _sync.get_dashcam_filenames("http://192.0.2.1")
        # still a UserWarning, so the cli's handling is unchanged
        assert isinstance(caught.value, UserWarning)


def test_refused_connection_is_not_treated_as_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """a refusal means something answered: a misconfiguration, not the car away."""
    refused = urllib.error.URLError(OSError(errno.ECONNREFUSED, "Connection refused"))
    monkeypatch.setattr(urllib.request, "urlopen", _raise(refused))
    with pytest.raises(RuntimeError):
        _sync.get_dashcam_filenames("http://192.0.2.1")


def _settings(destination: Path) -> Any:
    return types.SimpleNamespace(
        connection=types.SimpleNamespace(address="192.0.2.1", timeout_seconds=10.0),
        system=types.SimpleNamespace(destination=str(destination), dry_run=False),
        sync=types.SimpleNamespace(
            grouping="none",
            priority="date",
            include=(),
            exclude=(),
            retry_failed_after="1d",
            skip_metadata=(),
            affinity_key=None,
        ),
        retention=types.SimpleNamespace(keep="", max_used_disk_percent=90),
        metrics=types.SimpleNamespace(
            file=None,
            pushgateway_url=None,
            job="blackvuesync",
            instance=None,
            state_file="",
        ),
        stats=types.SimpleNamespace(retention_days=365),
    )


def _run_with(
    error: BaseException,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> StatsStore:
    destination = tmp_path / "rec"
    destination.mkdir()
    monkeypatch.setattr(_sync, "ensure_destination", lambda _d: None)
    monkeypatch.setattr(_sync, "lock", lambda _d: 1)
    monkeypatch.setattr(_sync, "unlock", lambda _fd: None)
    monkeypatch.setattr(_sync, "clean_destination", lambda _d, _g: None)

    def fake_sync(*_args: Any, **_kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(_sync, "sync", fake_sync)
    store = StatsStore(str(tmp_path / "stats.db"))
    runner._do_sync(
        _settings(destination), ProgressPublisher(), job_id="j", stats_store=store
    )
    return store


def test_unreachable_run_logs_one_info_line_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=runner.logger.name)
    store = _run_with(
        DashcamUnavailableError("Dashcam unavailable : [Errno 113] No route to host"),
        tmp_path,
        monkeypatch,
    )
    records = [r for r in caplog.records if r.name == runner.logger.name]
    assert [r.levelno for r in records] == [logging.INFO]
    assert "192.0.2.1 not reachable" in records[0].getMessage()
    assert records[0].exc_info is None
    row = store.latest()
    assert row is not None and row.success == 0
    assert {reason: n for reason, n in row.failures.items() if n} == {"network": 1}


def test_other_failures_still_log_a_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=runner.logger.name)
    _run_with(RuntimeError("Not enough disk space left."), tmp_path, monkeypatch)
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors and errors[0].exc_info is not None


def test_stored_unreachable_run_renders_as_not_reachable_on_the_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """end to end: the runner's stored row drives the card's label."""
    import dataclasses
    import os
    from unittest.mock import patch

    from blackvuesync_v2.server import create_app
    from blackvuesync_v2.server.auth import (
        SESSION_VERSION_KEY,
        hash_password,
        session_version,
    )
    from blackvuesync_v2.settings import SettingsStore

    stats = _run_with(
        DashcamUnavailableError("Dashcam unavailable : <urlopen error timed out>"),
        tmp_path,
        monkeypatch,
    )
    with patch.dict(os.environ, {"ADDRESS": "192.0.2.1"}, clear=False):
        settings = SettingsStore(tmp_path / "settings.json")
    pw_hash = hash_password("pw-1234-test")
    settings.update(
        lambda s: dataclasses.replace(
            s, auth=dataclasses.replace(s.auth, password_hash=pw_hash)
        )
    )
    app = create_app(settings, testing=True, stats_store=stats)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "admin"
        sess[SESSION_VERSION_KEY] = session_version(pw_hash)
    body = client.get("/hx/sync/last-run-card").data
    assert b"dashcam not reachable" in body
    assert b"badge-failed" not in body
