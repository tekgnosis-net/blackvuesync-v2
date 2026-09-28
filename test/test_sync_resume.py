"""tests for byte-range resume in download_file."""

from __future__ import annotations

import re
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread

import pytest

import blackvuesync_v2.sync as _sync
from blackvuesync_v2.sync import download_file

_PAYLOAD = b"".join(bytes([i % 256]) for i in range(1024 * 7))  # 7 KiB
_FILENAME = "20230101_120000_NF.mp4"
_RANGE_RE = re.compile(r"bytes=(\d+)-")


class _RangeHandler(BaseHTTPRequestHandler):
    """serves _PAYLOAD honoring a single open-ended Range with 206.

    returns 416 when the requested start is >= len(_PAYLOAD).
    """

    seen_range: str | None = None

    def do_GET(self) -> None:  # noqa: N802
        rng = self.headers.get("Range")
        type(self).seen_range = rng
        if rng and (m := _RANGE_RE.fullmatch(rng.strip())):
            start = int(m.group(1))
            if start >= len(_PAYLOAD):
                self.send_response(416)
                self.end_headers()
                return
            body = _PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header(
                "Content-Range", f"bytes {start}-{len(_PAYLOAD) - 1}/{len(_PAYLOAD)}"
            )
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        """silences test server logging."""


class _MismatchedRangeHandler(BaseHTTPRequestHandler):
    """always responds 206 with Content-Range starting at 0, regardless of the
    requested Range; simulates a server that sends the full payload as 206."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(206)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.send_header(
            "Content-Range",
            f"bytes 0-{len(_PAYLOAD) - 1}/{len(_PAYLOAD)}",
        )
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        """silences test server logging."""


class _NoRangeHandler(BaseHTTPRequestHandler):
    """ignores Range; always returns the whole payload with 200."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        """silences test server logging."""


class _UnconfirmedPartialHandler(BaseHTTPRequestHandler):
    """on a Range request returns 206 with a partial tail body but NO Content-Range
    header; on a plain request returns 200 with the full payload.

    simulates a server that responds 206 without confirming the resume offset,
    which would corrupt the destination file if written with 'wb'.
    """

    def do_GET(self) -> None:  # noqa: N802
        rng = self.headers.get("Range")
        if rng and (m := _RANGE_RE.fullmatch(rng.strip())):
            start = int(m.group(1))
            body = _PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            # deliberately omits Content-Range to leave the resume unconfirmed
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        """silences test server logging."""


def _serve(handler: type[BaseHTTPRequestHandler]) -> Generator[str, None, None]:
    handler.seen_range = None  # type: ignore[attr-defined]
    server = HTTPServer(("127.0.0.1", 0), handler)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()


@pytest.fixture()
def range_server() -> Generator[str, None, None]:
    yield from _serve(_RangeHandler)


@pytest.fixture()
def norange_server() -> Generator[str, None, None]:
    yield from _serve(_NoRangeHandler)


@pytest.fixture()
def mismatched_range_server() -> Generator[str, None, None]:
    yield from _serve(_MismatchedRangeHandler)


@pytest.fixture()
def unconfirmed_partial_server() -> Generator[str, None, None]:
    yield from _serve(_UnconfirmedPartialHandler)


def _seed_partial(destination: Path, nbytes: int) -> None:
    (destination / f".{_FILENAME}").write_bytes(_PAYLOAD[:nbytes])


def _final(destination: Path) -> bytes:
    return (destination / _FILENAME).read_bytes()


class TestResume:
    def test_resumes_from_partial_with_206(
        self, range_server: str, tmp_path: Path
    ) -> None:
        _seed_partial(tmp_path, 3000)
        ok, _ = download_file(range_server, _FILENAME, str(tmp_path), None)
        assert ok is True
        assert _final(tmp_path) == _PAYLOAD
        assert _RangeHandler.seen_range == "bytes=3000-"

    def test_only_tail_transferred_on_resume(
        self, range_server: str, tmp_path: Path
    ) -> None:
        _seed_partial(tmp_path, 5000)
        totals: list[int] = []
        download_file(
            range_server,
            _FILENAME,
            str(tmp_path),
            None,
            on_chunk=lambda _d, t: totals.append(t),
        )
        # on_chunk total reflects the whole file, not the 2 KiB tail
        assert totals and all(t == len(_PAYLOAD) for t in totals)

    def test_no_range_header_when_no_partial(
        self, range_server: str, tmp_path: Path
    ) -> None:
        ok, _ = download_file(range_server, _FILENAME, str(tmp_path), None)
        assert ok is True
        assert _final(tmp_path) == _PAYLOAD
        assert _RangeHandler.seen_range is None

    def test_falls_back_to_full_download_when_server_ignores_range(
        self, norange_server: str, tmp_path: Path
    ) -> None:
        _seed_partial(tmp_path, 4000)
        ok, _ = download_file(norange_server, _FILENAME, str(tmp_path), None)
        assert ok is True
        # a 200 truncates the partial and rewrites cleanly
        assert _final(tmp_path) == _PAYLOAD

    def test_stop_mid_resume_keeps_partial(
        self, range_server: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_partial(tmp_path, 1000)
        monkeypatch.setattr(_sync, "is_stop_requested", lambda: True)
        with pytest.raises(UserWarning):
            download_file(range_server, _FILENAME, str(tmp_path), None)
        # partial survives (and is at least the seeded size); no final file
        assert (tmp_path / f".{_FILENAME}").exists()
        assert not (tmp_path / _FILENAME).exists()

    def test_mismatched_content_range_start_triggers_full_restart(
        self, mismatched_range_server: str, tmp_path: Path
    ) -> None:
        # server returns 206 but Content-Range start is 0, not 3000;
        # download_file must treat this as a full restart and produce the correct file.
        _seed_partial(tmp_path, 3000)
        ok, _ = download_file(mismatched_range_server, _FILENAME, str(tmp_path), None)
        assert ok is True
        assert _final(tmp_path) == _PAYLOAD

    def test_416_discards_partial_and_restarts(
        self, range_server: str, tmp_path: Path
    ) -> None:
        # seed a partial larger than _PAYLOAD so the resume offset exceeds the
        # source size; the server returns 416, the partial is discarded, and
        # download_file retries once from byte 0 producing the correct final file.
        oversized = _PAYLOAD + b"\x00" * 100
        (tmp_path / f".{_FILENAME}").write_bytes(oversized)
        ok, _ = download_file(range_server, _FILENAME, str(tmp_path), None)
        assert ok is True
        assert _final(tmp_path) == _PAYLOAD
        # partial dotfile must be gone after the successful retry
        assert not (tmp_path / f".{_FILENAME}").exists()

    def test_unconfirmed_206_discards_partial_and_restarts(
        self, unconfirmed_partial_server: str, tmp_path: Path
    ) -> None:
        # server returns 206 with a partial tail body but no Content-Range header;
        # download_file must NOT write the tail as a complete file (corruption).
        # it must discard the partial and restart from byte 0, producing the full
        # payload via the server's 200 path.
        _seed_partial(tmp_path, 3000)
        ok, _ = download_file(
            unconfirmed_partial_server, _FILENAME, str(tmp_path), None
        )
        assert ok is True
        assert _final(tmp_path) == _PAYLOAD
        assert not (tmp_path / f".{_FILENAME}").exists()
