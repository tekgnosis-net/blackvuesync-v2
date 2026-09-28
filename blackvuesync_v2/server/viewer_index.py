"""enumerates downloaded recordings and computes auto-advance journey chains.

a recording instant is keyed by (base_filename, type); front/rear .mp4 share it
and differ by direction, and share one .gps/.3gf. an .mp4 may carry an upload
flag (`_NFL.mp4`), so the actual on-disk video/thumbnail filenames are kept per
direction; sync.py stores the .thm/.gps/.3gf without the flag. built on
sync.to_recording.
"""

from __future__ import annotations

import dataclasses
import datetime
import itertools
import os
import threading
import time

from blackvuesync_v2.sync import to_recording

# two segments are part of one journey when the next starts within this window
# of the prior (blackvue writes ~1-minute back-to-back segments).
_CONTIGUOUS_GAP = datetime.timedelta(seconds=120)


@dataclasses.dataclass(frozen=True)
class RecordingEntry:
    """one recording instant (base_filename + type) with its available artifacts."""

    # pylint: disable=too-many-instance-attributes
    base_filename: str
    type: str
    datetime: datetime.datetime
    directions: tuple[str, ...]
    has_gps: bool
    has_3gf: bool
    has_thm: bool
    rel_dir: str  # directory relative to destination ("" when ungrouped)
    # (direction, filename) pairs of the on-disk .mp4 / .thm, sorted by direction
    video_files: tuple[tuple[str, str], ...] = ()
    thumb_files: tuple[tuple[str, str], ...] = ()


@dataclasses.dataclass
class _InstantSlot:
    """accumulates per-direction .mp4 filenames for one recording instant."""

    dt: datetime.datetime
    videos: dict[str, list[str]] = dataclasses.field(default_factory=dict)


def _pick_video(names: list[str]) -> str:
    """picks one .mp4 per direction, preferring the unflagged (shortest) name."""
    return min(names, key=lambda n: (len(n), n))


def _thumb_for(rel_dir: str, video: str, plain_stem: str, present: set[str]) -> str:
    """returns the .thm filename for a video, or "" when none is on disk.

    sync.py names the thumbnail without the upload flag; a flagged thumbnail
    (same stem as the video) is also accepted.
    """
    for stem in dict.fromkeys((plain_stem, video[: -len(".mp4")])):
        if os.path.join(rel_dir, f"{stem}.thm") in present:
            return f"{stem}.thm"
    return ""


def _build_entry(
    rel_dir: str,
    base: str,
    rtype: str,
    slot: _InstantSlot,
    present: set[str],
) -> RecordingEntry:
    """constructs a RecordingEntry from a collected slot and present-file set."""
    dirs = sorted(slot.videos)
    videos = tuple((d, _pick_video(slot.videos[d])) for d in dirs)
    thumbs = tuple(
        (d, thm)
        for d, video in videos
        if (thm := _thumb_for(rel_dir, video, f"{base}_{rtype}{d}", present))
    )
    return RecordingEntry(
        base_filename=base,
        type=rtype,
        datetime=slot.dt,
        directions=tuple(dirs),
        has_gps=os.path.join(rel_dir, f"{base}_{rtype}.gps") in present,
        has_3gf=os.path.join(rel_dir, f"{base}_{rtype}.3gf") in present,
        has_thm=bool(thumbs),
        rel_dir=rel_dir,
        video_files=videos,
        thumb_files=thumbs,
    )


def _sort_newest_first(entries: list[RecordingEntry]) -> None:
    entries.sort(key=lambda e: (e.datetime, e.base_filename, e.rel_dir), reverse=True)


def _entries_in_dir(
    rel_dir: str, files: list[str], grouping: str
) -> list[RecordingEntry]:
    """builds the recording instants of one directory from its file names.

    sidecars (.thm/.gps/.3gf) always sit next to their .mp4, so a directory
    is self-contained and can be rebuilt on its own.
    """
    grouped: dict[tuple[str, str], _InstantSlot] = {}
    present = {os.path.join(rel_dir, name) for name in files}
    for name in files:
        rec = to_recording(name, grouping)
        if rec is None:
            continue  # only .mp4 names match to_recording
        slot = grouped.setdefault(
            (rec.base_filename, rec.type), _InstantSlot(dt=rec.datetime)
        )
        slot.videos.setdefault(rec.direction, []).append(name)
    return [
        _build_entry(rel_dir, base, rtype, slot, present)
        for (base, rtype), slot in grouped.items()
    ]


def list_recordings(destination: str, grouping: str) -> list[RecordingEntry]:
    """walks destination and returns recording instants, newest first (uncached)."""
    if not os.path.isdir(destination):
        return []
    entries: list[RecordingEntry] = []
    for root, _dirs, files in os.walk(destination):
        rel_dir = os.path.relpath(root, destination)
        rel_dir = "" if rel_dir == "." else rel_dir
        entries.extend(_entries_in_dir(rel_dir, files, grouping))
    _sort_newest_first(entries)
    return entries


# a directory listed within this many seconds of its mtime is re-listed on the
# next lookup: a file created in the same timestamp tick as the scan would not
# move the mtime again ("racy mtime"), so such a listing is never trusted.
_RACY_MTIME_WINDOW_NS = 2_000_000_000

# hidden directories and nas system directories (synology @eaDir, #recycle,
# #snapshot) never hold synced recordings.
_SKIPPED_DIR_PREFIXES = (".", "@", "#")


@dataclasses.dataclass
class _DirListing:
    """cached scan of one directory, valid while its mtime is unchanged."""

    mtime_ns: int
    subdirs: list[str]
    entries: list[RecordingEntry]
    racy: bool
    seq: int  # unique per scan, so a re-listed directory always forces a rebuild


@dataclasses.dataclass(frozen=True)
class _Views:
    """merged views over all directory listings, swapped in as one unit."""

    signature: tuple[tuple[str, int], ...] = ()
    entries: tuple[RecordingEntry, ...] = ()
    by_key: dict[tuple[str, str], RecordingEntry] = dataclasses.field(
        default_factory=dict
    )
    days: tuple[tuple[str, list[RecordingEntry]], ...] = ()


class RecordingIndex:
    """recording instants of one destination, cached per directory.

    a directory's mtime changes whenever an entry is created, removed or
    renamed in it, so each lookup costs one stat per directory and re-lists
    only the directories that changed (with daily grouping, usually today's).
    thread-safe; concurrent lookups share one scan.
    """

    def __init__(self, destination: str, grouping: str) -> None:
        self.destination = destination
        self.grouping = grouping
        self._lock = threading.Lock()
        self._dirs: dict[str, _DirListing] = {}
        self._scans = itertools.count()
        self._views = _Views()

    def _listing(self, rel_dir: str, now_ns: int) -> _DirListing | None:
        """returns the cached listing of rel_dir, re-listing it when stale."""
        path = os.path.join(self.destination, rel_dir) if rel_dir else self.destination
        try:
            mtime_ns = os.stat(path).st_mtime_ns
        except OSError:
            return None
        cached = self._dirs.get(rel_dir)
        if cached and cached.mtime_ns == mtime_ns and not cached.racy:
            return cached
        try:
            with os.scandir(path) as it:
                items = [(e.name, e.is_dir(follow_symlinks=False)) for e in it]
        except OSError:
            return None
        files = [name for name, is_dir in items if not is_dir]
        subdirs = sorted(
            os.path.join(rel_dir, name) if rel_dir else name
            for name, is_dir in items
            if is_dir and not name.startswith(_SKIPPED_DIR_PREFIXES)
        )
        listing = _DirListing(
            mtime_ns=mtime_ns,
            subdirs=subdirs,
            entries=_entries_in_dir(rel_dir, files, self.grouping),
            racy=now_ns - mtime_ns < _RACY_MTIME_WINDOW_NS,
            seq=next(self._scans),
        )
        self._dirs[rel_dir] = listing
        return listing

    def _refresh(self) -> None:
        """re-stats every directory and rebuilds the merged views if any changed."""
        now_ns = time.time_ns()
        seen: dict[str, _DirListing] = {}
        pending = [""]
        while pending:
            rel_dir = pending.pop()
            listing = self._listing(rel_dir, now_ns)
            if listing is None:
                continue
            seen[rel_dir] = listing
            pending.extend(listing.subdirs)
        self._dirs = seen  # drops directories that disappeared
        signature = tuple(sorted((d, listing.seq) for d, listing in seen.items()))
        if signature == self._views.signature:
            return
        entries = [e for listing in seen.values() for e in listing.entries]
        _sort_newest_first(entries)
        days: dict[str, list[RecordingEntry]] = {}
        for entry in entries:
            days.setdefault(entry.datetime.date().isoformat(), []).append(entry)
        self._views = _Views(
            signature=signature,
            entries=tuple(entries),
            by_key={(e.base_filename, e.type): e for e in reversed(entries)},
            days=tuple(days.items()),
        )

    def _current(self) -> _Views:
        with self._lock:
            self._refresh()
            return self._views

    def entries(self) -> list[RecordingEntry]:
        """returns every recording instant, newest first."""
        return list(self._current().entries)

    def days(self) -> list[tuple[str, list[RecordingEntry]]]:
        """returns (iso date, recordings newest first) pairs, newest day first."""
        return list(self._current().days)

    def find(self, base_filename: str, rtype: str) -> RecordingEntry | None:
        """returns the recording instant with this base filename and type."""
        return self._current().by_key.get((base_filename, rtype))


_indexes: dict[tuple[str, str], RecordingIndex] = {}
_indexes_lock = threading.Lock()


def recording_index(destination: str, grouping: str) -> RecordingIndex:
    """returns the shared index for destination + grouping."""
    with _indexes_lock:
        key = (destination, grouping)
        if key not in _indexes:
            _indexes.clear()  # settings changed; only the current pair is live
            _indexes[key] = RecordingIndex(destination, grouping)
        return _indexes[key]


def journey_chain(
    entries: list[RecordingEntry],
    base_filename: str,
    rtype: str,
    continuous: bool = False,
) -> list[RecordingEntry]:
    """returns the forward chain of contiguous segments from a start.

    only segments of the start's type are linked unless `continuous`: blackvue
    writes an event (E) or parking (P) segment in place of the normal one for
    that minute, so a drive is only complete across types.
    """
    candidates = sorted(
        (e for e in entries if continuous or e.type == rtype),
        key=lambda e: (e.datetime, e.type),
    )
    chain: list[RecordingEntry] = []
    for entry in candidates:
        if not chain:
            if entry.base_filename == base_filename and entry.type == rtype:
                chain.append(entry)
            continue
        gap = (entry.datetime - chain[-1].datetime).total_seconds()
        if gap <= 0:
            continue  # another type at the same instant does not end the journey
        if gap > _CONTIGUOUS_GAP.total_seconds():
            break
        chain.append(entry)
    return chain


__all__ = [
    "RecordingEntry",
    "RecordingIndex",
    "journey_chain",
    "list_recordings",
    "recording_index",
]
