"""declarative field descriptors for the settings ui.

maps each settings section to an ordered list of editable fields and the widget
to render. secret fields (auth.password_hash / session_secret) are intentionally
absent -- the auth section renders dedicated password/rotate controls instead.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from blackvuesync_v2.settings import _SECTION_FIELDS


@dataclass(frozen=True)
class FieldSpec:
    """describes how one settings field is rendered and typed in the form.

    widget is one of: text, number, toggle, select, checkboxes, lines.
    options applies to select/checkboxes. data_type drives client-side json
    coercion: text, number, bool, lines (newline-split -> list), letters
    (checkbox group -> list).
    """

    name: str
    label: str
    widget: str
    data_type: str
    options: tuple[str, ...] = ()
    help: str = ""


# ordered, per section. excludes redacted secrets (handled by auth controls).
SECTION_FIELD_SPECS: dict[str, tuple[FieldSpec, ...]] = {
    "connection": (
        FieldSpec(
            "address",
            "Dashcam address",
            "text",
            "text",
            help="IP or hostname on the LAN",
        ),
        FieldSpec("timeout_seconds", "Timeout (seconds)", "number", "number"),
    ),
    "schedule": (
        FieldSpec(
            "cron_expression",
            "Schedule (cron)",
            "text",
            "text",
            help="5-field cron, e.g. */15 * * * *",
        ),
        FieldSpec("timezone", "Timezone", "text", "text"),
        FieldSpec("paused", "Pause scheduled syncs", "toggle", "bool"),
    ),
    "sync": (
        FieldSpec(
            "priority",
            "Download priority",
            "select",
            "text",
            options=("date", "rdate", "type"),
        ),
        FieldSpec(
            "grouping",
            "Grouping",
            "select",
            "text",
            options=("none", "daily", "weekly", "monthly", "yearly"),
        ),
        FieldSpec(
            "include",
            "Include recordings",
            "lines",
            "lines",
            help="one type or type+direction code per line, e.g. P or NF",
        ),
        FieldSpec(
            "exclude",
            "Exclude recordings",
            "lines",
            "lines",
            help="one type or type+direction code per line, e.g. P or NF",
        ),
        FieldSpec(
            "retry_failed_after",
            "Retry failed after",
            "text",
            "text",
            help="duration in s, h, d or w, e.g. 12h or 1d",
        ),
        FieldSpec(
            "skip_metadata",
            "Skip metadata",
            "checkboxes",
            "letters",
            options=("t", "3", "g"),
        ),
        FieldSpec(
            "affinity_key",
            "Affinity key",
            "text",
            "text",
            help="reserved for test isolation",
        ),
    ),
    "retention": (
        FieldSpec(
            "keep",
            "Keep recordings for",
            "text",
            "text",
            help="days or weeks, e.g. 30d or 2w; empty keeps forever",
        ),
        FieldSpec("max_used_disk_percent", "Max used disk (%)", "number", "number"),
    ),
    "logging": (
        FieldSpec("verbose", "Verbosity", "number", "number"),
        FieldSpec("quiet", "Quiet (errors only)", "toggle", "bool"),
        FieldSpec("format", "Log format", "select", "text", options=("text", "json")),
        FieldSpec("file_max_bytes", "Log file max bytes", "number", "number"),
        FieldSpec("file_backup_count", "Log file backups", "number", "number"),
        FieldSpec("ring_buffer_capacity", "Ring buffer capacity", "number", "number"),
    ),
    "metrics": (
        FieldSpec("file", "Metrics file", "text", "text"),
        FieldSpec("pushgateway_url", "Pushgateway URL", "text", "text"),
        FieldSpec("job", "Job name", "text", "text"),
        FieldSpec("instance", "Instance", "text", "text"),
        FieldSpec(
            "state_file",
            "State file",
            "text",
            "text",
            help="blank = metrics-state.json next to settings.json",
        ),
    ),
    "stats": (
        FieldSpec(
            "retention_days",
            "History retention (days)",
            "number",
            "number",
            help="prune run history older than this; 0 keeps all",
        ),
    ),
    "viewer": (
        FieldSpec(
            "journey_mode",
            "Journey accumulation",
            "select",
            "text",
            options=("progressive", "full"),
            help="progressive grows the map as you watch; full plots the whole route up front",
        ),
        FieldSpec("speed_unit", "Speed unit", "select", "text", options=("kmh", "mph")),
        FieldSpec(
            "continuous_play",
            "Continuous play",
            "toggle",
            "bool",
            help="plays every back-to-back segment in time order, whatever its type"
            " (normal, event, parking...); off plays only segments of the selected type",
        ),
    ),
    "camera": (
        FieldSpec(
            "read_timeout_seconds",
            "Camera read timeout (seconds)",
            "number",
            "number",
            help="how long the Camera panes wait before showing the last-known settings",
        ),
    ),
    "web": (
        FieldSpec("port", "Port", "number", "number"),
        FieldSpec(
            "session_lifetime_hours", "Session lifetime (hours)", "number", "number"
        ),
    ),
    "auth": (
        FieldSpec(
            "mode", "Auth mode", "select", "text", options=("login", "none", "proxy")
        ),
        FieldSpec("username", "Username", "text", "text"),
        FieldSpec(
            "trusted_proxies",
            "Trusted proxies",
            "lines",
            "lines",
            help="one IP/CIDR per line",
        ),
        FieldSpec("proxy_user_header", "Proxy user header", "text", "text"),
    ),
    "system": (
        FieldSpec(
            "destination", "Destination", "text", "text", help="recordings directory"
        ),
        FieldSpec("dry_run", "Dry run", "toggle", "bool"),
    ),
}

# human labels for the section nav.
SECTION_LABELS: dict[str, str] = {
    "connection": "Connection",
    "schedule": "Schedule",
    "sync": "Sync",
    "retention": "Retention",
    "logging": "Logging",
    "metrics": "Metrics",
    "stats": "Statistics",
    "viewer": "Viewer",
    "camera": "Camera access",
    "web": "Web",
    "auth": "Auth",
    "system": "System",
}


def _field_value(spec: FieldSpec, raw: Any) -> Any:
    """shapes a raw settings value for its widget.

    tuple-of-strings (lines) join with newlines for the textarea; None becomes
    an empty string for text widgets; everything else passes through.
    """
    if spec.widget == "lines":
        return "\n".join(raw or ())
    if raw is None and spec.widget in ("text", "number"):
        return ""
    return raw


def build_sections(settings_dict: dict[str, Any]) -> list[dict[str, Any]]:
    """builds the ordered render model for the settings page.

    pairs each section's field specs with the current (redacted) values from a
    GET /api/settings-shaped dict; carries the per-section tier and an is_auth
    flag (the template renders password/rotate controls for auth).
    """
    sections: list[dict[str, Any]] = []
    for name in _SECTION_FIELDS:  # preserves section order
        section_values = settings_dict.get(name, {})
        tier = section_values.get("_tier", "")
        fields = [
            {
                "name": spec.name,
                "label": spec.label,
                "widget": spec.widget,
                "data_type": spec.data_type,
                "options": spec.options,
                "help": spec.help,
                "value": _field_value(spec, section_values.get(spec.name)),
            }
            for spec in SECTION_FIELD_SPECS[name]
        ]
        sections.append(
            {
                "name": name,
                "label": SECTION_LABELS[name],
                "tier": tier,
                "fields": fields,
                "is_auth": name == "auth",
            }
        )
    return sections


__all__ = ["FieldSpec", "SECTION_FIELD_SPECS", "SECTION_LABELS", "build_sections"]
