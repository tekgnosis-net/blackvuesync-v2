"""tests for download_file on_chunk callback and publisher integration in sync."""

from __future__ import annotations

from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from unittest.mock import MagicMock, patch

import pytest

from blackvuesync_v2.sync import download_file

# ---------------------------------------------------------------------------
# mini in-process HTTP server for download_file tests
# ---------------------------------------------------------------------------

_PAYLOAD = b"A" * (1024 * 5)  # 5 KiB; smaller than DOWNLOAD_CHUNK_SIZE (1 MiB)


class _OneFileHandler(BaseHTTPRequestHandler):
    """serves a single fixed payload for any GET /Record/* request."""

    def do_GET(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """suppresses server log output during tests."""


@pytest.fixture()
def http_server() -> Generator[tuple[str, int], None, None]:
    """starts a one-shot HTTP server; yields (host, port)."""
    server = HTTPServer(("127.0.0.1", 0), _OneFileHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield "127.0.0.1", server.server_address[1]
    server.shutdown()


# ---------------------------------------------------------------------------
# on_chunk callback tests
# ---------------------------------------------------------------------------


class TestDownloadFileOnChunk:
    """tests for download_file on_chunk callback invocations."""

    def test_on_chunk_called_at_least_once(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        host, port = http_server
        base_url = f"http://{host}:{port}/"
        calls: list[tuple[int, int]] = []

        def cb(downloaded: int, total: int) -> None:
            calls.append((downloaded, total))

        downloaded, _ = download_file(
            base_url,
            "20230101_120000_NF.mp4",
            str(tmp_path),
            None,
            on_chunk=cb,
        )
        assert downloaded is True
        assert len(calls) >= 1

    def test_on_chunk_downloaded_bytes_matches_payload(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        host, port = http_server
        base_url = f"http://{host}:{port}/"
        calls: list[tuple[int, int]] = []

        def cb(downloaded: int, total: int) -> None:
            calls.append((downloaded, total))

        download_file(
            base_url,
            "20230101_120000_NF.mp4",
            str(tmp_path),
            None,
            on_chunk=cb,
        )
        # last call should have downloaded == total == payload size
        final_downloaded, final_total = calls[-1]
        assert final_downloaded == len(_PAYLOAD)
        assert final_total == len(_PAYLOAD)

    def test_on_chunk_monotonically_increasing(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        host, port = http_server
        base_url = f"http://{host}:{port}/"
        calls: list[int] = []

        def cb(downloaded: int, _total: int) -> None:
            calls.append(downloaded)

        download_file(
            base_url,
            "20230101_120000_NF.mp4",
            str(tmp_path),
            None,
            on_chunk=cb,
        )
        # downloaded bytes should be non-decreasing
        assert all(calls[i] <= calls[i + 1] for i in range(len(calls) - 1))

    def test_no_on_chunk_works_unchanged(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        host, port = http_server
        base_url = f"http://{host}:{port}/"

        downloaded, _ = download_file(
            base_url,
            "20230101_120000_NF.mp4",
            str(tmp_path),
            None,
        )
        assert downloaded is True
        assert (tmp_path / "20230101_120000_NF.mp4").exists()

    def test_on_chunk_not_called_for_already_downloaded_file(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        host, port = http_server
        base_url = f"http://{host}:{port}/"
        # pre-create the destination file
        dest = tmp_path / "20230101_120000_NF.mp4"
        dest.write_bytes(b"existing")

        calls: list[tuple[int, int]] = []

        def cb(downloaded: int, total: int) -> None:
            calls.append((downloaded, total))

        downloaded, _ = download_file(
            base_url,
            "20230101_120000_NF.mp4",
            str(tmp_path),
            None,
            on_chunk=cb,
        )
        assert downloaded is False
        assert len(calls) == 0


# ---------------------------------------------------------------------------
# publisher integration in download_recording
# ---------------------------------------------------------------------------


class TestDownloadRecordingPublisher:
    """tests that download_recording calls the publisher writer api."""

    def _make_mock_publisher(self) -> MagicMock:
        pub = MagicMock()
        pub.update_bytes = MagicMock()
        pub.start_file = MagicMock()
        pub.finish_file = MagicMock()
        return pub

    def test_start_file_called_for_mp4(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        from blackvuesync_v2.sync import download_recording, to_recording

        host, port = http_server
        base_url = f"http://{host}:{port}/"
        pub = self._make_mock_publisher()

        recording = to_recording("20230101_120000_NF.mp4", "none")
        assert recording is not None

        with patch("blackvuesync_v2.sync.skip_metadata", set()):
            download_recording(base_url, recording, str(tmp_path), publisher=pub)

        # start_file should be called for at least the mp4
        calls_args = [c[0][0] for c in pub.start_file.call_args_list]
        assert "20230101_120000_NF.mp4" in calls_args

    def test_finish_file_called_for_successful_download(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        from blackvuesync_v2.sync import download_recording, to_recording

        host, port = http_server
        base_url = f"http://{host}:{port}/"
        pub = self._make_mock_publisher()

        recording = to_recording("20230101_120000_NF.mp4", "none")
        assert recording is not None

        with patch("blackvuesync_v2.sync.skip_metadata", set()):
            download_recording(base_url, recording, str(tmp_path), publisher=pub)

        assert pub.finish_file.called
        # at least one success=True call
        success_calls = [
            c for c in pub.finish_file.call_args_list if c[1].get("success") is True
        ]
        assert len(success_calls) >= 1

    def test_no_publisher_works_unchanged(
        self,
        http_server: tuple[str, int],
        tmp_path: Path,
    ) -> None:
        from blackvuesync_v2.sync import download_recording, to_recording

        host, port = http_server
        base_url = f"http://{host}:{port}/"

        recording = to_recording("20230101_120000_NF.mp4", "none")
        assert recording is not None

        with patch("blackvuesync_v2.sync.skip_metadata", set()):
            # should not raise
            download_recording(base_url, recording, str(tmp_path))

        assert (tmp_path / "20230101_120000_NF.mp4").exists()
