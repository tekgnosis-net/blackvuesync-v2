"""tests for scripts/release.py's pure helpers and the single version source."""

from __future__ import annotations

import datetime
import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_script", REPO / "scripts" / "release.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


release = _load()

CHANGELOG = """# CHANGELOG

<!-- --8<-- [start:changelog] -->
intro

## Unreleased

* fix a thing

## 3.0.0 - 2026-09-28

### Fixed

* old fix
<!-- --8<-- [end:changelog] -->
"""


def test_parse_version_accepts_only_major_minor_patch() -> None:
    assert release.parse_version("3.10.0") == (3, 10, 0)
    for bad in ("3.0", "v3.0.0", "3.0.0-rc.1", "3.0.0a0", "03.0.0", ""):
        with pytest.raises(release.ReleaseError):
            release.parse_version(bad)


def test_highest_tag_ignores_non_semver_tags() -> None:
    tags = ["1.7", "v2.0.0", "v2.10.1", "v2.9.9", "vnext", "v3.0"]
    assert release.highest_tag(tags) == (2, 10, 1)
    assert release.highest_tag(["1.7"]) is None


def test_set_version_replaces_the_single_assignment() -> None:
    text = 'x = 1\n__version__ = "3.0.0"\n'
    assert '__version__ = "3.1.0"' in release.set_version(
        text, release.INIT_RE, "3.1.0", "init"
    )
    with pytest.raises(release.ReleaseError):
        release.set_version("nothing here", release.INIT_RE, "3.1.0", "init")


def test_date_changelog_promotes_unreleased() -> None:
    dated = release.date_changelog(CHANGELOG, "3.0.1", datetime.date(2026, 10, 1))
    assert "## 3.0.1 - 2026-10-01" in dated
    assert "## Unreleased" not in dated
    assert release.changelog_section(dated, "3.0.1") == "* fix a thing"


def test_date_changelog_dates_a_named_unreleased_section() -> None:
    text = CHANGELOG.replace("## Unreleased", "## 3.1.0 - unreleased")
    dated = release.date_changelog(text, "3.1.0", datetime.date(2026, 10, 2))
    assert "## 3.1.0 - 2026-10-02" in dated


def test_date_changelog_rejects_missing_or_empty_sections() -> None:
    with pytest.raises(release.ReleaseError):
        release.date_changelog(
            CHANGELOG.replace("## Unreleased", "## Later"), "3.0.1",
            datetime.date(2026, 10, 1),
        )  # fmt: skip
    empty = CHANGELOG.replace("* fix a thing\n", "")
    with pytest.raises(release.ReleaseError):
        release.date_changelog(empty, "3.0.1", datetime.date(2026, 10, 1))


def test_changelog_section_stops_at_the_include_end_marker() -> None:
    section = release.changelog_section(CHANGELOG, "3.0.0")
    assert section == "### Fixed\n\n* old fix"
    assert release.changelog_section(CHANGELOG, "9.9.9") == ""


def test_check_versions_reports_the_disagreeing_file() -> None:
    with pytest.raises(release.ReleaseError, match="pyproject.toml says 3.0.0"):
        release.check_versions(
            "3.1.0", '__version__ = "3.1.0"\n', 'version = "3.0.0"\n'
        )


def test_code_versions_agree_and_have_a_changelog_section() -> None:
    """the package version lives in two files; they must never drift."""
    init_version = release.read_version(
        release.INIT_FILE.read_text(), release.INIT_RE, "__init__.py"
    )
    pyproject_version = release.read_version(
        release.PYPROJECT.read_text(), release.PYPROJECT_RE, "pyproject.toml"
    )
    assert init_version == pyproject_version
    assert re.fullmatch(r"\d+\.\d+\.\d+", init_version)
    assert release.changelog_section(release.CHANGELOG.read_text(), init_version)
