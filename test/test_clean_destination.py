"""tests for clean_destination and prune_orphan_partials."""

from __future__ import annotations

from pathlib import Path

import blackvuesync_v2.sync as _sync
from blackvuesync_v2.sync import clean_destination, prune_orphan_partials


def _touch(p: Path) -> None:
    p.write_bytes(b"x")


class TestCleanDestinationKeepsPartials:
    def test_partial_dotfiles_are_not_removed(self, tmp_path: Path) -> None:
        partial = tmp_path / ".20230101_120000_NF.mp4"
        _touch(partial)
        clean_destination(str(tmp_path), "none")
        assert partial.exists()

    def test_empty_group_directories_still_removed(self, tmp_path: Path) -> None:
        group = tmp_path / "2023-01-01"
        group.mkdir()
        clean_destination(str(tmp_path), "daily")
        assert not group.exists()


class TestPruneOrphanPartials:
    def test_removes_partials_not_in_expected_set(self, tmp_path: Path) -> None:
        keep = tmp_path / ".20230101_120000_NF.mp4"
        orphan = tmp_path / ".20220101_120000_NF.mp4"
        _touch(keep)
        _touch(orphan)
        prune_orphan_partials(str(tmp_path), {"20230101_120000_NF.mp4"})
        assert keep.exists()
        assert not orphan.exists()

    def test_dry_run_keeps_everything(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        orphan = tmp_path / ".20220101_120000_NF.mp4"
        _touch(orphan)
        monkeypatch.setattr(_sync, "dry_run", True)  # type: ignore[attr-defined]
        prune_orphan_partials(str(tmp_path), set())
        assert orphan.exists()
