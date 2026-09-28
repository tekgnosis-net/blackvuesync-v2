"""api dashcam routes: read-only inspection of on-camera config.

fetches and parses /Config/version.bin and /Config/config.ini from the
dashcam over http (blackvue firmware is http-only). all writes (changing
settings) are deliberately out of scope; that is a future sub-project.
"""

from __future__ import annotations

import configparser
import json
import urllib.request

from flask import Blueprint, Response, current_app

from blackvuesync_v2.server.auth import login_required
from blackvuesync_v2.settings import SettingsStore

api_dashcam_bp = Blueprint("api_dashcam_bp", __name__, url_prefix="/api/dashcam")

_MIME_JSON = "application/json"

# default per-file timeout for the two config fetches; deliberately short so a
# slow or offline dashcam does not stall the dashboard card.
_FETCH_TIMEOUT = 2.0

# how many flattened config entries the card preview surfaces.
_PREVIEW_LIMIT = 8

# dict key reused across the structural-availability return shapes.
_KEY_AVAILABLE = "available"


def _fetch_text(url: str, timeout: float) -> str | None:
    """GETs url and returns its decoded body, or None on any failure.

    decodes with errors='replace' because version.bin is a binary-ish blob;
    callers clean it further. blackvue firmware is http-only (no https variant
    exists).
    """
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body: bytes = resp.read()
            return body.decode("utf-8", errors="replace")
    except OSError:
        return None


def _parse_version_bin(text: str) -> str:
    """extracts a clean firmware/model string from version.bin content.

    keeps only printable characters and whitespace, then strips; the raw file
    can carry trailing nulls or control bytes.
    """
    cleaned = "".join(c for c in text if c.isprintable() or c == " ")
    return cleaned.strip()


def _parse_config_ini(text: str) -> dict[str, dict[str, str]]:
    """parses config.ini text into a {section: {key: value}} dict.

    uses a permissive parser (strict=False, no interpolation). legacy firmware
    may omit a leading section header; if so the text is retried under a
    synthetic [General] section so header-less keys are still captured.
    returns an empty dict if parsing fails entirely.
    """
    parser = configparser.ConfigParser(strict=False, interpolation=None)
    # preserves original key casing; firmware keys are CamelCase (e.g. Voice).
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read_string(text)
    except configparser.MissingSectionHeaderError:
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.optionxform = str  # type: ignore[assignment,method-assign]
        try:
            parser.read_string("[General]\n" + text)
        except configparser.Error:
            return {}
    except configparser.Error:
        return {}
    return {section: dict(parser.items(section)) for section in parser.sections()}


def _config_preview(
    config: dict[str, dict[str, str]], limit: int = _PREVIEW_LIMIT
) -> list[tuple[str, str]]:
    """flattens config to up to `limit` (section.key, value) pairs for display."""
    entries: list[tuple[str, str]] = []
    for section, keys in config.items():
        for key, value in keys.items():
            entries.append((f"{section}.{key}", value))
            if len(entries) >= limit:
                return entries
    return entries


def _compute_dashcam_info(
    address: str, timeout: float = _FETCH_TIMEOUT
) -> dict[str, object]:
    """fetches and parses the dashcam's version.bin + config.ini.

    factored out so /api/dashcam/info and /hx/dashcam-info-card share the same
    computation. returns {available: False, reason: ...} when no address is
    configured or both files are unreachable; otherwise returns the parsed
    firmware string and config dict (either may be partial).
    """
    if not address:
        return {_KEY_AVAILABLE: False, "reason": "no address configured"}

    # blackvue firmware is http-only (no https).
    version_url = f"http://{address}/Config/version.bin"
    config_url = f"http://{address}/Config/config.ini"
    firmware_raw = _fetch_text(version_url, timeout)
    config_raw = _fetch_text(config_url, timeout)

    if firmware_raw is None and config_raw is None:
        return {_KEY_AVAILABLE: False, "reason": "dashcam unreachable"}

    config = _parse_config_ini(config_raw) if config_raw else {}
    return {
        _KEY_AVAILABLE: True,
        "address": address,
        "firmware": _parse_version_bin(firmware_raw) if firmware_raw else None,
        "config": config,
        "setting_count": sum(len(keys) for keys in config.values()),
    }


@api_dashcam_bp.route("/info", methods=["GET"])
@login_required
def info() -> Response:
    """returns read-only dashcam firmware + config information."""
    store: SettingsStore = current_app.settings_store  # type: ignore[attr-defined]
    address = store.get().connection.address
    body = json.dumps(_compute_dashcam_info(address))
    return Response(body, status=200, mimetype=_MIME_JSON)


__all__ = ["api_dashcam_bp"]
