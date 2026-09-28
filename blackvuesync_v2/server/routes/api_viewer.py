"""api routes for the dashcam viewer: days, recordings, journey, gps, gsensor."""

from __future__ import annotations

import dataclasses
import datetime
import json
from pathlib import Path

from flask import Blueprint, Response, abort, current_app, request

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.server.gps import parse_gps
from blackvuesync_v2.server.gsensor import parse_gsensor
from blackvuesync_v2.server.viewer_index import (
    RecordingEntry,
    RecordingIndex,
    journey_chain,
    recording_index,
)
from blackvuesync_v2.settings import Settings, SettingsStore

api_viewer_bp = Blueprint("api_viewer_bp", __name__, url_prefix="/api/viewer")

_MIME_JSON = "application/json"


def _settings() -> Settings:
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    return store.get()


def _media_url(rel_dir: str, filename: str) -> str:
    """builds the /media URL for a file, respecting the grouping subdir."""
    rel = f"{rel_dir}/{filename}" if rel_dir else filename
    return f"/media/{rel}"


def _thumb_url(entry: RecordingEntry) -> str | None:
    """returns the thumbnail URL of a direction that has one, preferring front."""
    thumbs = dict(entry.thumb_files)
    if not thumbs:
        return None
    direction = "F" if "F" in thumbs else next(iter(thumbs))
    return _media_url(entry.rel_dir, thumbs[direction])


def _segment_dict(entry: RecordingEntry) -> dict[str, object]:
    """serializes one recording instant for the API."""
    return {
        "base_filename": entry.base_filename,
        "type": entry.type,
        "datetime": entry.datetime.isoformat(),
        "directions": list(entry.directions),
        "has_gps": entry.has_gps,
        "has_3gf": entry.has_3gf,
        "has_thm": entry.has_thm,
        "videos": {d: _media_url(entry.rel_dir, f) for d, f in entry.video_files},
        "thumb": _thumb_url(entry),
    }


def _index() -> RecordingIndex:
    settings = _settings()
    return recording_index(settings.system.destination, settings.sync.grouping)


def _find(key: str) -> RecordingEntry | None:
    """resolves a `<base>_<type>` key (e.g. 20260607_101500_N) to an entry."""
    base, _, rtype = key.rpartition("_")
    return _index().find(base, rtype)


def _json(payload: object, status: int = 200) -> Response:
    return Response(json.dumps(payload), status=status, mimetype=_MIME_JSON)


def _sidecar_path(entry: RecordingEntry, suffix: str) -> Path:
    """builds the sidecar path for a recording instant.

    callers must have verified the matching `has_*` flag first; this does not
    check existence (an absent file surfaces as OSError on the subsequent read).
    """
    base = Path(_settings().system.destination)
    rel = f"{entry.base_filename}_{entry.type}{suffix}"
    return base / entry.rel_dir / rel if entry.rel_dir else base / rel


@api_viewer_bp.route("/days", methods=["GET"])
@login_required
def days() -> Response:
    """returns the calendar days that have recordings, newest first, with counts."""
    return _json(
        {"days": [{"date": d, "count": len(recs)} for d, recs in _index().days()]}
    )


@api_viewer_bp.route("/recordings", methods=["GET"])
@login_required
def recordings() -> Response:
    """returns one day's recordings (?date=YYYY-MM-DD, default newest day).

    the listing is day-scoped so a library of tens of thousands of recordings
    never reaches the browser in one response.
    """
    all_days = _index().days()
    date = request.args.get("date")
    if date is None:
        selected = all_days[:1]
    else:
        try:
            iso = datetime.date.fromisoformat(date).isoformat()
        except ValueError:
            return _json(
                {
                    "error": "date must be YYYY-MM-DD",
                    "code": "INVALID_DATE",
                    "details": {"date": date},
                },
                status=422,
            )
        selected = [(d, recs) for d, recs in all_days if d == iso]
    return _json(
        {
            "days": [
                {"date": d, "recordings": [_segment_dict(e) for e in recs]}
                for d, recs in selected
            ]
        }
    )


@api_viewer_bp.route("/recordings/<key>/journey", methods=["GET"])
@login_required
def journey(key: str) -> Response:
    """returns the forward chain of contiguous same-type segments from <key>."""
    start = _find(key)
    if start is None:
        abort(404)
    chain = journey_chain(_index().entries(), start.base_filename, start.type)
    body = json.dumps({"segments": [_segment_dict(e) for e in chain]})
    return Response(body, status=200, mimetype=_MIME_JSON)


@api_viewer_bp.route("/recordings/<key>/gps", methods=["GET"])
@login_required
def gps(key: str) -> Response:
    """returns the parsed GPS track for one recording instant."""
    entry = _find(key)
    if entry is None or not entry.has_gps:
        abort(404)
    # file may have vanished since enumeration; an OSError (-> 500) is an acceptable, honest failure
    text = _sidecar_path(entry, ".gps").read_text(encoding="utf-8", errors="replace")
    points = [dataclasses.asdict(p) for p in parse_gps(text)]
    return Response(json.dumps({"points": points}), status=200, mimetype=_MIME_JSON)


@api_viewer_bp.route("/recordings/<key>/gsensor", methods=["GET"])
@login_required
def gsensor(key: str) -> Response:
    """returns the parsed G-sensor samples for one recording instant."""
    entry = _find(key)
    if entry is None or not entry.has_3gf:
        abort(404)
    # file may have vanished since enumeration; an OSError (-> 500) is an acceptable, honest failure
    data = _sidecar_path(entry, ".3gf").read_bytes()
    samples = [dataclasses.asdict(s) for s in parse_gsensor(data)]
    return Response(json.dumps({"samples": samples}), status=200, mimetype=_MIME_JSON)


__all__ = ["api_viewer_bp"]
