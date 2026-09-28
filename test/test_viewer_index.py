"""unit tests for recording enumeration + journey chaining."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

from blackvuesync_v2.server.viewer_index import (
    RecordingEntry,
    RecordingIndex,
    journey_chain,
    list_recordings,
    recording_index,
)


def _touch(root: Path, name: str) -> None:
    (root / name).write_bytes(b"x")


def test_groups_directions_and_detects_sidecars(tmp_path: Path) -> None:
    _touch(tmp_path, "20260607_101500_NF.mp4")
    _touch(tmp_path, "20260607_101500_NR.mp4")
    _touch(tmp_path, "20260607_101500_N.gps")
    _touch(tmp_path, "20260607_101500_NF.thm")
    entries = list_recordings(str(tmp_path), "none")
    assert len(entries) == 1
    e = entries[0]
    assert isinstance(e, RecordingEntry)
    assert e.base_filename == "20260607_101500"
    assert e.type == "N"
    assert e.directions == ("F", "R")
    assert e.has_gps is True
    assert e.has_3gf is False
    assert e.has_thm is True
    assert e.rel_dir == ""


def test_newest_first_and_grouping_subdir(tmp_path: Path) -> None:
    day = tmp_path / "2026-06-07"
    day.mkdir()
    _touch(day, "20260607_101500_NF.mp4")
    _touch(day, "20260607_101600_NF.mp4")
    entries = list_recordings(str(tmp_path), "daily")
    assert [e.base_filename for e in entries] == ["20260607_101600", "20260607_101500"]
    assert entries[0].rel_dir == "2026-06-07"


def test_journey_chain_links_contiguous_same_type_only() -> None:
    def entry(ts: str, typ: str = "N") -> RecordingEntry:
        import datetime

        dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
        return RecordingEntry(ts, typ, dt, ("F",), False, False, False, "")

    a, b, c = (
        entry("20260607_101500"),
        entry("20260607_101600"),
        entry("20260607_101700"),
    )
    far = entry("20260607_120000")  # >2 min later -> breaks the chain
    parking = entry("20260607_101800", "P")  # different type -> not chained
    chain = journey_chain([a, b, c, far, parking], "20260607_101500", "N")
    assert [e.base_filename for e in chain] == [
        "20260607_101500",
        "20260607_101600",
        "20260607_101700",
    ]


def _at(ts: str, typ: str) -> RecordingEntry:
    import datetime

    dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
    return RecordingEntry(ts, typ, dt, ("F",), False, False, False, "")


# real pattern from a dr900-series library: an event cuts the normal segment
# short (a 5 s stub) and takes over the next minute; parking runs into driving
_DRIVE = [
    _at("20260929_000156", "P"),
    _at("20260929_000256", "N"),
    _at("20260929_001144", "N"),
    _at("20260929_001149", "E"),
    _at("20260929_001249", "N"),
    _at("20260929_001349", "E"),
    _at("20260929_001449", "E"),
    _at("20260929_001549", "N"),
]


def test_continuous_chain_plays_every_type_in_time_order() -> None:
    chain = journey_chain(_DRIVE, "20260929_001144", "N", continuous=True)
    assert [(e.base_filename[9:], e.type) for e in chain] == [
        ("001144", "N"),
        ("001149", "E"),
        ("001249", "N"),
        ("001349", "E"),
        ("001449", "E"),
        ("001549", "N"),
    ]
    # parking into driving is one journey too
    from_parking = journey_chain(_DRIVE, "20260929_000156", "P", continuous=True)
    assert [e.type for e in from_parking] == ["P", "N"]


def test_same_type_chain_is_unchanged_without_continuous() -> None:
    """the default skips what lies between segments of the selected type."""
    chain = journey_chain(_DRIVE, "20260929_001144", "N")
    # skips the one-minute event, stops at the two-minute one
    assert [e.base_filename[9:] for e in chain] == ["001144", "001249"]
    events = journey_chain(_DRIVE, "20260929_001149", "E")
    # skips the normal minute between the events (120 s apart, inclusive)
    assert [e.base_filename[9:] for e in events] == ["001149", "001349", "001449"]


def test_continuous_chain_skips_a_second_type_at_the_same_instant() -> None:
    entries = [
        _at("20260607_101500", "N"),
        _at("20260607_101500", "E"),
        _at("20260607_101600", "N"),
    ]
    chain = journey_chain(entries, "20260607_101500", "N", continuous=True)
    assert [(e.base_filename, e.type) for e in chain] == [
        ("20260607_101500", "N"),
        ("20260607_101600", "N"),
    ]
    # the start is matched on type as well as time
    started_on_event = journey_chain(entries, "20260607_101500", "E", continuous=True)
    assert [e.type for e in started_on_event] == ["E", "N"]


def test_journey_chain_start_not_found_returns_empty() -> None:
    import datetime

    dt = datetime.datetime(2026, 6, 7, 10, 15, 0)
    e = RecordingEntry("20260607_101500", "N", dt, ("F",), False, False, False, "")
    assert journey_chain([e], "20260607_999999", "N") == []


def test_journey_chain_gap_boundary_inclusive_at_120s() -> None:
    import datetime

    base = datetime.datetime(2026, 6, 7, 10, 15, 0)

    def at(seconds: int) -> RecordingEntry:
        return RecordingEntry(
            f"ts_{seconds}",
            "N",
            base + datetime.timedelta(seconds=seconds),
            ("F",),
            False,
            False,
            False,
            "",
        )

    # ts_120 is exactly at the 120-second boundary from ts_0 -- included.
    # ts_241 is 121 seconds after ts_120 -- exceeds the gap window, breaks the
    # chain.
    chain = journey_chain([at(0), at(120), at(241)], "ts_0", "N")
    assert [e.base_filename for e in chain] == ["ts_0", "ts_120"]


def test_upload_flag_keeps_real_filenames(tmp_path: Path) -> None:
    # sync.py stores the .thm/.gps/.3gf of a flagged recording without the flag
    _touch(tmp_path, "20260607_101500_NFL.mp4")
    _touch(tmp_path, "20260607_101500_NRS.mp4")
    _touch(tmp_path, "20260607_101500_NR.thm")
    _touch(tmp_path, "20260607_101500_N.gps")
    _touch(tmp_path, "20260607_101500_N.3gf")
    (e,) = list_recordings(str(tmp_path), "none")
    assert e.directions == ("F", "R")
    assert e.video_files == (
        ("F", "20260607_101500_NFL.mp4"),
        ("R", "20260607_101500_NRS.mp4"),
    )
    assert e.thumb_files == (("R", "20260607_101500_NR.thm"),)
    assert e.has_thm is True
    assert e.has_gps is True and e.has_3gf is True


def test_flagged_thumbnail_name_is_accepted(tmp_path: Path) -> None:
    _touch(tmp_path, "20260607_101500_NFL.mp4")
    _touch(tmp_path, "20260607_101500_NFL.thm")
    (e,) = list_recordings(str(tmp_path), "none")
    assert e.thumb_files == (("F", "20260607_101500_NFL.thm"),)


def test_unflagged_video_preferred_when_both_present(tmp_path: Path) -> None:
    _touch(tmp_path, "20260607_101500_NFL.mp4")
    _touch(tmp_path, "20260607_101500_NF.mp4")
    (e,) = list_recordings(str(tmp_path), "none")
    assert e.video_files == (("F", "20260607_101500_NF.mp4"),)


def _age(path: Path, seconds: int = 3600) -> None:
    """backdates path's mtime past the racy window so the cache trusts it."""
    past = time.time() - seconds
    os.utime(path, (past, past))


def _daily_tree(root: Path) -> None:
    for day, names in {
        "2026-06-07": ("20260607_101500_NF.mp4", "20260607_101500_NR.mp4"),
        "2026-06-08": ("20260608_080000_EF.mp4",),
    }.items():
        (root / day).mkdir()
        for name in names:
            _touch(root / day, name)
        _age(root / day)
    _age(root)


def _count_scandirs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    real = os.scandir

    def spy(path: str) -> Any:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(os, "scandir", spy)
    return calls


def test_index_relists_only_changed_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daily_tree(tmp_path)
    index = RecordingIndex(str(tmp_path), "daily")
    calls = _count_scandirs(monkeypatch)
    assert len(index.entries()) == 2
    assert len(calls) == 3  # root + two day directories

    calls.clear()
    assert len(index.entries()) == 2
    assert calls == []  # nothing changed: stats only

    _touch(tmp_path / "2026-06-08", "20260608_090000_NF.mp4")
    _age(tmp_path / "2026-06-08", seconds=60)
    calls.clear()
    assert [e.base_filename for e in index.entries()][:2] == [
        "20260608_090000",
        "20260608_080000",
    ]
    assert calls == [str(tmp_path / "2026-06-08")]


def test_index_relists_a_directory_changed_within_the_racy_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _daily_tree(tmp_path)
    os.utime(tmp_path / "2026-06-08")  # mtime = now: listing is not trusted
    index = RecordingIndex(str(tmp_path), "daily")
    index.entries()
    calls = _count_scandirs(monkeypatch)
    index.entries()
    assert calls == [str(tmp_path / "2026-06-08")]


def test_index_days_find_and_removed_directory(tmp_path: Path) -> None:
    _daily_tree(tmp_path)
    index = RecordingIndex(str(tmp_path), "daily")
    assert [(d, len(recs)) for d, recs in index.days()] == [
        ("2026-06-08", 1),
        ("2026-06-07", 1),
    ]
    found = index.find("20260607_101500", "N")
    assert found is not None and found.directions == ("F", "R")
    assert index.find("20260607_101500", "E") is None

    for child in (tmp_path / "2026-06-08").iterdir():
        child.unlink()
    (tmp_path / "2026-06-08").rmdir()
    assert [d for d, _ in index.days()] == ["2026-06-07"]


def test_index_skips_hidden_and_nas_system_directories(tmp_path: Path) -> None:
    _daily_tree(tmp_path)
    for special in ("#recycle", "@eaDir", ".snapshot"):
        (tmp_path / special).mkdir()
        _touch(tmp_path / special, "20260601_000000_NF.mp4")
    index = RecordingIndex(str(tmp_path), "daily")
    assert {e.base_filename for e in index.entries()} == {
        "20260607_101500",
        "20260608_080000",
    }


def test_index_on_missing_destination_is_empty(tmp_path: Path) -> None:
    index = RecordingIndex(str(tmp_path / "absent"), "none")
    assert index.entries() == [] and index.days() == []


def test_recording_index_is_shared_per_destination_and_grouping(
    tmp_path: Path,
) -> None:
    first = recording_index(str(tmp_path), "daily")
    assert recording_index(str(tmp_path), "daily") is first
    other = recording_index(str(tmp_path), "none")
    assert other is not first
    assert recording_index(str(tmp_path), "daily") is not first
