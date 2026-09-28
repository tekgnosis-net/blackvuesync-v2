"""tests for the cli entry point: settings file creation and waitress threads."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import blackvuesync_v2.__main__ as cli


@pytest.fixture()
def default_path(tmp_path: Path) -> Iterator[Path]:
    """points the default settings path at a file inside tmp_path."""
    path = tmp_path / "default" / "settings.json"
    with patch.object(cli, "_DEFAULT_SETTINGS_PATH", path):
        yield path


@pytest.mark.parametrize("argv", [["--help"], ["--version"], ["sync", "--help"]])
def test_help_and_version_do_not_create_settings(
    default_path: Path, argv: list[str]
) -> None:
    """verifies informational invocations leave no settings.json behind."""
    with patch("sys.argv", ["blackvuesync-v2", *argv]), pytest.raises(SystemExit):
        cli.main()
    assert not default_path.exists()


def test_sync_does_not_create_settings(default_path: Path) -> None:
    """verifies the sync subcommand is configured by its arguments alone."""
    with (
        patch("sys.argv", ["blackvuesync-v2", "sync", "192.168.0.1"]),
        patch.object(cli, "cmd_sync", return_value=0) as mock_sync,
    ):
        assert cli.main() == 0
    mock_sync.assert_called_once()
    assert not default_path.exists()


def test_serve_creates_settings_only_at_config_path(
    default_path: Path, tmp_path: Path
) -> None:
    """verifies serve --config-path seeds that file and not the default one."""
    config_path = tmp_path / "custom" / "settings.json"
    with (
        patch(
            "sys.argv", ["blackvuesync-v2", "serve", "--config-path", str(config_path)]
        ),
        patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False),
        patch.object(cli, "configure_logging"),
        patch.object(cli, "set_logging_levels"),
        patch("waitress.serve") as mock_serve,
        patch("blackvuesync_v2.server.scheduler.init_scheduler"),
    ):
        assert cli.main() == 0
    assert config_path.exists()
    assert not default_path.exists()
    assert mock_serve.call_args.kwargs["threads"] == cli.WAITRESS_THREADS
    assert cli.WAITRESS_THREADS > 4
