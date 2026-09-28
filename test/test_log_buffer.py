"""unit tests for the in-memory ring-buffer log handler."""

from __future__ import annotations

import logging
import threading
import time

from blackvuesync_v2.server.log_buffer import LogBuffer, LogLine, verbosity_token


def _record(
    msg: str, level: int = logging.INFO, name: str = "test"
) -> logging.LogRecord:
    return logging.LogRecord(name, level, "path.py", 1, msg, None, None)


def test_emit_then_snapshot_returns_logline() -> None:
    buf = LogBuffer(capacity=10)
    buf.emit(_record("hello world", logging.WARNING, "blackvuesync"))
    lines = buf.snapshot()
    assert len(lines) == 1
    ln = lines[0]
    assert isinstance(ln, LogLine)
    assert ln.message == "hello world"
    assert ln.level == "WARNING"
    assert ln.level_no == logging.WARNING
    assert ln.logger == "blackvuesync"
    assert ln.seq == 1
    assert ln.ts.endswith("Z")


def test_deque_evicts_oldest_beyond_capacity() -> None:
    buf = LogBuffer(capacity=3)
    for i in range(5):
        buf.emit(_record(f"line {i}"))
    msgs = [ln.message for ln in buf.snapshot()]
    assert msgs == ["line 2", "line 3", "line 4"]


def test_seq_is_monotonic_and_matches_order() -> None:
    buf = LogBuffer(capacity=100)
    for i in range(10):
        buf.emit(_record(f"line {i}"))
    seqs = [ln.seq for ln in buf.snapshot()]
    assert seqs == list(range(1, 11))


def test_subscribe_yields_new_lines_in_batches_no_drops() -> None:
    buf = LogBuffer(capacity=100)
    gen = buf.subscribe()
    for i in range(3):
        buf.emit(_record(f"line {i}"))
    batch = next(gen)
    # all three queued lines arrive (possibly coalesced into one batch)
    collected = list(batch)
    while len(collected) < 3:
        collected += next(gen)
    assert [ln.message for ln in collected] == ["line 0", "line 1", "line 2"]
    gen.close()


def test_set_capacity_resizes_and_truncates() -> None:
    buf = LogBuffer(capacity=5)
    for i in range(5):
        buf.emit(_record(f"line {i}"))
    buf.set_capacity(2)
    assert [ln.message for ln in buf.snapshot()] == ["line 3", "line 4"]
    assert buf.capacity == 2


def test_emit_is_threadsafe_under_concurrency() -> None:
    buf = LogBuffer(capacity=10000)

    def worker() -> None:
        for i in range(500):
            buf.emit(_record(f"x{i}"))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = buf.snapshot()
    assert len(snap) == 2000
    # seq values are unique and strictly increasing in storage order
    seqs = [ln.seq for ln in snap]
    assert len(set(seqs)) == 2000
    assert seqs == sorted(seqs)


def test_emit_never_raises_on_bad_format_args() -> None:
    buf = LogBuffer(capacity=10)
    bad = logging.LogRecord("t", logging.INFO, "p", 1, "%d and %d", (1,), None)
    buf.emit(bad)  # getMessage() would raise; emit must swallow via handleError
    # the buffer simply has no line (or a safe one); the call did not raise
    assert isinstance(buf.snapshot(), list)


def test_verbosity_token_maps_quiet_verbose() -> None:
    class _L:
        def __init__(self, quiet: bool, verbose: int) -> None:
            self.quiet = quiet
            self.verbose = verbose

    assert verbosity_token(_L(True, 0)) == "quiet"
    assert verbosity_token(_L(False, 0)) == "normal"
    assert verbosity_token(_L(False, 1)) == "verbose"
    assert verbosity_token(_L(False, 2)) == "debug"
    assert verbosity_token(_L(False, 5)) == "debug"


def test_subscribe_heartbeat_yields_empty_list_quickly() -> None:
    buf = LogBuffer(capacity=10)
    buf.HEARTBEAT_SECONDS = 0.05  # shrink for the test
    gen = buf.subscribe()
    start = time.monotonic()
    batch = next(gen)
    assert batch == []
    assert time.monotonic() - start < 1.0
    gen.close()


def test_build_file_handler_creates_logs_dir(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from blackvuesync_v2.__main__ import _build_file_handler

    class _L:
        format = "text"
        file_max_bytes = 1024
        file_backup_count = 2

    log_dir = tmp_path / "logs"
    handler = _build_file_handler(_L(), log_dir)
    try:
        assert log_dir.is_dir()
        assert handler.baseFilename == str(log_dir / "blackvuesync.log")
        assert handler.maxBytes == 1024
        assert handler.backupCount == 2
    finally:
        handler.close()


def test_reconfigure_serve_logging_resizes_buffer(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import dataclasses as _dc
    import os as _os
    from unittest.mock import patch as _patch

    from blackvuesync_v2.__main__ import _build_file_handler, _reconfigure_serve_logging
    from blackvuesync_v2.settings import SettingsStore

    with _patch.dict(_os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    old = store.get()
    new = _dc.replace(old, logging=_dc.replace(old.logging, ring_buffer_capacity=7))
    buf = LogBuffer(capacity=1000)
    log_dir = tmp_path / "logs"
    box = [_build_file_handler(old.logging, log_dir)]
    try:
        _reconfigure_serve_logging(old, new, buf, box, log_dir)
        assert buf.capacity == 7
    finally:
        box[0].close()


def test_reconfigure_serve_logging_swaps_file_handler(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import dataclasses as _dc
    import logging as _logging
    import os as _os
    from unittest.mock import patch as _patch

    from blackvuesync_v2.__main__ import _build_file_handler, _reconfigure_serve_logging
    from blackvuesync_v2.settings import SettingsStore

    with _patch.dict(_os.environ, {"ADDRESS": "192.168.0.1"}, clear=False):
        store = SettingsStore(tmp_path / "settings.json")
    old = store.get()
    new = _dc.replace(old, logging=_dc.replace(old.logging, file_backup_count=9))
    buf = LogBuffer(capacity=10)
    log_dir = tmp_path / "logs"
    first = _build_file_handler(old.logging, log_dir)
    box = [first]
    root = _logging.getLogger()
    root.addHandler(first)
    try:
        _reconfigure_serve_logging(old, new, buf, box, log_dir)
        assert box[0] is not first
        assert box[0].backupCount == 9
        assert first not in root.handlers
        assert box[0] in root.handlers
    finally:
        root.removeHandler(box[0])
        box[0].close()
        first.close()
