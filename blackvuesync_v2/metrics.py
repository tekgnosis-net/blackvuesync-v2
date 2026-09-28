"""metrics collection and emission for blackvuesync-v2 runs."""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

METRICS_DEFAULT_JOB = "blackvuesync"
METRICS_DEFAULT_STATE_FILENAME = ".blackvuesync.metrics-state.json"
METRIC_FAILURE_REASONS = ("http", "network", "timeout", "disk", "unknown")

# cron logger (remains active in cron mode)
cron_logger = logging.getLogger("cron")


def parse_pushgateway_url(value: str) -> str:
    """parses and validates a Pushgateway URL."""
    parsed_url = urllib.parse.urlparse(value)
    if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
        raise argparse.ArgumentTypeError(
            "metrics pushgateway URL must be an http or https URL"
        )
    return value.rstrip("/")


def default_metrics_state_file(destination: str) -> str:
    """returns the default metrics state file path for a destination."""
    return os.path.join(destination, METRICS_DEFAULT_STATE_FILENAME)


def metrics_enabled(args: argparse.Namespace) -> bool:
    """returns whether any metrics sink has been configured."""
    return bool(args.metrics_file or args.metrics_pushgateway_url)


@dataclass
class SyncMetrics:  # pylint: disable=too-many-instance-attributes
    """tracks metrics for a single sync run."""

    run_start_monotonic: float
    run_start_timestamp: float
    dry_run: bool = False
    metrics_job: str = METRICS_DEFAULT_JOB
    metrics_instance: str = ""
    last_successful_file_pull_timestamp_seconds: float | None = None
    last_run_timestamp_seconds: float | None = None
    last_run_success: int = 0
    last_run_exit_code: int | None = None
    last_run_failures: dict[str, int] | None = None
    run_duration_seconds: float = 0.0
    dashcam_recordings_seen: int = 0
    recordings_selected: int = 0
    files_downloaded_last_run: int = 0
    bytes_downloaded_last_run: int = 0
    destination_disk_used_ratio: float | None = None
    failed_marker_files: int = 0
    file_download_failures_last_run: dict[str, int] | None = None

    def __post_init__(self) -> None:
        """initializes mutable metric fields."""
        if self.file_download_failures_last_run is None:
            self.file_download_failures_last_run = dict.fromkeys(
                METRIC_FAILURE_REASONS, 0
            )
        if self.last_run_failures is None:
            self.last_run_failures = dict.fromkeys(METRIC_FAILURE_REASONS, 0)

    def record_file_download(self, content_length_bytes: int | None) -> None:
        """records a successful file download."""
        self.files_downloaded_last_run += 1
        if content_length_bytes is not None:
            self.bytes_downloaded_last_run += content_length_bytes
        if not self.dry_run:
            self.last_successful_file_pull_timestamp_seconds = time.time()

    def record_file_download_failure(self, reason: str) -> None:
        """records a file download failure."""
        if self.file_download_failures_last_run is None:
            self.file_download_failures_last_run = {}
        if reason not in self.file_download_failures_last_run:
            reason = "unknown"
        self.file_download_failures_last_run[reason] += 1

    def record_run_failure(self, reason: str) -> None:
        """records a run-level failure reason."""
        if self.last_run_failures is None:
            self.last_run_failures = {}
        if reason not in self.last_run_failures:
            reason = "unknown"
        self.last_run_failures[reason] = 1

    def record_destination_disk_usage(self, used: int, total: int) -> None:
        """records destination disk usage ratio."""
        self.destination_disk_used_ratio = used / total

    def finalize(self, exit_code: int, sync_success: bool) -> None:
        """finalizes metrics after a run completes."""
        self.last_run_timestamp_seconds = time.time()
        self.last_run_success = 1 if sync_success else 0
        self.last_run_exit_code = exit_code
        self.run_duration_seconds = time.perf_counter() - self.run_start_monotonic


def classify_run_failure(error: BaseException) -> str:
    """classifies a run-level failure for metrics."""
    error_message = str(error).lower()
    if "not enough disk space" in error_message:
        return "disk"
    if "timed out" in error_message or "timeout" in error_message:
        return "timeout"
    if "dashcam unavailable" in error_message or "network" in error_message:
        return "network"
    if "http error" in error_message or "status code" in error_message:
        return "http"
    return "unknown"


def load_metrics_state(state_file: str) -> float | None:
    """loads persisted metrics state from disk."""
    try:
        with open(state_file, encoding="utf-8") as f:
            state = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, TypeError) as e:
        cron_logger.warning(
            "Could not read metrics state file : %s; error : %s; ignoring.",
            state_file,
            e,
        )
        return None

    value = state.get("last_successful_file_pull_timestamp_seconds")
    if isinstance(value, (int, float)):
        return float(value)

    cron_logger.warning(
        "Invalid metrics state file : %s; ignoring.",
        state_file,
    )
    return None


def save_metrics_state(state_file: str, metrics: SyncMetrics) -> None:
    """saves persisted metrics state to disk."""
    if metrics.last_successful_file_pull_timestamp_seconds is None:
        return

    state = {
        "last_successful_file_pull_timestamp_seconds": (
            metrics.last_successful_file_pull_timestamp_seconds
        )
    }
    try:
        with open(state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, separators=(",", ":"))
            f.write("\n")
    except OSError as e:
        cron_logger.warning(
            "Could not write metrics state file : %s; error : %s; ignoring.",
            state_file,
            e,
        )


def count_failed_marker_files(destination: str) -> int:
    """counts failed marker files under the destination."""
    marker_glob = os.path.join(destination, "**", "*.failed")
    return len(glob.glob(marker_glob, recursive=True))


def _escape_prometheus_label_value(value: str) -> str:
    """escapes a Prometheus label value."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _format_prometheus_sample(
    name: str, value: int | float, labels: dict[str, str] | None = None
) -> str:
    """formats one Prometheus sample."""
    if not labels:
        return f"{name} {value}"

    label_text = ",".join(
        f'{key}="{_escape_prometheus_label_value(label_value)}"'
        for key, label_value in sorted(labels.items())
    )
    return f"{name}{{{label_text}}} {value}"


def render_metrics(metrics: SyncMetrics) -> str:
    """renders metrics in Prometheus text exposition format."""
    metric_definitions: list[tuple[str, str, list[str]]] = [
        (
            "blackvuesync_last_run_timestamp_seconds",
            "Unix timestamp for the most recent completed run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_last_run_timestamp_seconds",
                    metrics.last_run_timestamp_seconds or 0,
                )
            ],
        ),
        (
            "blackvuesync_last_run_success",
            "Whether the most recent run completed a successful sync.",
            [
                _format_prometheus_sample(
                    "blackvuesync_last_run_success",
                    metrics.last_run_success,
                )
            ],
        ),
        (
            "blackvuesync_last_run_exit_code",
            "Process exit code for the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_last_run_exit_code",
                    (
                        metrics.last_run_exit_code
                        if metrics.last_run_exit_code is not None
                        else 0
                    ),
                )
            ],
        ),
        (
            "blackvuesync_last_run_failure",
            "Run-level failure reason for the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_last_run_failure",
                    value,
                    {"reason": reason},
                )
                for reason, value in sorted((metrics.last_run_failures or {}).items())
            ],
        ),
        (
            "blackvuesync_last_successful_file_pull_timestamp_seconds",
            "Unix timestamp for the most recent successful file pull.",
            [
                _format_prometheus_sample(
                    "blackvuesync_last_successful_file_pull_timestamp_seconds",
                    metrics.last_successful_file_pull_timestamp_seconds or 0,
                )
            ],
        ),
        (
            "blackvuesync_file_download_failures_last_run",
            "File download failures observed in the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_file_download_failures_last_run",
                    count,
                    {"reason": reason},
                )
                for reason, count in sorted(
                    (metrics.file_download_failures_last_run or {}).items()
                )
            ],
        ),
        (
            "blackvuesync_files_downloaded_last_run",
            "Files downloaded in the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_files_downloaded_last_run",
                    metrics.files_downloaded_last_run,
                )
            ],
        ),
        (
            "blackvuesync_run_duration_seconds",
            "Elapsed runtime for the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_run_duration_seconds",
                    metrics.run_duration_seconds,
                )
            ],
        ),
        (
            "blackvuesync_dashcam_recordings_seen",
            "Recordings returned by the dashcam index in the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_dashcam_recordings_seen",
                    metrics.dashcam_recordings_seen,
                )
            ],
        ),
        (
            "blackvuesync_recordings_selected",
            "Recordings selected for download after filtering in the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_recordings_selected",
                    metrics.recordings_selected,
                )
            ],
        ),
        (
            "blackvuesync_bytes_downloaded_last_run",
            "Bytes downloaded in the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_bytes_downloaded_last_run",
                    metrics.bytes_downloaded_last_run,
                )
            ],
        ),
        (
            "blackvuesync_destination_disk_used_ratio",
            "Destination disk usage ratio observed during the most recent run.",
            [
                _format_prometheus_sample(
                    "blackvuesync_destination_disk_used_ratio",
                    metrics.destination_disk_used_ratio or 0,
                )
            ],
        ),
        (
            "blackvuesync_failed_marker_files",
            "Failed marker files under the destination.",
            [
                _format_prometheus_sample(
                    "blackvuesync_failed_marker_files",
                    metrics.failed_marker_files,
                )
            ],
        ),
    ]

    lines = []
    for metric_name, help_text, samples in metric_definitions:
        lines.append(f"# HELP {metric_name} {help_text}")
        lines.append(f"# TYPE {metric_name} gauge")
        lines.extend(samples)

    return "\n".join(lines) + "\n"


def write_metrics_file(metrics_file: str, payload: str) -> None:
    """writes a metrics file atomically."""
    metrics_dir = os.path.dirname(metrics_file) or "."
    temp_file = os.path.join(metrics_dir, f".{os.path.basename(metrics_file)}.tmp")
    try:
        with open(temp_file, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, metrics_file)
    except OSError as e:
        with contextlib.suppress(OSError):
            os.remove(temp_file)
        cron_logger.warning(
            "Could not write metrics file : %s; error : %s; ignoring.",
            metrics_file,
            e,
        )


def _quote_pushgateway_grouping_value(value: str) -> str:
    """quotes a Pushgateway grouping value for a path segment."""
    return urllib.parse.quote(value, safe="")


def get_pushgateway_metrics_url(
    pushgateway_url: str, metrics_job: str, metrics_instance: str
) -> str:
    """returns the Pushgateway metrics endpoint URL."""
    return (
        f"{pushgateway_url.rstrip('/')}/metrics/job/"
        f"{_quote_pushgateway_grouping_value(metrics_job)}/instance/"
        f"{_quote_pushgateway_grouping_value(metrics_instance)}"
    )


def push_metrics(
    pushgateway_url: str,
    metrics_job: str,
    metrics_instance: str,
    payload: str,
    timeout: float,
) -> None:
    """pushes metrics to a Pushgateway."""
    url = get_pushgateway_metrics_url(pushgateway_url, metrics_job, metrics_instance)
    request = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        method="PUT",
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout):
            pass
    except OSError as e:
        cron_logger.warning(
            "Could not push metrics to Pushgateway : %s; error : %s; ignoring.",
            url,
            e,
        )


def emit_metrics(
    metrics: SyncMetrics,
    metrics_file: str | None,
    pushgateway_url: str | None,
    timeout: float,
) -> None:
    """emits metrics to all configured sinks."""
    payload = render_metrics(metrics)

    if metrics_file:
        write_metrics_file(metrics_file, payload)

    if pushgateway_url:
        push_metrics(
            pushgateway_url,
            metrics.metrics_job,
            metrics.metrics_instance,
            payload,
            timeout,
        )
