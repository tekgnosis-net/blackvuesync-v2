"""tests that _scheduled_run honors the schedule.paused flag."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from blackvuesync_v2.server.progress import ProgressPublisher
from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


class TestScheduledRunPause:
    """tests for _scheduled_run skipping when settings.schedule.paused is True."""

    def test_skips_when_paused(self, settings_path: Path) -> None:
        """when paused=True, _scheduled_run logs and does not call trigger_sync."""
        from blackvuesync_v2.server.scheduler import _scheduled_run

        store = _make_store(settings_path)
        store.update(
            lambda s: dataclasses.replace(
                s, schedule=dataclasses.replace(s.schedule, paused=True)
            )
        )
        publisher = ProgressPublisher()

        with patch("blackvuesync_v2.server.scheduler.trigger_sync") as mock_trigger:
            _scheduled_run(store, publisher)
            mock_trigger.assert_not_called()

    def test_runs_when_not_paused(self, settings_path: Path) -> None:
        """when paused=False (default), _scheduled_run calls trigger_sync."""
        from blackvuesync_v2.server.scheduler import _scheduled_run

        store = _make_store(settings_path)
        publisher = ProgressPublisher()

        with patch(
            "blackvuesync_v2.server.scheduler.trigger_sync",
            return_value={"status": "started", "job_id": "deadbeef"},
        ) as mock_trigger:
            _scheduled_run(store, publisher)
            mock_trigger.assert_called_once()

    def test_resume_after_pause(self, settings_path: Path) -> None:
        """toggling paused=True then False restores normal scheduling."""
        from blackvuesync_v2.server.scheduler import _scheduled_run

        store = _make_store(settings_path)
        publisher = ProgressPublisher()

        # pause: should skip
        store.update(
            lambda s: dataclasses.replace(
                s, schedule=dataclasses.replace(s.schedule, paused=True)
            )
        )
        with patch("blackvuesync_v2.server.scheduler.trigger_sync") as mock_trigger:
            _scheduled_run(store, publisher)
            assert mock_trigger.call_count == 0

        # resume: should run
        store.update(
            lambda s: dataclasses.replace(
                s, schedule=dataclasses.replace(s.schedule, paused=False)
            )
        )
        with patch(
            "blackvuesync_v2.server.scheduler.trigger_sync",
            return_value={"status": "started", "job_id": "deadbeef"},
        ) as mock_trigger:
            _scheduled_run(store, publisher)
            assert mock_trigger.call_count == 1
