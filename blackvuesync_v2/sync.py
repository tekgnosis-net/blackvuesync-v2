"""sync core: filename parsing, dashcam HTTP client, download, retention, locking."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import errno
import fcntl
import glob
import http.client
import json
import logging
import os
import re
import shutil
import socket
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

if TYPE_CHECKING:
    from typing import Union

    from blackvuesync_v2.metrics import SyncMetrics
    from blackvuesync_v2.server.progress import ProgressPublisher, _NoopPublisher

    _AnyPublisher = Union[ProgressPublisher, _NoopPublisher]

# logging format strings and accepted values
TEXT_LOG_FORMAT = "%(asctime)s: %(levelname)s %(message)s"
LOG_FORMATS = ("text", "json")
LOG_RECORD_RESERVED_FIELDS = frozenset(logging.makeLogRecord({}).__dict__) | {
    "asctime",
    "message",
}


class StructuredLogFormatter(logging.Formatter):
    """formats log records as newline-delimited JSON."""

    def format(self, record: logging.LogRecord) -> str:
        log_record: dict[str, object] = {
            "timestamp": datetime.datetime.fromtimestamp(
                record.created, datetime.timezone.utc
            )
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in LOG_RECORD_RESERVED_FIELDS and not key.startswith("_"):
                log_record[key] = value

        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)

        if record.stack_info:
            log_record["stack_info"] = self.formatStack(record.stack_info)

        return json.dumps(log_record, default=str, separators=(",", ":"))


def configure_logging(log_format: str) -> None:
    """sets up logging output format."""
    if log_format == "json":
        formatter: logging.Formatter = StructuredLogFormatter()
    elif log_format == "text":
        formatter = logging.Formatter(TEXT_LOG_FORMAT)
    else:
        raise RuntimeError(f"unknown log format : {log_format}")

    for handler in logging.root.handlers:
        handler.setFormatter(formatter)


logging.basicConfig(format=TEXT_LOG_FORMAT)

# root logger
logger = logging.getLogger()

# cron logger (remains active in cron mode)
cron_logger = logging.getLogger("cron")


def set_logging_levels(verbosity: int, is_cron_mode: bool) -> None:
    """sets up the logging levels according to the desired verbosity and operation mode"""
    if verbosity == -1:
        logger.setLevel(logging.ERROR)
        cron_logger.setLevel(logging.ERROR)
    elif verbosity == 0:
        logger.setLevel(logging.ERROR if is_cron_mode else logging.WARN)
        cron_logger.setLevel(logging.INFO if is_cron_mode else logging.WARN)
    elif verbosity == 1:
        logger.setLevel(logging.INFO)
        cron_logger.setLevel(logging.INFO)
    else:
        logger.setLevel(logging.DEBUG)
        cron_logger.setLevel(logging.DEBUG)


def flush_logs() -> None:
    """flushes all logging handlers"""
    for handler in logging.root.handlers:
        handler.flush()


# max disk usage percent
max_disk_used_percent: int | None = None  # pylint: disable=invalid-name

# socket timeout
socket_timeout: float | None = None  # pylint: disable=invalid-name

# indicator that we're doing a dry run
dry_run: bool | None = None  # pylint: disable=invalid-name

# minimum elapsed time before retrying a failed download
retry_failed_after: datetime.timedelta = datetime.timedelta(days=1)  # pylint: disable=invalid-name  # fmt: skip

# affinity key reserved for test isolation
affinity_key: str | None = None  # pylint: disable=invalid-name

# metadata types to skip downloading
skip_metadata: set[str] = set()  # pylint: disable=invalid-name

# duration regex for --keep and --retry-failed-after
duration_re = re.compile(r"""(?P<range>\d+)(?P<unit>[shdw]?)""")

# parses a Content-Range value such as "bytes 100-199/500" (total may be "*")
content_range_re = re.compile(r"bytes\s+(?P<start>\d+)-\d+/(?P<total>\d+|\*)")

# cutoff date; only recordings from this date on are downloaded and kept
cutoff_date: datetime.date | None = None  # pylint: disable=invalid-name

# errno codes for unavailable dashcam
dashcam_unavailable_errno_codes = (
    errno.EHOSTDOWN,  # host is down
    errno.EHOSTUNREACH,  # host is unreachable
    errno.ENETUNREACH,  # network is unreachable
    errno.ETIMEDOUT,  # connection timed out
)

# for unit testing
today: datetime.date = datetime.date.today()

# valid metadata type codes for --skip-metadata
VALID_METADATA_TYPES = frozenset("t3g")

# download chunk size in bytes
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

# cooperative stop flag for the active sync. set by /api/sync/stop,
# checked by the download chunk loop between reads, cleared by trigger_sync
# at the start of each new sync.
_stop_event: threading.Event = threading.Event()


def request_stop() -> None:
    """requests cooperative stop of the active sync; the download loop will
    raise UserWarning("sync stopped by user") on its next chunk-boundary check."""
    _stop_event.set()


def clear_stop() -> None:
    """clears the stop flag; called by trigger_sync before each new sync run."""
    _stop_event.clear()


def is_stop_requested() -> bool:
    """returns True if request_stop has been called and clear_stop has not
    yet reset the flag."""
    return _stop_event.is_set()


# valid recording type and direction characters
#
# reference:
# - https://support.blackvue.com.au/hc/en-us/articles/13301776266895-Video-File-Naming
# N: Normal
# E: Event
# P: Parking motion detection
# M: Manual
# I: Parking impact
# O: Overspeed
# A: Hard acceleration
# T: Hard cornering
# B: Hard braking
# R: Geofence-enter (Fleet)
# X: Geofence-exit (Fleet)
# G: Geofence-pass (Fleet)
# D: Drowsiness (DMS)
# L: Distraction (DMS)
# Y: Seatbelt not detected (DMS)
# F: Driver undetected (DMS)
#
# F: Front camera
# R: Rear camera
# I: Interior camera
# O: Optional camera
#
# L or S: upload flag, Substream or Live
#
RECORDING_TYPES = "NEPMIOATBRXGDLYF"
RECORDING_DIRECTIONS = "FRIO"


def parse_skip_metadata(value: str) -> set[str]:
    """parses and validates the --skip-metadata argument"""
    types = set(value)
    invalid = types - VALID_METADATA_TYPES
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid value '{value}': unknown metadata type(s)"
            f" '{', '.join(sorted(invalid))}' (valid: t, 3, g)"
        )
    return types


def parse_filter(value: str) -> tuple[str, ...]:
    """parses and validates a comma-separated filter of recording type/direction codes"""
    codes = [c.strip() for c in value.split(",") if c.strip()]
    if not codes:
        return ()
    for code in codes:
        if len(code) == 1:
            if code not in RECORDING_TYPES:
                raise argparse.ArgumentTypeError(
                    f"invalid filter code '{code}': unknown recording type"
                    f" (valid: {', '.join(RECORDING_TYPES)})"
                )
        elif len(code) == 2:
            if code[0] not in RECORDING_TYPES:
                raise argparse.ArgumentTypeError(
                    f"invalid filter code '{code}': unknown recording type"
                    f" '{code[0]}' (valid: {', '.join(RECORDING_TYPES)})"
                )
            if code[1] not in RECORDING_DIRECTIONS:
                raise argparse.ArgumentTypeError(
                    f"invalid filter code '{code}': unknown direction"
                    f" '{code[1]}' (valid: {', '.join(RECORDING_DIRECTIONS)})"
                )
        else:
            raise argparse.ArgumentTypeError(
                f"invalid filter code '{code}': must be 1 or 2 characters"
                " (type, or type + direction)"
            )
    return tuple(codes)


def parse_duration(
    duration: str, *, label: str = "DURATION", allowed_units: str = "shdw"
) -> datetime.timedelta:
    """parses a duration string like '30s', '12h', '1d', '2w' into a timedelta; defaults to days"""

    if (duration_match := re.fullmatch(duration_re, duration)) is None:
        raise RuntimeError(f"{label} must be in the format <number>[{allowed_units}]")

    duration_range = int(duration_match.group("range"))

    if duration_range < 1:
        raise RuntimeError(f"{label} must be greater than zero.")

    duration_unit = duration_match.group("unit") or "d"

    if duration_unit not in allowed_units:
        raise RuntimeError(
            f"{label} does not support unit '{duration_unit}';"
            f" use one of [{allowed_units}]"
        )

    if duration_unit == "s":
        return datetime.timedelta(seconds=duration_range)
    if duration_unit == "h":
        return datetime.timedelta(hours=duration_range)
    if duration_unit == "d":
        return datetime.timedelta(days=duration_range)
    if duration_unit == "w":
        return datetime.timedelta(weeks=duration_range)

    # this indicates a coding error
    raise RuntimeError(f"unknown duration unit : {duration_unit}")


def calc_cutoff_date(keep: str) -> datetime.date:
    """given a retention period, calculates the date before which files should be deleted"""
    return today - parse_duration(keep, label="KEEP", allowed_units="dw")


@dataclass(frozen=True)
class Recording:
    """represents a recording from the dashcam; the dashcam serves the list of video recording filenames (front and rear)"""

    filename: str
    base_filename: str
    group_name: str | None
    datetime: datetime.datetime
    type: str
    direction: str


# dashcam recording filename regular expression
filename_re = re.compile(
    rf"""(?P<base_filename>(?P<year>\d\d\d\d)(?P<month>\d\d)(?P<day>\d\d)
    _(?P<hour>\d\d)(?P<minute>\d\d)(?P<second>\d\d))
    _(?P<type>[{RECORDING_TYPES}])
    (?P<direction>[{RECORDING_DIRECTIONS}])
    (?P<upload>[LS]?)
    \.(?P<extension>mp4)""",
    re.VERBOSE,
)


def to_recording(filename: str, grouping: str) -> Recording | None:
    """extracts recording information from a filename"""
    if (filename_match := re.fullmatch(filename_re, filename)) is None:
        return None

    year = int(filename_match.group("year"))
    month = int(filename_match.group("month"))
    day = int(filename_match.group("day"))
    hour = int(filename_match.group("hour"))
    minute = int(filename_match.group("minute"))
    second = int(filename_match.group("second"))
    recording_datetime = datetime.datetime(year, month, day, hour, minute, second)

    recording_base_filename = filename_match.group("base_filename")
    recording_group_name = get_group_name(recording_datetime, grouping)
    recording_type = filename_match.group("type")
    recording_direction = filename_match.group("direction")

    return Recording(
        filename,
        recording_base_filename,
        recording_group_name,
        recording_datetime,
        recording_type,
        recording_direction,
    )


# artifact table: (artifact_type, skip_metadata_key, filename_builder).
# used by download_recording and prune_orphan_partials to enumerate all
# expected artifact filenames for a recording.
_ARTIFACTS: list[tuple[str, str | None, Callable[[Recording], str]]] = [
    ("mp4", None, lambda r: r.filename),
    ("thm", "t", lambda r: f"{r.base_filename}_{r.type}{r.direction}.thm"),
    ("3gf", "3", lambda r: f"{r.base_filename}_{r.type}.3gf"),
    ("gps", "g", lambda r: f"{r.base_filename}_{r.type}.gps"),
]

# maps artifact skip_metadata_key to the skip-log metadata_type and format string
_ARTIFACT_SKIP_LABEL: dict[str, tuple[str, str]] = {
    "t": ("thumbnail", "Skipping thumbnail : %s (--skip-metadata)"),
    "3": ("accelerometer", "Skipping accelerometer : %s (--skip-metadata)"),
    "g": ("gps", "Skipping gps : %s (--skip-metadata)"),
}

# pattern of a recording filename as returned in each line from the dashcam index page
file_line_re = re.compile(r"n:/Record/(?P<filename>.*\.mp4),s:1000000\r\n")


def get_filenames(file_lines: list[str]) -> list[str]:
    """extracts the recording filenames from the lines returned by the dashcam index page"""
    filenames = []
    for file_line in file_lines:
        # the first line is "v:1.00", which won't match, so we skip it
        if file_line_match := re.fullmatch(file_line_re, file_line):
            filenames.append(file_line_match.group("filename"))

    return filenames


def get_dashcam_filenames(base_url: str) -> list[str]:
    """gets the recording filenames from the dashcam"""
    try:
        url = urllib.parse.urljoin(base_url, "blackvue_vod.cgi")
        request = urllib.request.Request(url)
        if affinity_key:
            request.add_header("X-Affinity-Key", affinity_key)

        with urllib.request.urlopen(request) as response:
            response_status_code = response.getcode()
            if response_status_code != 200:
                raise RuntimeError(
                    f"Error response from : {base_url} ; status code : {response_status_code}"
                )

            charset = response.info().get_param("charset", "UTF-8")
            file_lines = [x.decode(charset) for x in response.readlines()]

        return get_filenames(file_lines)
    except urllib.error.URLError as e:
        if isinstance(e.reason, OSError) and (
            isinstance(e.reason, (TimeoutError, socket.timeout))
            or e.reason.errno in dashcam_unavailable_errno_codes
        ):
            raise UserWarning(f"Dashcam unavailable : {e}") from e

        raise RuntimeError(
            f"Cannot obtain list of recordings from dashcam at address : {base_url}; error : {e}"
        ) from e
    except socket.timeout as e:
        raise UserWarning(
            f"Timeout communicating with dashcam at address : {base_url}; error : {e}"
        ) from e
    except http.client.RemoteDisconnected as e:
        raise UserWarning(
            f"Dashcam disconnected without a response; address : {base_url}; error : {e}"
        ) from e


def get_group_name(recording_datetime: datetime.datetime, grouping: str) -> str | None:
    """determines the group name for a given recording according to the indicated grouping"""
    if grouping == "daily":
        return recording_datetime.date().isoformat()

    if grouping == "weekly":
        recording_date = recording_datetime.date()

        # day of the week (mon = 0, ..., sun = 6)
        recording_weekday = recording_date.weekday()
        recording_weekday_delta = datetime.timedelta(days=recording_weekday)
        recording_mon_date = recording_date - recording_weekday_delta
        return recording_mon_date.isoformat()

    if grouping == "monthly":
        return recording_datetime.date().strftime("%Y-%m")

    if grouping == "yearly":
        return recording_datetime.date().strftime("%Y")

    return None


# download speed units for conversion to a natural representation
speed_units = [(1000000, "Mbps"), (1000, "Kbps"), (1, "bps")]


def to_natural_speed(speed_bps: int) -> tuple[int, str]:
    """returns a natural representation of a given download speed in bps as an scalar+unit tuple (base 10)"""
    for speed_unit_multiplier, speed_unit_name in speed_units:
        if speed_bps > speed_unit_multiplier:
            return int(speed_bps / speed_unit_multiplier), speed_unit_name

    return 0, "bps"


def format_natural_speed(speed_bps: int | None) -> str:
    """formats download speed in bps as a human-readable string like ' (123Mbps)', or empty string if None"""
    if not speed_bps:
        return ""

    speed_value, speed_unit = to_natural_speed(speed_bps)
    return f" ({speed_value}{speed_unit})"


def get_filepath(destination: str, group_name: str | None, filename: str) -> str:
    """constructs a path for a recording file from the destination, group name and filename (or glob pattern)"""
    if group_name:
        return os.path.join(destination, group_name, filename)
    return os.path.join(destination, filename)


def get_failed_marker_filepath(
    destination: str, group_name: str | None, filename: str
) -> str:
    """returns the filepath for a .failed marker file"""
    return get_filepath(destination, group_name, f"{filename}.failed")


def is_download_blocked_by_failure(
    destination: str, group_name: str | None, filename: str
) -> bool:
    """returns whether a non-stale failure marker prevents retrying this download"""
    marker_filepath = get_failed_marker_filepath(destination, group_name, filename)

    try:
        with open(marker_filepath, encoding="utf-8") as f:
            timestamp_str = f.read().strip()

        failure_time = datetime.datetime.fromisoformat(timestamp_str)
        elapsed = datetime.datetime.now() - failure_time

        return elapsed < retry_failed_after
    except FileNotFoundError:
        return False
    except ValueError:
        logger.debug(
            "Invalid timestamp in failure marker : %s; allowing retry", filename
        )
        return False
    except OSError as e:
        cron_logger.warning(
            "Could not read failure marker : %s; error : %s; allowing retry",
            filename,
            e,
        )
        return False


def mark_download_failed(
    destination: str, group_name: str | None, filename: str
) -> None:
    """creates or updates a .failed marker file with the current timestamp"""
    marker_filepath = get_failed_marker_filepath(destination, group_name, filename)

    try:
        with open(marker_filepath, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now().isoformat())
    except OSError as e:
        cron_logger.warning(
            "Could not create failure marker : %s; error : %s", filename, e
        )


def remove_download_failed_marker(
    destination: str, group_name: str | None, filename: str
) -> None:
    """removes a .failed marker file if it exists"""
    marker_filepath = get_failed_marker_filepath(destination, group_name, filename)

    try:
        os.remove(marker_filepath)
    except FileNotFoundError:
        pass
    except OSError as e:
        cron_logger.warning(
            "Could not remove failure marker : %s; error : %s", filename, e
        )


def _resume_offset(temp_filepath: str) -> int:
    """returns the byte size of an existing partial download, or 0 if absent."""
    try:
        return os.path.getsize(temp_filepath)
    except OSError:
        return 0


def _build_record_request(url: str, resume_from: int) -> urllib.request.Request:
    """builds the record GET, adding the affinity key and a Range header when resuming."""
    request = urllib.request.Request(url)
    if affinity_key:
        request.add_header("X-Affinity-Key", affinity_key)
    if resume_from > 0:
        request.add_header("Range", f"bytes={resume_from}-")
    return request


def _resume_decision(status: int, headers: object, resume_from: int) -> tuple[str, int]:
    """determines how to handle the server response for a download attempt.

    returns (action, total_bytes) where action is one of:
      "resume"  -- confirmed 206 resume; use "ab" and start count from resume_from.
      "restart" -- unconfirmed 206; discard partial and retry from byte 0.
      "fresh"   -- 200 or any non-resume case; use "wb" and start count from 0.
    """
    content_length = int(headers.get("Content-Length") or 0)  # type: ignore[attr-defined]
    if status == 206 and resume_from > 0:
        m = content_range_re.fullmatch(
            (headers.get("Content-Range") or "").strip()  # type: ignore[attr-defined]
        )
        if m is not None and int(m.group("start")) == resume_from:
            total = m.group("total")
            total_bytes = int(total) if total != "*" else resume_from + content_length
            return "resume", total_bytes
        # 206 but content-range is missing, malformed, or starts at the wrong
        # offset -- writing this partial body as a complete file would corrupt it.
        return "restart", 0
    return "fresh", content_length


def _stream_response(
    response: object,
    temp_filepath: str,
    *,
    append: bool,
    total_bytes: int,
    on_chunk: Callable[[int, int], None] | None,
) -> int:
    """writes the body to the temp file; returns bytes transferred this run."""
    mode = "ab" if append else "wb"
    downloaded = 0
    with open(temp_filepath, mode) as f:
        while True:
            chunk = response.read(DOWNLOAD_CHUNK_SIZE)  # type: ignore[attr-defined]
            if not chunk:
                break
            # cooperative stop check between read and write; the exception
            # propagates to classify_run_failure and the job ends as failed.
            if is_stop_requested():
                raise UserWarning("sync stopped by user")
            f.write(chunk)
            downloaded += len(chunk)
            if on_chunk is not None:
                on_chunk(downloaded, total_bytes)
    return downloaded


class TruncatedDownloadError(Exception):
    """describes a download that ended before the expected number of bytes arrived."""


def _log_download_success(
    filename: str,
    destination_filepath: str,
    transferred: int,
    elapsed_s: float,
    speed_bps: int | None,
) -> None:
    """emits the structured debug log for a successfully downloaded file."""
    speed_str = format_natural_speed(speed_bps)
    logger.debug(
        "Downloaded file : %s%s",
        filename,
        speed_str,
        extra={
            "event": "file_downloaded",
            "recording_filename": filename,
            "destination_path": destination_filepath,
            "content_length_bytes": transferred,
            "elapsed_seconds": elapsed_s,
            "speed_bps": speed_bps,
        },
    )


def _log_download_failure(filename: str, error: object, *, marker: bool) -> None:
    """emits the shared structured warning for a failed file download."""
    cron_logger.warning(
        "Could not download file : %s; error : %s; ignoring.",
        filename,
        error,
        extra={
            "event": "file_download_failed",
            "recording_filename": filename,
            "error_type": type(error).__name__,
            "error": str(error),
            "failure_marker_created": marker,
        },
    )


def _precheck_download(
    destination_filepath: str,
    destination: str,
    group_name: str | None,
    filename: str,
) -> tuple[bool, int | None] | None:
    """checks guard conditions before a download attempt; returns a result tuple to
    short-circuit, or None when the download should proceed."""
    if os.path.exists(destination_filepath):
        logger.debug(
            "Ignoring already downloaded file : %s",
            filename,
            extra={
                "event": "file_already_downloaded",
                "recording_filename": filename,
                "destination_path": destination_filepath,
            },
        )
        return False, None

    if dry_run:
        logger.debug(
            "DRY RUN Would download file : %s",
            filename,
            extra={"event": "file_download_dry_run", "recording_filename": filename},
        )
        return True, None

    # skips downloads with a recent failure marker to avoid repeated retries
    if is_download_blocked_by_failure(destination, group_name, filename):
        logger.debug(
            "Skipping recently failed download : %s",
            filename,
            extra={
                "event": "file_download_recently_failed",
                "recording_filename": filename,
            },
        )
        return False, None

    return None


def _transfer_to_temp(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    base_url: str,
    filename: str,
    temp_filepath: str,
    resume_from: int,
    on_chunk: Callable[[int, int], None] | None,
) -> tuple[bool, int, float, int]:
    """opens the dashcam HTTP connection, streams the body to the temp file, and
    returns (needs_restart, transferred, elapsed_s, expected_size), where
    expected_size is the full file size or 0 when unknown. raises HTTPError/
    URLError/socket.timeout on network failures; does not rename or log success."""
    if resume_from:
        logger.debug(
            "Found incomplete download : %s",
            temp_filepath,
            extra={
                "event": "incomplete_download_found",
                "recording_filename": filename,
                "temp_path": temp_filepath,
                "resume_from_bytes": resume_from,
            },
        )

    url = urllib.parse.urljoin(base_url, f"Record/{filename}")
    start = time.perf_counter()
    needs_restart = False
    transferred = 0
    total_bytes = 0
    try:
        request = _build_record_request(url, resume_from)
        with urllib.request.urlopen(request) as response:
            action, total_bytes = _resume_decision(
                response.getcode(), response.info(), resume_from
            )
            if action == "restart":
                # unconfirmed 206: partial body cannot be safely written as
                # a complete file; discard partial and retry from byte 0.
                needs_restart = True
            else:
                transferred = _stream_response(
                    response,
                    temp_filepath,
                    append=(action == "resume"),
                    total_bytes=total_bytes,
                    on_chunk=on_chunk,
                )
    finally:
        elapsed_s = time.perf_counter() - start

    return needs_restart, transferred, elapsed_s, total_bytes


def _handle_http_error(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    e: urllib.error.HTTPError,
    base_url: str,
    filename: str,
    destination: str,
    group_name: str | None,
    temp_filepath: str,
    resume_from: int,
    metrics: SyncMetrics | None,
    on_chunk: Callable[[int, int], None] | None,
    on_start: Callable[[], None] | None = None,
) -> tuple[bool, int | None]:
    """handles an HTTPError raised during a download attempt; either restarts
    the download (416 with a partial present) or records the failure and returns."""
    if e.code == 416 and resume_from > 0:
        # the local partial is larger than the source (corrupt); discards and
        # restarts once from byte 0. after removal _resume_offset returns 0,
        # so the retry sends no Range header and cannot loop. a 416 on a
        # no-Range request (resume_from == 0) falls through to generic
        # http-error handling rather than retrying.
        with contextlib.suppress(OSError):
            os.remove(temp_filepath)
        return download_file(
            base_url, filename, destination, group_name, metrics, on_chunk, on_start
        )
    # other HTTP errors (e.g. 500 for corrupted recordings); marks as failed
    if metrics:
        metrics.record_file_download_failure("http")
    _log_download_failure(filename, e, marker=True)
    mark_download_failed(destination, group_name, filename)
    return False, None


def download_file(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    base_url: str,
    filename: str,
    destination: str,
    group_name: str | None,
    metrics: SyncMetrics | None = None,
    on_chunk: Callable[[int, int], None] | None = None,
    on_start: Callable[[], None] | None = None,
) -> tuple[bool, int | None]:
    """downloads a file from the dashcam to the destination directory; returns whether data was transferred.

    on_start is called once the guard checks pass and a transfer is attempted,
    so callers can tell skipped files from attempted ones."""
    # if we have a group name, we may not have ensured it exists yet
    if group_name:
        ensure_destination(os.path.join(destination, group_name))

    destination_filepath = get_filepath(destination, group_name, filename)

    precheck = _precheck_download(
        destination_filepath, destination, group_name, filename
    )
    if precheck is not None:
        return precheck

    if on_start is not None:
        on_start()

    # clears any prior failure marker now that we've decided to retry
    remove_download_failed_marker(destination, group_name, filename)

    temp_filepath = os.path.join(destination, f".{filename}")
    resume_from = _resume_offset(temp_filepath)

    try:
        needs_restart, transferred, elapsed_s, expected_size = _transfer_to_temp(
            base_url, filename, temp_filepath, resume_from, on_chunk
        )
    except urllib.error.HTTPError as e:
        return _handle_http_error(
            e,
            base_url,
            filename,
            destination,
            group_name,
            temp_filepath,
            resume_from,
            metrics,
            on_chunk,
            on_start,
        )
    except urllib.error.URLError as e:
        # network-level errors (connection reset, etc.); does not mark as failed
        if metrics:
            metrics.record_file_download_failure("network")
        _log_download_failure(filename, e, marker=False)
        return False, None
    except socket.timeout as e:
        if metrics:
            metrics.record_file_download_failure("timeout")
        raise UserWarning(
            f"Timeout communicating with dashcam at address : {base_url}; error : {e}"
        ) from e

    if needs_restart:
        with contextlib.suppress(OSError):
            os.remove(temp_filepath)
        return download_file(
            base_url, filename, destination, group_name, metrics, on_chunk, on_start
        )

    # read() returns b"" on a premature close instead of raising, so a short
    # body is detected here; the partial is kept so the next run resumes it.
    if (
        expected_size
        and (actual_size := _resume_offset(temp_filepath)) != expected_size
    ):
        if metrics:
            metrics.record_file_download_failure("network")
        _log_download_failure(
            filename,
            TruncatedDownloadError(
                f"connection closed after {actual_size} of {expected_size} bytes"
            ),
            marker=False,
        )
        return False, None

    os.rename(temp_filepath, destination_filepath)
    speed_bps = int(10.0 * transferred / elapsed_s) if transferred else None
    _log_download_success(
        filename, destination_filepath, transferred, elapsed_s, speed_bps
    )
    if metrics:
        metrics.record_file_download(transferred)
    return True, speed_bps


def download_recording(  # pylint: disable=too-many-locals
    base_url: str,
    recording: Recording,
    destination: str,
    metrics: SyncMetrics | None = None,
    publisher: _AnyPublisher | None = None,
) -> None:
    """downloads the set of recordings, including gps data, for the given filename from the dashcam to the destination
    directory"""
    # first checks that we have enough room left
    disk_usage = shutil.disk_usage(destination)
    if metrics:
        metrics.record_destination_disk_usage(disk_usage.used, disk_usage.total)
    disk_used_percent = disk_usage.used / disk_usage.total * 100.0

    if max_disk_used_percent is not None and disk_used_percent > max_disk_used_percent:
        if metrics:
            metrics.record_file_download_failure("disk")
        raise RuntimeError(
            f"Not enough disk space left. Max used disk space percentage allowed : {max_disk_used_percent}%"
        )

    # whether any file of a recording (video, thumbnail, gps, accel.) was downloaded
    any_downloaded = False

    def _dl(fn: str, artifact_type: str) -> tuple[bool, int | None]:
        """wraps download_file with publisher notifications; files that need
        no transfer (already present, recently failed) are reported as skipped."""
        if publisher is None:
            return download_file(
                base_url, fn, destination, recording.group_name, metrics
            )

        started = False

        def _start() -> None:
            nonlocal started
            started = True
            publisher.start_file(
                fn,
                artifact_type,  # type: ignore[arg-type]
                0,
                direction=cast(Literal["F", "R", "I", "O"], recording.direction),
            )

        try:
            result = download_file(
                base_url,
                fn,
                destination,
                recording.group_name,
                metrics,
                on_chunk=publisher.update_bytes,
                on_start=_start,
            )
        except Exception as exc:
            if not started:
                _start()
            publisher.finish_file(success=False, reason=type(exc).__name__)
            raise
        if started:
            publisher.finish_file(success=result[0])
        elif result[0]:
            # dry run: reported as a completed file without a transfer
            _start()
            publisher.finish_file(success=True)
        else:
            publisher.skip_file()
        return result

    speed_bps: int | None = None
    for artifact_type, skip_key, build_filename in _ARTIFACTS:
        if skip_key is not None and skip_key in skip_metadata:
            metadata_type, fmt = _ARTIFACT_SKIP_LABEL[skip_key]
            logger.debug(
                fmt,
                recording.base_filename,
                extra={
                    "event": "metadata_skipped",
                    "metadata_type": metadata_type,
                    "recording_base_filename": recording.base_filename,
                },
            )
            continue
        fn = build_filename(recording)
        downloaded, artifact_speed = _dl(fn, artifact_type)
        any_downloaded |= downloaded
        if artifact_type == "mp4":
            speed_bps = artifact_speed

    # logs if any part of a recording was downloaded (or would have been)
    if any_downloaded:
        # recording logger, depends on type of recording
        recording_logger = cron_logger if recording.type in ("N", "M") else logger

        if not dry_run:
            speed_str = format_natural_speed(speed_bps)
            recording_logger.info(
                "Downloaded recording : %s (%s%s)%s",
                recording.base_filename,
                recording.type,
                recording.direction,
                speed_str,
                extra={
                    "event": "recording_downloaded",
                    "recording_base_filename": recording.base_filename,
                    "recording_type": recording.type,
                    "recording_direction": recording.direction,
                    "recording_group_name": recording.group_name,
                    "speed_bps": speed_bps,
                },
            )
        else:
            recording_logger.info(
                "DRY RUN Would download recording : %s (%s%s)",
                recording.base_filename,
                recording.type,
                recording.direction,
                extra={
                    "event": "recording_download_dry_run",
                    "recording_base_filename": recording.base_filename,
                    "recording_type": recording.type,
                    "recording_direction": recording.direction,
                    "recording_group_name": recording.group_name,
                },
            )


def sort_recordings(recordings: list[Recording], recording_priority: str) -> None:
    """sorts recordings in place according to the given priority"""

    # preferred orderings (by type and direction)
    recording_types = "MEIBOATRXGNP"
    recording_directions = "FRIO"

    # tomorrow, for reverse datetime sorting
    tomorrow = datetime.datetime.now() + datetime.timedelta(days=1)

    def datetime_sort_key(recording: Recording) -> tuple[datetime.datetime, int]:
        """sorts by datetime, then front/rear direction, then recording type"""
        return recording.datetime, recording_directions.find(recording.direction)

    def rev_datetime_sort_key(recording: Recording) -> tuple[datetime.timedelta, int]:
        """sorts by newest to oldest datetime, then front/rear/interior direction"""
        return tomorrow - recording.datetime, recording_directions.find(
            recording.direction
        )

    def manual_event_sort_key(
        recording: Recording,
    ) -> tuple[int, datetime.datetime, int]:
        """sorts by recording type (manual and events first), then datetime, then front/rear/interior direction"""
        return (
            recording_types.find(recording.type),
            recording.datetime,
            recording_directions.find(recording.direction),
        )

    sort_key: Callable[[Recording], tuple[object, ...]]
    if recording_priority == "date":
        # least recent first
        sort_key = datetime_sort_key
    elif recording_priority == "rdate":
        # most recent first
        sort_key = rev_datetime_sort_key
    elif recording_priority == "type":
        # manual, event, normal, parking
        sort_key = manual_event_sort_key
    else:
        # this indicates a coding error
        raise RuntimeError(f"unknown recording priority : {recording_priority}")

    recordings.sort(key=sort_key)


# group name globs, keyed by grouping
group_name_globs = {
    "none": None,
    "daily": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "weekly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]",
    "monthly": "[0-9][0-9][0-9][0-9]-[0-9][0-9]",
    "yearly": "[0-9][0-9][0-9][0-9]",
}


@dataclass(frozen=True)
class DownloadedRecording:
    """represents a recording downloaded to the destination; matches all files (video front/rear, gps, etc.)"""

    base_filename: str
    group_name: str | None
    datetime: datetime.datetime


# downloaded recording filename regular expression
downloaded_filename_re = re.compile(
    r"""^(?P<base_filename>(?P<year>\d\d\d\d)(?P<month>\d\d)(?P<day>\d\d)
    _(?P<hour>\d\d)(?P<minute>\d\d)(?P<second>\d\d))_""",
    re.VERBOSE,
)


def to_downloaded_recording(filename: str, grouping: str) -> DownloadedRecording | None:
    """extracts destination recording information from a filename"""
    if (filename_match := re.match(downloaded_filename_re, filename)) is None:
        return None

    year = int(filename_match.group("year"))
    month = int(filename_match.group("month"))
    day = int(filename_match.group("day"))
    hour = int(filename_match.group("hour"))
    minute = int(filename_match.group("minute"))
    second = int(filename_match.group("second"))
    recording_datetime = datetime.datetime(year, month, day, hour, minute, second)

    recording_base_filename = filename_match.group("base_filename")
    recording_group_name = get_group_name(recording_datetime, grouping)

    return DownloadedRecording(
        recording_base_filename, recording_group_name, recording_datetime
    )


# downloaded recording filename glob pattern
DOWNLOADED_FILENAME_GLOB = (
    "[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]_*.*"
)


def get_downloaded_recordings(
    destination: str, grouping: str
) -> set[DownloadedRecording]:
    """reads files from the destination directory and returns them as recording records"""
    group_name_glob = group_name_globs[grouping]

    downloaded_filepath_glob = get_filepath(
        destination, group_name_glob, DOWNLOADED_FILENAME_GLOB
    )

    downloaded_filepaths = glob.glob(downloaded_filepath_glob)

    return {
        r
        for p in downloaded_filepaths
        if (r := to_downloaded_recording(os.path.basename(p), grouping)) is not None
    }


def get_outdated_recordings(
    destination: str, grouping: str
) -> list[DownloadedRecording]:
    """returns the recordings prior to the cutoff date"""
    if cutoff_date is None:
        return []

    downloaded_recordings = get_downloaded_recordings(destination, grouping)

    return [x for x in downloaded_recordings if x.datetime.date() < cutoff_date]


def get_current_recordings(recordings: list[Recording]) -> list[Recording]:
    """returns the recordings that are after or on the cutoff date"""
    return (
        recordings
        if cutoff_date is None
        else [x for x in recordings if x.datetime.date() >= cutoff_date]
    )


def _matches_filter(recording: Recording, code: str) -> bool:
    """checks if a recording matches a single filter code"""
    if len(code) == 1:
        return recording.type == code
    return f"{recording.type}{recording.direction}" == code


def apply_recording_filters(
    recordings: list[Recording],
    include: tuple[str, ...] | None,
    exclude: tuple[str, ...] | None,
) -> list[Recording]:
    """returns recordings filtered by include/exclude codes"""
    result = recordings
    if include is not None and include:
        result = [r for r in result if any(_matches_filter(r, c) for c in include)]
    if exclude is not None and exclude:
        result = [r for r in result if not any(_matches_filter(r, c) for c in exclude)]
    return result


def ensure_destination(destination: str) -> None:
    """ensures the destination directory exists, creates if not, verifies it's writeable"""
    # if no destination, creates it
    if not os.path.exists(destination):
        os.makedirs(destination)
        return

    # destination exists, tests if directory
    if not os.path.isdir(destination):
        raise RuntimeError(f"download destination is not a directory : {destination}")

    # destination is a directory, tests if writable
    if not os.access(destination, os.W_OK):
        raise RuntimeError(
            f"download destination directory not writable : {destination}"
        )


def prepare_destination(destination: str, grouping: str) -> None:
    """prepares the destination, ensuring it's valid and removing excess recordings"""
    # optionally removes outdated recordings
    if cutoff_date:
        outdated_recordings = get_outdated_recordings(destination, grouping)

        for outdated_recording in outdated_recordings:
            if dry_run:
                logger.info(
                    "DRY RUN Would remove outdated recording : %s",
                    outdated_recording.base_filename,
                    extra={
                        "event": "outdated_recording_remove_dry_run",
                        "recording_base_filename": outdated_recording.base_filename,
                        "recording_group_name": outdated_recording.group_name,
                    },
                )
                continue

            logger.info(
                "Removing outdated recording : %s",
                outdated_recording.base_filename,
                extra={
                    "event": "outdated_recording_removed",
                    "recording_base_filename": outdated_recording.base_filename,
                    "recording_group_name": outdated_recording.group_name,
                },
            )

            outdated_recording_glob = (
                f"{outdated_recording.base_filename}_[NEPMIOATBRXGDLYF]*.*"
            )
            outdated_filepath_glob = get_filepath(
                destination, outdated_recording.group_name, outdated_recording_glob
            )

            outdated_filepaths = glob.glob(outdated_filepath_glob)

            for outdated_filepath in outdated_filepaths:
                os.remove(outdated_filepath)


def sync(  # pylint: disable=too-many-arguments,too-many-positional-arguments
    address: str,
    destination: str,
    grouping: str,
    download_priority: str,
    include: tuple[str, ...] | None,
    exclude: tuple[str, ...] | None,
    metrics: SyncMetrics | None = None,
    publisher: _AnyPublisher | None = None,
    job_id: str | None = None,
) -> None:
    """synchronizes the recordings at the dashcam address with the destination directory"""
    # begins the job before listing so early failures (e.g. dashcam
    # unreachable) surface as a failed job; the total is set once known.
    if publisher is not None:
        publisher.begin_job(0, job_id=job_id)

    sync_success = False
    try:
        _sync_recordings(
            address,
            destination,
            grouping,
            download_priority,
            include,
            exclude,
            metrics,
            publisher,
        )
        sync_success = True
    finally:
        if publisher is not None:
            publisher.end_job(sync_success)


def _sync_recordings(  # pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals
    address: str,
    destination: str,
    grouping: str,
    download_priority: str,
    include: tuple[str, ...] | None,
    exclude: tuple[str, ...] | None,
    metrics: SyncMetrics | None,
    publisher: _AnyPublisher | None,
) -> None:
    """lists, filters, and downloads recordings; the body of sync()."""
    prepare_destination(destination, grouping)

    # BlackVue dashcam firmware exposes only HTTP on the LAN web server;
    # HTTPS is not supported at the device. Deployment context is a trusted
    # LAN.
    base_url = f"http://{address}"
    dashcam_filenames = get_dashcam_filenames(base_url)
    dashcam_recordings = [
        r for x in dashcam_filenames if (r := to_recording(x, grouping)) is not None
    ]
    if metrics:
        metrics.dashcam_recordings_seen = len(dashcam_recordings)

    # figures out which recordings are current and should be downloaded
    current_dashcam_recordings = get_current_recordings(dashcam_recordings)

    # filters recordings according to include/exclude options
    current_dashcam_recordings = apply_recording_filters(
        current_dashcam_recordings, include, exclude
    )
    if metrics:
        metrics.recordings_selected = len(current_dashcam_recordings)

    # sorts the dashcam recordings so we download them according to some priority
    sort_recordings(current_dashcam_recordings, download_priority)

    # prunes partials for recordings no longer downloadable this run; preserves
    # partials for recordings that are still current so they can be resumed.
    expected_filenames = {
        build_filename(r)
        for r in current_dashcam_recordings
        for (_artifact_type, _skip_key, build_filename) in _ARTIFACTS
    }
    prune_orphan_partials(destination, expected_filenames)

    if publisher is not None:
        artifacts_per_recording = sum(
            1 for _, skip_key, _ in _ARTIFACTS if skip_key not in skip_metadata
        )
        publisher.set_total(len(current_dashcam_recordings) * artifacts_per_recording)

    for recording in current_dashcam_recordings:
        download_recording(base_url, recording, destination, metrics, publisher)


def is_empty_directory(dirpath: str) -> bool:
    """tests if a directory is empty, ignoring anything that's not a video recording"""
    return all(not x.endswith(".mp4") for x in os.listdir(dirpath))


# temp filename regular expression
TEMP_FILENAME_GLOB = ".[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]_[0-9][0-9][0-9][0-9][0-9][0-9]_[NEPMIOATBRXGDLYF]*.*"


def clean_destination(destination: str, grouping: str) -> None:
    """removes temporary artifacts from the destination directory"""
    # removes empty grouping directories; ignores dotfiles such as .DS_Store
    group_name_glob = group_name_globs[grouping]
    if group_name_glob:
        group_filepath_glob = os.path.join(destination, group_name_glob)

        group_filepaths = glob.glob(group_filepath_glob)

        for group_filepath in group_filepaths:
            if is_empty_directory(group_filepath):
                if not dry_run:
                    logger.debug("Removing grouping directory : %s", group_filepath)
                    shutil.rmtree(group_filepath)
                else:
                    logger.debug(
                        "DRY RUN Would remove grouping directory : %s", group_filepath
                    )


def prune_orphan_partials(destination: str, expected_filenames: set[str]) -> None:
    """removes partial dotfiles whose recording is no longer downloadable this run.

    a partial is kept when its filename is in expected_filenames (it will be
    resumed); otherwise it is an orphan (rolled off the dashcam, out of the
    retention window, or now filtered out) and is removed."""
    temp_glob = os.path.join(destination, TEMP_FILENAME_GLOB)
    for temp_filepath in glob.glob(temp_glob):
        filename = os.path.basename(temp_filepath)[1:]  # strips leading dot
        if filename in expected_filenames:
            continue
        if dry_run:
            logger.debug("DRY RUN Would remove orphan partial : %s", temp_filepath)
            continue
        logger.debug("Removing orphan partial : %s", temp_filepath)
        os.remove(temp_filepath)


def lock(destination: str) -> int:
    """creates a lock to ensure only one instance is running on a given destination; adapted from:
    https://stackoverflow.com/questions/220525/ensure-a-single-instance-of-an-application-in-linux
    """
    # establish lock file settings
    lf_path = os.path.join(destination, ".blackvuesync.lock")
    lf_flags = os.O_WRONLY | os.O_CREAT
    lf_mode = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH  # this is 0o222, i.e. 146

    # creates the lock file with mode 0o222 (world-writable, no read) so that
    # cooperative fcntl.lockf() works across multiple user contexts on
    # bare-metal deployments. the umask is cleared so the requested mode is
    # not masked away (see https://stackoverflow.com/a/15015748/832230).
    # phase g may tighten this to 0o600 once the deployment model is
    # finalized; phase a preserves upstream behavior.
    umask_original = os.umask(0)

    try:
        lf_fd = os.open(lf_path, lf_flags, lf_mode)
    finally:
        os.umask(umask_original)

    try:
        fcntl.lockf(lf_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

        return lf_fd
    except OSError as e:
        with contextlib.suppress(OSError):
            os.close(lf_fd)

        if e.errno in (errno.EAGAIN, errno.EACCES):
            raise UserWarning(
                f"Another instance is already running for destination : {destination}"
            ) from e

        raise RuntimeError(
            f"Could not acquire lock on destination : {destination}"
        ) from e


def unlock(lf_fd: int) -> None:
    """unlocks and closes the lock file; does not remove because another process may lock it in the meantime"""
    try:
        fcntl.lockf(lf_fd, fcntl.LOCK_UN)
    finally:
        os.close(lf_fd)
