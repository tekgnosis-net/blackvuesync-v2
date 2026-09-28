"""tests for the settings-form field descriptors and context builder."""

from __future__ import annotations

from typing import Any

from blackvuesync_v2.server.settings_form import (
    SECTION_FIELD_SPECS,
    build_sections,
)
from blackvuesync_v2.settings import _REDACTED_FIELDS, _SECTION_FIELDS


def test_every_section_has_specs() -> None:
    # one spec list per settings section, same names as the backend
    assert set(SECTION_FIELD_SPECS) == set(_SECTION_FIELDS)


def test_redacted_secrets_are_not_rendered_as_fields() -> None:
    # password_hash / session_secret must not appear as editable fields
    auth_field_names = {f.name for f in SECTION_FIELD_SPECS["auth"]}
    assert auth_field_names.isdisjoint(_REDACTED_FIELDS["auth"])


def test_build_sections_pairs_values_and_tier() -> None:
    settings_dict: dict[str, Any] = {
        "connection": {
            "address": "192.168.0.1",
            "timeout_seconds": 10.0,
            "_tier": "restart",
        },
        "schedule": {
            "cron_expression": "*/15 * * * *",
            "timezone": "UTC",
            "paused": False,
            "_tier": "next_tick",
        },
        "sync": {
            "priority": "date",
            "grouping": "none",
            "include": [],
            "exclude": [],
            "retry_failed_after": "1d",
            "skip_metadata": [],
            "affinity_key": None,
            "_tier": "next_tick",
        },
        "retention": {"keep": "2w", "max_used_disk_percent": 90, "_tier": "next_tick"},
        "logging": {
            "verbose": 0,
            "quiet": False,
            "format": "text",
            "file_max_bytes": 1024,
            "file_backup_count": 5,
            "ring_buffer_capacity": 1000,
            "_tier": "immediate",
        },
        "metrics": {
            "file": None,
            "pushgateway_url": None,
            "job": "blackvuesync",
            "instance": None,
            "state_file": "/config/metrics-state.json",
            "_tier": "immediate",
        },
        "web": {"port": 8080, "session_lifetime_hours": 24, "_tier": "restart"},
        "auth": {
            "mode": "login",
            "username": "admin",
            "password_hash": "***",
            "session_secret": "***",
            "trusted_proxies": [],
            "proxy_user_header": "X-Remote-User",
            "_tier": "immediate",
        },
        "stats": {"retention_days": 365, "_tier": "next_tick"},
        "viewer": {
            "journey_mode": "progressive",
            "speed_unit": "kmh",
            "_tier": "immediate",
        },
        "system": {"destination": "/recordings", "dry_run": False, "_tier": "restart"},
    }
    sections = build_sections(settings_dict)
    by_name = {s["name"]: s for s in sections}
    assert by_name["connection"]["tier"] == "restart"
    addr = next(f for f in by_name["connection"]["fields"] if f["name"] == "address")
    assert addr["value"] == "192.168.0.1"
    assert addr["widget"] == "text"
    # auth section flagged so the template renders the password/rotate controls
    assert by_name["auth"]["is_auth"] is True


def test_lines_widget_value_is_joined() -> None:
    settings_dict: dict[str, Any] = {
        name: {"_tier": "immediate"} for name in _SECTION_FIELDS
    }
    settings_dict["sync"].update(
        {
            "include": ["a*", "b*"],
            "exclude": [],
            "priority": "date",
            "grouping": "none",
            "retry_failed_after": "1d",
            "skip_metadata": [],
            "affinity_key": None,
        }
    )
    sections = {s["name"]: s for s in build_sections(settings_dict)}
    inc = next(f for f in sections["sync"]["fields"] if f["name"] == "include")
    assert inc["widget"] == "lines"
    assert inc["value"] == "a*\nb*"  # tuple-of-strings joined for the textarea


def test_stats_section_has_retention_field() -> None:
    from blackvuesync_v2.server.settings_form import SECTION_FIELD_SPECS, SECTION_LABELS

    assert SECTION_LABELS["stats"] == "Statistics"
    names = [f.name for f in SECTION_FIELD_SPECS["stats"]]
    assert names == ["retention_days"]
    spec = SECTION_FIELD_SPECS["stats"][0]
    assert spec.widget == "number" and spec.data_type == "number"


def test_viewer_section_has_two_select_fields() -> None:
    from blackvuesync_v2.server.settings_form import SECTION_FIELD_SPECS, SECTION_LABELS

    assert SECTION_LABELS["viewer"] == "Viewer"
    specs = {f.name: f for f in SECTION_FIELD_SPECS["viewer"]}
    assert specs["journey_mode"].widget == "select"
    assert specs["journey_mode"].options == ("progressive", "full")
    assert specs["speed_unit"].options == ("kmh", "mph")
