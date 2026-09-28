"""cli entry point for blackvuesync-v2."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

import blackvuesync_v2.sync as _sync
from blackvuesync_v2 import __version__
from blackvuesync_v2.metrics import (
    METRICS_DEFAULT_JOB,
    SyncMetrics,
    classify_run_failure,
    count_failed_marker_files,
    default_metrics_state_file,
    emit_metrics,
    load_metrics_state,
    metrics_enabled,
    parse_pushgateway_url,
    save_metrics_state,
)
from blackvuesync_v2.settings import LoggingSettings, Settings, SettingsStore
from blackvuesync_v2.sync import (
    LOG_FORMATS,
    calc_cutoff_date,
    clean_destination,
    configure_logging,
    ensure_destination,
    flush_logs,
    lock,
    parse_duration,
    parse_filter,
    parse_skip_metadata,
    set_logging_levels,
    sync,
    unlock,
)

if TYPE_CHECKING:
    from blackvuesync_v2.server.log_buffer import LogBuffer

# module-level loggers
logger = logging.getLogger()
cron_logger = logging.getLogger("cron")

# default settings file path; can be overridden for testing
_DEFAULT_SETTINGS_PATH = Path(
    os.environ.get("BLACKVUESYNC_CONFIG_PATH", "/config/settings.json")
)

# waitress worker threads; each open SSE stream (dashboard, logs) holds one
# for its lifetime, so the default of 4 starves /healthz with a few tabs open.
WAITRESS_THREADS = 32


def _build_sync_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """adds sync subcommand arguments to subparsers."""
    sync_parser = subparsers.add_parser(
        "sync",
        help="sync recordings from a dashcam to a local directory",
        description="Synchronizes BlackVue dashcam recordings with a local directory.",
    )
    sync_parser.add_argument(
        "address", metavar="ADDRESS", help="dashcam IP address or name"
    )
    sync_parser.add_argument(
        "-d",
        "--destination",
        metavar="DEST",
        help="sets the destination directory to DEST; defaults to the current directory",
    )
    sync_parser.add_argument(
        "-g",
        "--grouping",
        metavar="GROUPING",
        default="none",
        choices=["none", "daily", "weekly", "monthly", "yearly"],
        help="groups recording by day, week, month or year under a directory named after the date; so respectively 2019-06-15, 2019-06-09 (Mon), 2019-07 or 2019; defaults to none, indicating no grouping",
    )
    sync_parser.add_argument(
        "-k",
        "--keep",
        metavar="KEEP_RANGE",
        help="keeps recordings in the given range, removing the rest; defaults to days, but can suffix with d, w for days or weeks respectively",
    )
    sync_parser.add_argument(
        "-p",
        "--priority",
        metavar="DOWNLOAD_PRIORITY",
        default="date",
        choices=["date", "rdate", "type"],
        help="sets the recording download priority; date: downloads in chronological order from oldest to newest; rdate: downloads in chronological order from newest to oldest; type: prioritizes manual, event, normal and then parkingrecordings; defaults to date",
    )
    sync_parser.add_argument(
        "-i",
        "--include",
        default=None,
        type=parse_filter,
        help="downloads only recordings matching the given codes; each code is a recording type optionally followed by a camera direction; e.g. --include P,NF downloads all Parking and Normal Front recordings",
    )
    sync_parser.add_argument(
        "-e",
        "--exclude",
        default=None,
        type=parse_filter,
        help="excludes recordings matching the given codes; takes priority over --include; e.g. --include N,E --exclude NR downloads all Normal and Event recordings except Normal Rear",
    )
    sync_parser.add_argument(
        "-u",
        "--max-used-disk",
        metavar="DISK_USAGE_PERCENT",
        default=90,
        type=int,
        choices=range(5, 99),
        help="stops downloading recordings if disk is over DISK_USAGE_PERCENT used; defaults to 90",
    )
    sync_parser.add_argument(
        "-t",
        "--timeout",
        metavar="TIMEOUT",
        default=10.0,
        type=float,
        help="sets the connection timeout in seconds (float); defaults to 10.0 seconds",
    )
    sync_parser.add_argument(
        "--retry-failed-after",
        metavar="DURATION",
        default="1d",
        help="waits at least the given duration before retrying a failed download; defaults to days, but can suffix with s, h, d, w for seconds, hours, days or weeks respectively; defaults to 1d",
    )
    sync_parser.add_argument(
        "--skip-metadata",
        metavar="TYPES",
        default=set(),
        type=parse_skip_metadata,
        help="skips downloading metadata file types; t=thumbnail (.thm), 3=accelerometer (.3gf), g=gps (.gps); e.g. --skip-metadata t3g skips all metadata files",
    )
    sync_parser.add_argument(
        "-v", "--verbose", action="count", default=0, help="increases verbosity"
    )
    sync_parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="quiets down output messages; overrides verbosity options",
    )
    sync_parser.add_argument(
        "--log-format",
        default="text",
        choices=LOG_FORMATS,
        help="sets log output format; defaults to text",
    )
    sync_parser.add_argument(
        "--metrics-file",
        metavar="PATH",
        help="writes Prometheus metrics text format to PATH",
    )
    sync_parser.add_argument(
        "--metrics-pushgateway-url",
        metavar="URL",
        type=parse_pushgateway_url,
        help="pushes Prometheus metrics to the Pushgateway URL",
    )
    sync_parser.add_argument(
        "--metrics-job",
        metavar="NAME",
        default=METRICS_DEFAULT_JOB,
        help=f"sets the Pushgateway metrics job; defaults to {METRICS_DEFAULT_JOB}",
    )
    sync_parser.add_argument(
        "--metrics-instance",
        metavar="NAME",
        help="sets the Pushgateway metrics instance; defaults to ADDRESS",
    )
    sync_parser.add_argument(
        "--metrics-state-file",
        metavar="PATH",
        help="persists cross-run metrics state at PATH",
    )
    sync_parser.add_argument(
        "--cron",
        action="store_true",
        help="cron mode, only logs normal recordings at default verbosity",
    )
    sync_parser.add_argument(
        "--dry-run", action="store_true", help="shows what the program would do"
    )
    sync_parser.add_argument(
        "--affinity-key",
        metavar="AFFINITY_KEY",
        help="affinity key; reserved for test isolation",
    )


def _build_serve_parser(subparsers: argparse._SubParsersAction) -> None:  # type: ignore[type-arg]
    """adds serve subcommand arguments to subparsers."""
    serve_parser = subparsers.add_parser(
        "serve",
        help="start the web server",
        description="Starts the BlackVue Sync v2 web server.",
    )
    serve_parser.add_argument(
        "--port",
        metavar="PORT",
        type=int,
        default=None,
        help="overrides the port from settings.json; defaults to settings.web.port (8080)",
    )
    serve_parser.add_argument(
        "--config-path",
        metavar="PATH",
        default=None,
        help="path to settings.json; overrides BLACKVUESYNC_CONFIG_PATH env var",
    )


def parse_args() -> argparse.Namespace:
    """parses the command-line arguments, dispatching to sync or serve subcommands.

    for backward compatibility, if the first argument is not a known
    subcommand, the entire argv is treated as sync-mode arguments.
    """
    # detects legacy invocation: first arg is an IP address or hostname, not a
    # subcommand name. rewrites sys.argv so the subparsers handle it uniformly.
    known_subcommands = {"sync", "serve"}
    if (
        len(sys.argv) > 1
        and sys.argv[1] not in known_subcommands
        and not sys.argv[1].startswith("-")
    ):
        # inserts "sync" before the positional ADDRESS argument
        sys.argv = [sys.argv[0], "sync"] + sys.argv[1:]

    arg_parser = argparse.ArgumentParser(
        prog="blackvuesync-v2",
        description="Synchronizes BlackVue dashcam recordings with a local directory.",
        epilog="Bug reports: https://github.com/tekgnosis-net/blackvuesync-v2/issues",
    )
    arg_parser.add_argument(
        "--version",
        action="version",
        default=__version__,
        version=f"%(prog)s {__version__}",
        help="shows the version and exits",
    )

    subparsers = arg_parser.add_subparsers(dest="subcommand")
    _build_sync_parser(subparsers)
    _build_serve_parser(subparsers)

    return arg_parser.parse_args()


def _apply_logging_settings(logging_settings: LoggingSettings) -> None:
    """applies log format and level from the logging section. shared by startup
    and the on_change listener so a logging change takes effect live. serve mode
    keeps a floor of INFO so scheduler/waitress lines always emit; quiet still
    suppresses everything below ERROR."""
    configure_logging(logging_settings.format)
    if logging_settings.quiet:
        set_logging_levels(-1, False)
    else:
        set_logging_levels(max(1, logging_settings.verbose), False)


def _register_logging_reload(store: SettingsStore) -> None:
    """re-applies logging settings whenever the logging section changes, so a
    settings update takes effect without a restart (logging is TIER immediate)."""

    def _on_change(old: Settings, new: Settings) -> None:
        """re-applies logging when the logging section changes."""
        if new.logging != old.logging:
            _apply_logging_settings(new.logging)

    store.on_change(_on_change)


def _build_file_handler(logging_settings: Any, log_dir: Path) -> RotatingFileHandler:
    """creates the rotating file handler under log_dir, making the dir if absent.

    serve mode only; the file survives restarts and is browsed on the host.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        log_dir.chmod(0o700)
    return RotatingFileHandler(
        str(log_dir / "blackvuesync.log"),
        maxBytes=logging_settings.file_max_bytes,
        backupCount=logging_settings.file_backup_count,
        encoding="utf-8",
    )


def _reconfigure_serve_logging(
    old: Settings,
    new: Settings,
    log_buffer: LogBuffer,
    file_handler_box: list[RotatingFileHandler],
    log_dir: Path,
) -> None:
    """re-applies serve-only logging handlers when the logging section changes.

    resizes the ring buffer and rebuilds the rotating file handler in place,
    then re-applies format and level to every handler (including the new one).
    file_handler_box is a single-element holder so the swapped handler is
    visible to the caller across invocations.
    """
    if new.logging == old.logging:
        return
    if new.logging.ring_buffer_capacity != old.logging.ring_buffer_capacity:
        log_buffer.set_capacity(new.logging.ring_buffer_capacity)
    if (
        new.logging.file_max_bytes != old.logging.file_max_bytes
        or new.logging.file_backup_count != old.logging.file_backup_count
    ):
        root = logging.getLogger()
        old_handler = file_handler_box[0]
        root.removeHandler(old_handler)
        old_handler.close()
        new_handler = _build_file_handler(new.logging, log_dir)
        root.addHandler(new_handler)
        file_handler_box[0] = new_handler
    _apply_logging_settings(new.logging)


def cmd_sync(args: argparse.Namespace) -> int:
    """runs the sync workflow and returns the exit code."""
    # pylint: disable=too-many-branches,too-many-statements

    configure_logging(args.log_format)
    set_logging_levels(-1 if args.quiet else args.verbose, args.cron)

    _sync.dry_run = args.dry_run
    _sync.affinity_key = args.affinity_key
    _sync.skip_metadata = args.skip_metadata
    if _sync.skip_metadata:
        logger.info(
            "Skipping metadata types : %s",
            ", ".join(sorted(_sync.skip_metadata)),
            extra={
                "event": "skip_metadata_configured",
                "metadata_types": sorted(_sync.skip_metadata),
            },
        )
    if _sync.dry_run:
        logger.info(
            "DRY RUN No action will be taken.",
            extra={"event": "dry_run_enabled"},
        )

    _sync.max_disk_used_percent = args.max_used_disk

    # sets socket timeout
    timeout: float = args.timeout
    if timeout <= 0:
        raise argparse.ArgumentTypeError("TIMEOUT must be greater than zero.")
    _sync.socket_timeout = timeout
    socket.setdefaulttimeout(timeout)

    destination = args.destination or os.getcwd()
    grouping = args.grouping
    lf_fd = None
    exit_code = 0
    sync_success = False
    metrics = None
    metrics_state_file = None

    if metrics_enabled(args):
        metrics_state_file = args.metrics_state_file or default_metrics_state_file(
            destination
        )
        metrics = SyncMetrics(
            run_start_monotonic=time.perf_counter(),
            run_start_timestamp=time.time(),
            dry_run=args.dry_run,
            metrics_job=args.metrics_job,
            metrics_instance=args.metrics_instance or args.address,
        )
        metrics.last_successful_file_pull_timestamp_seconds = load_metrics_state(
            metrics_state_file
        )

    try:
        if args.keep:
            _sync.cutoff_date = calc_cutoff_date(args.keep)
            logger.info(
                "Recording cutoff date : %s",
                _sync.cutoff_date,
                extra={
                    "event": "recording_cutoff_configured",
                    "cutoff_date": _sync.cutoff_date,
                },
            )

        _sync.retry_failed_after = parse_duration(
            args.retry_failed_after, label="RETRY_FAILED_AFTER"
        )

        # prepares the local file destination
        ensure_destination(destination)

        lf_fd = lock(destination)

        try:
            sync(
                args.address,
                destination,
                grouping,
                args.priority,
                args.include,
                args.exclude,
                metrics,
            )
            sync_success = True
        finally:
            # removes temporary files (if we synced successfully, these are temp files from lost recordings)
            clean_destination(destination, grouping)
    except UserWarning as e:
        logger.warning(
            e.args[0],
            extra={
                "event": "sync_warning",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        if metrics:
            metrics.record_run_failure(classify_run_failure(e))
        exit_code = 0 if args.cron else 1
    except RuntimeError as e:
        logger.exception(
            e.args[0],
            extra={
                "event": "sync_error",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        if metrics:
            metrics.record_run_failure(classify_run_failure(e))
        exit_code = 2
    except Exception as e:  # pylint: disable=broad-exception-caught
        logger.exception(
            e,
            extra={
                "event": "sync_unexpected_error",
                "error_type": type(e).__name__,
                "error": str(e),
            },
        )
        if metrics:
            metrics.record_run_failure(classify_run_failure(e))
        exit_code = 3
    finally:
        if lf_fd is not None:
            unlock(lf_fd)

        if metrics:
            with contextlib.suppress(OSError):
                metrics.failed_marker_files = count_failed_marker_files(destination)
            metrics.finalize(exit_code, sync_success)
            if metrics_state_file:
                save_metrics_state(metrics_state_file, metrics)
            emit_metrics(
                metrics,
                args.metrics_file,
                args.metrics_pushgateway_url,
                timeout,
            )

        flush_logs()

    return exit_code


def cmd_serve(args: argparse.Namespace) -> int:  # pylint: disable=too-many-locals
    """starts the web server and APScheduler; blocks until interrupted."""
    # configures logging using the settings.logging section once we have
    # loaded the settings store below. for now configure with defaults so
    # startup messages (including settings-load errors) are visible.
    configure_logging("text")
    set_logging_levels(1, False)

    # deferred imports keep these optional at module load time; the sync
    # subcommand does not need flask, waitress, or apscheduler.
    # pylint: disable=import-outside-toplevel
    import waitress

    from blackvuesync_v2.server import create_app
    from blackvuesync_v2.server.log_buffer import LogBuffer
    from blackvuesync_v2.server.progress import ProgressPublisher
    from blackvuesync_v2.server.scheduler import init_scheduler
    from blackvuesync_v2.server.stats_store import StatsStore

    # pylint: enable=import-outside-toplevel

    config_path = Path(args.config_path) if args.config_path else _DEFAULT_SETTINGS_PATH
    store = SettingsStore(config_path)
    settings = store.get()
    # re-applies log format and level now that settings are loaded.
    # configure_logging is idempotent: it iterates existing handlers and calls
    # setFormatter, so the second call replaces the bootstrap formatter without
    # duplicating handlers. serve mode keeps a floor of INFO so scheduler /
    # waitress startup lines always emit regardless of the user's verbose
    # setting (operators need to see them in docker logs); quiet=true still
    # suppresses everything below ERROR.
    _apply_logging_settings(settings.logging)

    # serve-only durable + live log sinks (sync.py stays stdlib-only / no file).
    root_logger = logging.getLogger()
    log_buffer = LogBuffer(capacity=settings.logging.ring_buffer_capacity)
    root_logger.addHandler(log_buffer)
    log_dir = config_path.parent / "logs"
    file_handler = _build_file_handler(settings.logging, log_dir)
    root_logger.addHandler(file_handler)
    log_file_path = str(log_dir / "blackvuesync.log")
    # re-applies the formatter to the two handlers just attached.
    configure_logging(settings.logging.format)

    publisher = ProgressPublisher()
    # persists per-run metrics next to settings.json so the stats page survives
    # restarts; the scheduler records into it after every run.
    stats_store = StatsStore(str(config_path.parent / "stats.db"))
    app = create_app(
        store,
        progress_publisher=publisher,
        log_buffer=log_buffer,
        log_file_path=log_file_path,
        stats_store=stats_store,
    )
    port = args.port if args.port is not None else settings.web.port

    scheduler = init_scheduler(store, publisher, stats_store)
    _register_logging_reload(store)
    # second listener: resizes the ring buffer / rebuilds the file handler live.
    file_handler_box = [file_handler]
    store.on_change(
        lambda old, new: _reconfigure_serve_logging(
            old, new, log_buffer, file_handler_box, log_dir
        )
    )
    logger.info(
        "scheduler started: %r (%s)",
        settings.schedule.cron_expression,
        settings.schedule.timezone,
    )
    logger.info("starting web server on 0.0.0.0:%d", port)
    try:
        waitress.serve(app, host="0.0.0.0", port=port, threads=WAITRESS_THREADS)
    finally:
        # waits for the active sync (if any) to finish gracefully on SIGTERM.
        scheduler.shutdown(wait=True)
    return 0


def main() -> int:
    """dispatches to sync or serve subcommand and returns the exit code.

    only serve loads (or seeds) settings.json, at the path it is given; the
    sync subcommand is configured entirely by its arguments.
    """
    args = parse_args()

    # subcommand may be absent when parse_args is monkey-patched in tests or
    # when the argparse namespace is constructed manually; defaults to "sync".
    subcommand = getattr(args, "subcommand", "sync")

    if subcommand == "serve":
        return cmd_serve(args)

    # defaults to sync when subcommand is "sync" or absent (legacy argv rewrite)
    return cmd_sync(args)


if __name__ == "__main__":
    sys.exit(main())
