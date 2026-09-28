"""tests for live logging reload via _register_logging_reload and _apply_logging_settings."""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

from blackvuesync_v2.__main__ import _apply_logging_settings, _register_logging_reload
from blackvuesync_v2.settings import LoggingSettings, SettingsStore
from blackvuesync_v2.sync import TEXT_LOG_FORMAT, StructuredLogFormatter

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _make_store(settings_path: Path) -> SettingsStore:
    """creates a SettingsStore with a dummy address seeded from env."""
    with patch.dict(os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        return SettingsStore(settings_path)


def _root_formatter() -> logging.Formatter:
    """returns the formatter on the first root logging handler."""
    return logging.root.handlers[0].formatter  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _restore_logging() -> Generator[None, None, None]:
    """snapshots root logger level and handler formatters before each test,
    restores them after, so logging state does not bleed between tests."""
    root = logging.root
    saved_level = root.level
    saved_formatters = [h.formatter for h in root.handlers]
    # ensure a known baseline level and text formatter
    root.setLevel(logging.INFO)
    for handler in root.handlers:
        handler.setFormatter(logging.Formatter(TEXT_LOG_FORMAT))
    yield
    root.setLevel(saved_level)
    for handler, fmt in zip(root.handlers, saved_formatters):
        handler.setFormatter(fmt)


# ---------------------------------------------------------------------------
# _apply_logging_settings
# ---------------------------------------------------------------------------


def test_apply_logging_settings_verbose_1_sets_info() -> None:
    """verbose=1 maps to INFO level."""
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))
    assert logging.root.level == logging.INFO


def test_apply_logging_settings_verbose_2_sets_debug() -> None:
    """verbose=2 maps to DEBUG level."""
    _apply_logging_settings(LoggingSettings(verbose=2, quiet=False, format="text"))
    assert logging.root.level == logging.DEBUG


def test_apply_logging_settings_quiet_sets_error() -> None:
    """quiet=True maps to ERROR level regardless of verbose."""
    _apply_logging_settings(LoggingSettings(verbose=2, quiet=True, format="text"))
    assert logging.root.level == logging.ERROR


def test_apply_logging_settings_verbose_0_floored_to_info() -> None:
    """verbose=0 is floored to 1 (INFO) in serve mode."""
    _apply_logging_settings(LoggingSettings(verbose=0, quiet=False, format="text"))
    assert logging.root.level == logging.INFO


def test_apply_logging_settings_format_json_uses_structured_formatter() -> None:
    """format='json' installs StructuredLogFormatter on root handlers."""
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="json"))
    assert isinstance(_root_formatter(), StructuredLogFormatter)


def test_apply_logging_settings_format_text_uses_plain_formatter() -> None:
    """format='text' installs a plain logging.Formatter on root handlers."""
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))
    fmt = _root_formatter()
    assert type(fmt) is logging.Formatter  # not a subclass
    assert fmt._fmt == TEXT_LOG_FORMAT


# ---------------------------------------------------------------------------
# _register_logging_reload via store.update
# ---------------------------------------------------------------------------


def test_verbose_change_updates_level(tmp_path: Path) -> None:
    """increasing verbose from 1 to 2 switches root logger to DEBUG."""
    store = _make_store(tmp_path / "settings.json")
    _register_logging_reload(store)
    # start from a known level
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))
    assert logging.root.level == logging.INFO

    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, verbose=2)
        )
    )
    assert logging.root.level == logging.DEBUG


def test_quiet_true_drops_level_to_error(tmp_path: Path) -> None:
    """setting quiet=True via store.update drops the root level to ERROR."""
    store = _make_store(tmp_path / "settings.json")
    _register_logging_reload(store)
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))

    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, quiet=True)
        )
    )
    assert logging.root.level == logging.ERROR


def test_format_text_to_json_swaps_formatter(tmp_path: Path) -> None:
    """changing format from 'text' to 'json' installs StructuredLogFormatter."""
    store = _make_store(tmp_path / "settings.json")
    _register_logging_reload(store)
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))
    assert type(_root_formatter()) is logging.Formatter

    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, format="json")
        )
    )
    assert isinstance(_root_formatter(), StructuredLogFormatter)


def test_format_json_to_text_swaps_back(tmp_path: Path) -> None:
    """changing format from 'json' back to 'text' reinstalls plain Formatter."""
    store = _make_store(tmp_path / "settings.json")
    # set json as initial format in the stored settings
    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, format="json")
        )
    )
    _register_logging_reload(store)
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="json"))
    assert isinstance(_root_formatter(), StructuredLogFormatter)

    store.update(
        lambda s: dataclasses.replace(
            s, logging=dataclasses.replace(s.logging, format="text")
        )
    )
    assert type(_root_formatter()) is logging.Formatter


def test_non_logging_change_does_not_alter_logging(tmp_path: Path) -> None:
    """changing connection.timeout_seconds leaves formatter and level unchanged."""
    store = _make_store(tmp_path / "settings.json")
    _register_logging_reload(store)
    _apply_logging_settings(LoggingSettings(verbose=1, quiet=False, format="text"))
    level_before = logging.root.level
    formatter_before = _root_formatter()

    store.update(
        lambda s: dataclasses.replace(
            s,
            connection=dataclasses.replace(s.connection, timeout_seconds=30.0),
        )
    )
    assert logging.root.level == level_before
    assert _root_formatter() is formatter_before
