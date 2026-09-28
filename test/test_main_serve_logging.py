"""tests that cmd_serve wires up logging on startup."""

from __future__ import annotations

import argparse
import dataclasses
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from blackvuesync_v2.settings import SettingsStore


@pytest.fixture()
def settings_path(tmp_path: Path) -> Path:
    """returns a settings file path inside tmp_path."""
    return tmp_path / "settings.json"


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore with a dummy address."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _run_cmd_serve(settings_path: Path) -> tuple[Any, Any, Any, Any]:
    """invokes cmd_serve with logging / scheduler / waitress mocked.

    returns the four mocks so each test can assert call args.
    """
    from blackvuesync_v2.__main__ import cmd_serve

    args = argparse.Namespace(
        port=None,
        config_path=str(settings_path),
    )
    with (
        patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False),
        patch("blackvuesync_v2.__main__.configure_logging") as mock_cfg,
        patch("blackvuesync_v2.__main__.set_logging_levels") as mock_set,
        patch("waitress.serve") as mock_waitress,
        patch("blackvuesync_v2.server.scheduler.init_scheduler") as mock_init,
    ):
        mock_waitress.return_value = None
        cmd_serve(args)
        return mock_cfg, mock_set, mock_init, mock_waitress


def test_cmd_serve_invokes_logging_scheduler_and_waitress(
    settings_path: Path,
) -> None:
    """cmd_serve wires up the four side effects in order."""
    mock_cfg, mock_set, mock_init, mock_waitress = _run_cmd_serve(settings_path)
    # configure_logging is called three times: bootstrap default,
    # re-applied from settings.logging.format via _apply_logging_settings,
    # then once more to propagate the formatter to the serve-only handlers.
    assert mock_cfg.call_count == 3
    # set_logging_levels is called twice: bootstrap (verbose=1) then the
    # settings-driven level.
    assert mock_set.call_count == 2
    mock_init.assert_called_once()
    mock_waitress.assert_called_once()


def test_cmd_serve_applies_verbosity_floor_when_settings_verbose_is_zero(
    settings_path: Path,
) -> None:
    """default LoggingSettings.verbose=0 must be floored to 1 in serve mode
    so scheduler / waitress INFO startup lines emit."""
    _make_store(settings_path)  # creates a fresh settings.json with verbose=0
    _, mock_set, _, _ = _run_cmd_serve(settings_path)
    # the second (settings-applied) call is what we care about
    second_call_args = mock_set.call_args_list[1].args
    assert second_call_args == (1, False)


def test_cmd_serve_passes_high_verbosity_through(settings_path: Path) -> None:
    """settings.logging.verbose=2 should pass through unchanged."""
    store = _make_store(settings_path)
    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, verbose=2)
        )
    )
    _, mock_set, _, _ = _run_cmd_serve(settings_path)
    second_call_args = mock_set.call_args_list[1].args
    assert second_call_args == (2, False)


def test_cmd_serve_honors_quiet_setting(settings_path: Path) -> None:
    """settings.logging.quiet=True must map to verbosity=-1 (ERROR only)."""
    store = _make_store(settings_path)
    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, quiet=True)
        )
    )
    _, mock_set, _, _ = _run_cmd_serve(settings_path)
    second_call_args = mock_set.call_args_list[1].args
    assert second_call_args == (-1, False)


def test_cmd_serve_reapplies_log_format_from_settings(settings_path: Path) -> None:
    """configure_logging must be called with settings.logging.format after
    the store loads, so users who set format='json' actually get json logs."""
    store = _make_store(settings_path)
    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, format="json")
        )
    )
    mock_cfg, _, _, _ = _run_cmd_serve(settings_path)
    # first call is bootstrap with "text"; second is settings-driven "json".
    assert mock_cfg.call_args_list[0].args == ("text",)
    assert mock_cfg.call_args_list[1].args == ("json",)
