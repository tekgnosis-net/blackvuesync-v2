"""settings schema, validation, atomic persistence, and env-var bootstrap."""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import ipaddress
import json
import logging
import os
import re
import secrets
import stat
import threading
import zoneinfo
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, ClassVar, Literal

from blackvuesync_v2.sync import calc_cutoff_date, parse_duration, parse_filter

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

# the metrics state file default before schema 2, when it was not yet
# resolved relative to the settings file.
_LEGACY_METRICS_STATE_FILE = "/config/metrics-state.json"

PropagationTier = Literal["immediate", "next_tick", "restart"]

# default schedule; also the scheduler's fallback when the stored one is unusable
DEFAULT_CRON_EXPRESSION = "*/15 * * * *"
DEFAULT_TIMEZONE = "UTC"

# valid skip-metadata type codes (mirrors sync.VALID_METADATA_TYPES)
_VALID_SKIP_METADATA = frozenset(("t", "3", "g"))

# valid Literal field values for member-check validation
_VALID_PRIORITIES = frozenset(("date", "rdate", "type"))
_VALID_GROUPINGS = frozenset(("none", "daily", "weekly", "monthly", "yearly"))
_VALID_LOG_FORMATS = frozenset(("text", "json"))
_VALID_AUTH_MODES = frozenset(("login", "none", "proxy"))


# ---------------------------------------------------------------------------
# cron and timezone validation
# ---------------------------------------------------------------------------

_MONTH_NAMES = (
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
)
# standard cron numbering: 0 (and 7) is sunday; also apscheduler's names
_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")

# (label, min, max, names, value of the first name) per cron field
_CRON_FIELD_SPECS: tuple[tuple[str, int, int, tuple[str, ...], int], ...] = (
    ("minute", 0, 59, (), 0),
    ("hour", 0, 23, (), 0),
    ("day of month", 1, 31, (), 0),
    ("month", 1, 12, _MONTH_NAMES, 1),
    ("day of week", 0, 7, _DOW_NAMES, 0),
)

_CRON_ITEM_RE = re.compile(
    r"^(?P<base>\*|[0-9a-z]+(?:-[0-9a-z]+)?)(?:/(?P<step>\d+))?$"
)


def _cron_value(
    token: str, lo: int, hi: int, names: tuple[str, ...], first: int
) -> int:
    """converts a cron value (number or name) to an int within [lo, hi]."""
    if token.isdigit():
        value = int(token)
    elif token in names:
        value = names.index(token) + first
    else:
        raise ValueError(f"unknown value {token!r}")
    if not lo <= value <= hi:
        raise ValueError(f"value {value} is outside {lo}-{hi}")
    return value


def _expand_cron_field(
    text: str, lo: int, hi: int, names: tuple[str, ...], first: int
) -> set[int]:
    """expands one cron field (lists, ranges, steps, names) to its values."""
    values: set[int] = set()
    for item in text.lower().split(","):
        match = _CRON_ITEM_RE.match(item)
        if match is None:
            raise ValueError(f"malformed item {item!r}")
        base, step_raw = match.group("base"), match.group("step")
        step = int(step_raw) if step_raw is not None else 1
        if step < 1:
            raise ValueError(f"step in {item!r} must be greater than zero")
        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            low_token, high_token = base.split("-")
            start = _cron_value(low_token, lo, hi, names, first)
            end = _cron_value(high_token, lo, hi, names, first)
            if start > end:
                raise ValueError(f"range {base!r} is reversed")
        else:
            start = _cron_value(base, lo, hi, names, first)
            # "5/10" means "5-max/10", as in vixie cron and apscheduler
            end = hi if step_raw is not None else start
        values.update(range(start, end + 1, step))
    return values


def _parse_cron(expression: str) -> list[set[int]]:
    """parses a standard 5-field cron expression; raises ValueError if invalid."""
    parts = expression.split()
    if len(parts) != len(_CRON_FIELD_SPECS):
        raise ValueError(f"expected 5 fields, got {len(parts)}")
    expanded: list[set[int]] = []
    for text, (label, lo, hi, names, first) in zip(parts, _CRON_FIELD_SPECS):
        try:
            expanded.append(_expand_cron_field(text, lo, hi, names, first))
        except ValueError as e:
            raise ValueError(f"{label} field {text!r}: {e}") from e
    return expanded


def cron_trigger_fields(expression: str) -> list[dict[str, str]]:
    """translates a standard cron expression to apscheduler CronTrigger kwargs.

    apscheduler numbers days of the week from monday (0) while cron numbers
    them from sunday (0 or 7), so the day of week is emitted as names. when
    both day of month and day of week are restricted, cron fires when either
    matches, so two kwarg sets are returned for the caller to OR together.
    raises ValueError if the expression is invalid.
    """
    expanded = _parse_cron(expression)
    minute, hour, day, month, dow = expression.split()
    days_of_week = sorted({d % 7 for d in expanded[4]})
    dow_field = (
        "*"
        if len(days_of_week) == len(_DOW_NAMES)
        else ",".join(_DOW_NAMES[d] for d in days_of_week)
    )
    month_field = re.sub(
        "[a-z]+",
        lambda m: str(_MONTH_NAMES.index(m.group()) + 1),
        month.lower(),
    )
    base = {"minute": minute, "hour": hour, "month": month_field}
    if not day.startswith("*") and not dow.startswith("*"):
        return [
            {**base, "day": day, "day_of_week": "*"},
            {**base, "day": "*", "day_of_week": dow_field},
        ]
    return [{**base, "day": day, "day_of_week": dow_field}]


def _cron_error(expression: str) -> str | None:
    """returns why expression is not a valid cron expression, or None."""
    try:
        _parse_cron(expression)
    except ValueError as e:
        return str(e)
    return None


def _timezone_error(timezone: str) -> str | None:
    """returns why timezone is not a known IANA timezone, or None."""
    if not timezone:
        return "must not be empty"
    try:
        zoneinfo.ZoneInfo(timezone)
    except (ValueError, zoneinfo.ZoneInfoNotFoundError):
        return f"unknown timezone {timezone!r}"
    return None


def _scheduler_error(expression: str, timezone: str) -> str | None:
    """returns why apscheduler rejects the schedule, or None.

    skipped when apscheduler is not installed (the cli sync path).
    """
    try:
        # pylint: disable-next=import-outside-toplevel
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        return None
    try:
        for kwargs in cron_trigger_fields(expression):
            CronTrigger(timezone=timezone, **kwargs)
    except Exception as e:  # pylint: disable=broad-exception-caught
        return str(e)
    return None


# ---------------------------------------------------------------------------
# field type checking
# ---------------------------------------------------------------------------


def _split_top_level(annotation: str, separator: str) -> list[str]:
    """splits annotation on separator, ignoring separators inside brackets."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in annotation:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == separator and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    parts.append(current.strip())
    return parts


def _literal_values(annotation: str) -> tuple[Any, ...]:
    """returns the values of a Literal[...] annotation string."""
    return tuple(ast.literal_eval(f"({annotation[len('Literal[') : -1]},)"))


def _type_matches(  # pylint: disable=too-many-return-statements
    annotation: str, value: Any
) -> bool:
    """returns True if value conforms to the annotation string.

    Literal annotations check only the base type; member checks stay in the
    section validators so their error messages are specific.
    """
    options = _split_top_level(annotation, "|")
    if len(options) > 1:
        return any(_type_matches(option, value) for option in options)
    annotation = options[0]
    if annotation == "None":
        return value is None
    if annotation == "bool":
        return isinstance(value, bool)
    if annotation == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if annotation == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if annotation == "str":
        return isinstance(value, str)
    if annotation.startswith("Literal["):
        return any(type(value) is type(v) for v in _literal_values(annotation))
    if annotation.startswith("tuple[") and annotation.endswith(", ...]"):
        item = annotation[len("tuple[") : -len(", ...]")]
        return isinstance(value, (tuple, list)) and all(
            _type_matches(item, v) for v in value
        )
    raise TypeError(f"unsupported settings annotation: {annotation!r}")


def _describe_type(annotation: str) -> str:
    """returns a human description of an annotation string."""
    options = _split_top_level(annotation, "|")
    if len(options) > 1:
        return " or ".join(_describe_type(option) for option in options)
    annotation = options[0]
    if annotation.startswith("tuple["):
        item = _describe_type(annotation[len("tuple[") : -len(", ...]")])
        return f"a list of {item.split(' ', 1)[1]}s"
    if annotation.startswith("Literal["):
        return _describe_type(type(_literal_values(annotation)[0]).__name__)
    return {
        "None": "null",
        "bool": "a boolean",
        "int": "an integer",
        "float": "a number",
        "str": "a string",
    }.get(annotation, annotation)


class _Section:  # pylint: disable=too-few-public-methods
    """base for section dataclasses: type-checks fields before value checks."""

    def validate(self) -> list[str]:
        """validates field types, then values; returns a list of error strings."""
        prefix = _SECTION_NAMES.get(type(self), type(self).__name__)
        errors = [
            f"{prefix}.{f.name} must be {_describe_type(str(f.type))}, "
            f"got {type(getattr(self, f.name)).__name__}"
            for f in fields(self)  # type: ignore[arg-type]
            if not _type_matches(str(f.type), getattr(self, f.name))
        ]
        # value checks assume correct types, so they only run when types pass
        return errors or self._validate_values()

    def _validate_values(self) -> list[str]:
        """validates field values; returns a list of error strings."""
        return []


# ---------------------------------------------------------------------------
# section dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConnectionSettings(_Section):
    """connection settings for the dashcam."""

    TIER: ClassVar[PropagationTier] = "restart"

    # empty until configured; the sync path reports a missing address
    address: str = ""
    timeout_seconds: float = 10.0

    def _validate_values(self) -> list[str]:
        """validates connection settings; returns a list of error strings."""
        errors: list[str] = []
        if self.timeout_seconds <= 0:
            errors.append("connection.timeout_seconds must be greater than zero")
        return errors


@dataclass(frozen=True)
class ScheduleSettings(_Section):
    """sync schedule settings."""

    TIER: ClassVar[PropagationTier] = "next_tick"

    cron_expression: str = DEFAULT_CRON_EXPRESSION
    timezone: str = DEFAULT_TIMEZONE
    paused: bool = False

    def _validate_values(self) -> list[str]:
        """validates schedule settings; returns a list of error strings."""
        errors: list[str] = []
        cron_error = _cron_error(self.cron_expression)
        if cron_error is not None:
            errors.append(
                f"schedule.cron_expression is not a valid 5-field cron expression "
                f"({cron_error}): {self.cron_expression!r}"
            )
        timezone_error = _timezone_error(self.timezone)
        if timezone_error is not None:
            errors.append(f"schedule.timezone is invalid: {timezone_error}")
        if not errors:
            scheduler_error = _scheduler_error(self.cron_expression, self.timezone)
            if scheduler_error is not None:
                errors.append(
                    f"schedule is rejected by the scheduler: {scheduler_error}"
                )
        return errors


@dataclass(frozen=True)
class SyncSettings(_Section):
    """recording sync settings."""

    TIER: ClassVar[PropagationTier] = "next_tick"

    priority: Literal["date", "rdate", "type"] = "date"
    grouping: Literal["none", "daily", "weekly", "monthly", "yearly"] = "none"
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    retry_failed_after: str = "1d"
    skip_metadata: tuple[Literal["t", "3", "g"], ...] = ()
    affinity_key: str | None = None

    def _validate_values(self) -> list[str]:
        """validates sync settings; returns a list of error strings."""
        errors: list[str] = []
        if self.priority not in _VALID_PRIORITIES:
            errors.append(
                f"sync.priority must be one of {sorted(_VALID_PRIORITIES)!r}, "
                f"got {self.priority!r}"
            )
        if self.grouping not in _VALID_GROUPINGS:
            errors.append(
                f"sync.grouping must be one of {sorted(_VALID_GROUPINGS)!r}, "
                f"got {self.grouping!r}"
            )
        for name in ("include", "exclude"):
            for code in getattr(self, name):
                try:
                    valid = parse_filter(code) == (code,)
                    reason = "must be a single code, e.g. P or NF"
                except argparse.ArgumentTypeError as e:
                    valid, reason = False, str(e)
                if not valid:
                    errors.append(f"sync.{name} entry {code!r} is invalid: {reason}")
        try:
            parse_duration(self.retry_failed_after, label="sync.retry_failed_after")
        except (RuntimeError, OverflowError) as e:
            errors.append(
                f"sync.retry_failed_after is not a valid duration: {e} "
                f"(got {self.retry_failed_after!r})"
            )
        # one letter per entry; the sync path matches entries individually
        invalid_meta = {m for m in self.skip_metadata if m not in _VALID_SKIP_METADATA}
        if invalid_meta:
            errors.append(
                f"sync.skip_metadata contains invalid tokens: {sorted(invalid_meta)!r}"
            )
        return errors


@dataclass(frozen=True)
class RetentionSettings(_Section):
    """recording retention settings."""

    TIER: ClassVar[PropagationTier] = "next_tick"

    # empty keeps recordings forever
    keep: str = "2w"
    max_used_disk_percent: int = 90

    def _validate_values(self) -> list[str]:
        """validates retention settings; returns a list of error strings."""
        errors: list[str] = []
        if self.keep:
            try:
                calc_cutoff_date(self.keep)
            except (RuntimeError, OverflowError) as e:
                errors.append(
                    f"retention.keep is not a valid duration: {e} (got {self.keep!r})"
                )
        if not 1 <= self.max_used_disk_percent <= 100:
            errors.append("retention.max_used_disk_percent must be between 1 and 100")
        return errors


@dataclass(frozen=True)
class LoggingSettings(_Section):
    """logging output settings."""

    TIER: ClassVar[PropagationTier] = "immediate"

    verbose: int = 0
    quiet: bool = False
    format: Literal["text", "json"] = "text"
    file_max_bytes: int = 10 * 1024 * 1024
    file_backup_count: int = 5
    ring_buffer_capacity: int = 1000

    def _validate_values(self) -> list[str]:
        """validates logging settings; returns a list of error strings."""
        errors: list[str] = []
        if self.format not in _VALID_LOG_FORMATS:
            errors.append(
                f"logging.format must be one of {sorted(_VALID_LOG_FORMATS)!r}, "
                f"got {self.format!r}"
            )
        if self.verbose < 0:
            errors.append("logging.verbose must be >= 0")
        if self.file_max_bytes <= 0:
            errors.append("logging.file_max_bytes must be greater than zero")
        if self.file_backup_count < 0:
            errors.append("logging.file_backup_count must be >= 0")
        if self.ring_buffer_capacity <= 0:
            errors.append("logging.ring_buffer_capacity must be greater than zero")
        return errors


@dataclass(frozen=True)
class MetricsSettings(_Section):
    """prometheus metrics export settings."""

    TIER: ClassVar[PropagationTier] = "immediate"

    file: str | None = None
    pushgateway_url: str | None = None
    job: str = "blackvuesync"
    instance: str | None = None
    # empty means metrics-state.json next to settings.json; only used while
    # metrics are enabled (file or pushgateway_url set).
    state_file: str = ""


@dataclass(frozen=True)
class StatsSettings(_Section):
    """statistics time-series store settings."""

    TIER: ClassVar[PropagationTier] = "next_tick"

    retention_days: int = 365  # prune run records older than this; 0 keeps all

    def _validate_values(self) -> list[str]:
        """validates stats settings; returns a list of error strings."""
        errors: list[str] = []
        if self.retention_days < 0:
            errors.append("stats.retention_days must be zero or greater")
        return errors


@dataclass(frozen=True)
class ViewerSettings(_Section):
    """dashcam viewer settings."""

    TIER: ClassVar[PropagationTier] = "immediate"

    journey_mode: Literal["progressive", "full"] = "progressive"
    speed_unit: Literal["kmh", "mph"] = "kmh"

    def _validate_values(self) -> list[str]:
        """validates viewer settings; returns a list of error strings."""
        errors: list[str] = []
        if self.journey_mode not in ("progressive", "full"):
            errors.append("viewer.journey_mode must be 'progressive' or 'full'")
        if self.speed_unit not in ("kmh", "mph"):
            errors.append("viewer.speed_unit must be 'kmh' or 'mph'")
        return errors


@dataclass(frozen=True)
class WebSettings(_Section):
    """web server settings."""

    TIER: ClassVar[PropagationTier] = "restart"

    port: int = 8080
    session_lifetime_hours: int = 24

    def _validate_values(self) -> list[str]:
        """validates web settings; returns a list of error strings."""
        errors: list[str] = []
        if not 1 <= self.port <= 65535:
            errors.append("web.port must be between 1 and 65535")
        if self.session_lifetime_hours <= 0:
            errors.append("web.session_lifetime_hours must be greater than zero")
        return errors


@dataclass(frozen=True)
class AuthSettings(_Section):
    """authentication settings."""

    TIER: ClassVar[PropagationTier] = "immediate"

    mode: Literal["login", "none", "proxy"] = "login"
    username: str = "admin"
    password_hash: str = ""
    # SettingsStore generates one on load when empty and refuses to store ""
    session_secret: str = ""
    trusted_proxies: tuple[str, ...] = ()
    proxy_user_header: str = "X-Remote-User"

    def _validate_values(self) -> list[str]:
        """validates auth settings; returns a list of error strings."""
        errors: list[str] = []
        if self.mode not in _VALID_AUTH_MODES:
            errors.append(
                f"auth.mode must be one of {sorted(_VALID_AUTH_MODES)!r}, "
                f"got {self.mode!r}"
            )
        for proxy in self.trusted_proxies:
            try:
                ipaddress.ip_network(proxy, strict=False)
            except ValueError:
                errors.append(
                    f"auth.trusted_proxies entry {proxy!r} is not an IP address or network"
                )
        if self.mode == "proxy":
            if not self.trusted_proxies:
                errors.append(
                    "auth.trusted_proxies must not be empty when mode is 'proxy'"
                )
            if not self.proxy_user_header:
                errors.append(
                    "auth.proxy_user_header must not be empty when mode is 'proxy'"
                )
        return errors


@dataclass(frozen=True)
class SystemSettings(_Section):
    """system-level settings."""

    TIER: ClassVar[PropagationTier] = "restart"

    destination: str = "/recordings"
    dry_run: bool = False

    def _validate_values(self) -> list[str]:
        """validates system settings; returns a list of error strings."""
        errors: list[str] = []
        if not self.destination:
            errors.append("system.destination must not be empty")
        return errors


# ---------------------------------------------------------------------------
# top-level settings
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:  # pylint: disable=too-many-instance-attributes
    """top-level settings container."""

    version: int = SCHEMA_VERSION
    connection: ConnectionSettings = field(default_factory=ConnectionSettings)
    schedule: ScheduleSettings = field(default_factory=ScheduleSettings)
    sync: SyncSettings = field(default_factory=SyncSettings)
    retention: RetentionSettings = field(default_factory=RetentionSettings)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    metrics: MetricsSettings = field(default_factory=MetricsSettings)
    stats: StatsSettings = field(default_factory=StatsSettings)
    viewer: ViewerSettings = field(default_factory=ViewerSettings)
    web: WebSettings = field(default_factory=WebSettings)
    auth: AuthSettings = field(default_factory=AuthSettings)
    system: SystemSettings = field(default_factory=SystemSettings)

    def validate(self) -> list[str]:
        """validates all sections; returns aggregated list of error strings."""
        errors: list[str] = []
        errors.extend(self.connection.validate())
        errors.extend(self.schedule.validate())
        errors.extend(self.sync.validate())
        errors.extend(self.retention.validate())
        errors.extend(self.logging.validate())
        errors.extend(self.metrics.validate())
        errors.extend(self.stats.validate())
        errors.extend(self.viewer.validate())
        errors.extend(self.web.validate())
        errors.extend(self.auth.validate())
        errors.extend(self.system.validate())
        return errors


# ---------------------------------------------------------------------------
# serialization helpers
# ---------------------------------------------------------------------------


_SECTION_FIELDS: dict[str, type] = {
    "connection": ConnectionSettings,
    "schedule": ScheduleSettings,
    "sync": SyncSettings,
    "retention": RetentionSettings,
    "logging": LoggingSettings,
    "metrics": MetricsSettings,
    "stats": StatsSettings,
    "viewer": ViewerSettings,
    "web": WebSettings,
    "auth": AuthSettings,
    "system": SystemSettings,
}

_SECTION_NAMES: dict[type, str] = {cls: name for name, cls in _SECTION_FIELDS.items()}

# fields whose values are tuple[str, ...] and must be round-tripped as lists
_TUPLE_FIELDS: dict[str, set[str]] = {
    "sync": {"include", "exclude", "skip_metadata"},
    "auth": {"trusted_proxies"},
}

# fields that must never be sent to clients; the redaction sentinel "***" is
# returned instead and stripped again on inbound patches.
_REDACTED_FIELDS: dict[str, set[str]] = {
    "auth": {"password_hash", "session_secret"},
}


def _section_to_dict(section: object) -> dict[str, Any]:
    """converts a frozen section dataclass to a JSON-serializable dict."""
    result: dict[str, Any] = {}
    for f in fields(section):  # type: ignore[arg-type]
        value = getattr(section, f.name)
        if isinstance(value, tuple):
            result[f.name] = list(value)
        else:
            result[f.name] = value
    return result


def _settings_to_dict(settings: Settings) -> dict[str, Any]:
    """converts a Settings object to a JSON-serializable dict."""
    result: dict[str, Any] = {"version": settings.version}
    for section_name in _SECTION_FIELDS:
        result[section_name] = _section_to_dict(getattr(settings, section_name))
    return result


def _section_from_dict(cls: type, raw: dict[str, Any], tuple_fields: set[str]) -> Any:
    """constructs a frozen section dataclass from a dict, restoring tuples.

    a value of the wrong type (e.g. a hand-edited file) falls back to the
    field default rather than reaching code that assumes the declared type.
    """
    kwargs: dict[str, Any] = {}
    annotations = {f.name: str(f.type) for f in fields(cls)}
    for key, value in raw.items():
        if key not in annotations:
            continue
        if key in tuple_fields and isinstance(value, list):
            value = tuple(value)
        if not _type_matches(annotations[key], value):
            logger.warning(
                "settings field %s.%s must be %s; using the default",
                _SECTION_NAMES.get(cls, cls.__name__),
                key,
                _describe_type(annotations[key]),
            )
            continue
        kwargs[key] = value
    return cls(**kwargs)


def _settings_from_dict(raw: dict[str, Any]) -> Settings:
    """constructs a Settings object from a raw dict."""
    kwargs: dict[str, Any] = {"version": raw.get("version", SCHEMA_VERSION)}
    for section_name, section_cls in _SECTION_FIELDS.items():
        section_raw = raw.get(section_name, {})
        tuple_fields = _TUPLE_FIELDS.get(section_name, set())
        kwargs[section_name] = _section_from_dict(
            section_cls, section_raw, tuple_fields
        )
    return Settings(**kwargs)


# ---------------------------------------------------------------------------
# schema migration
# ---------------------------------------------------------------------------


def migrate(raw: dict[str, Any], from_version: int) -> dict[str, Any]:
    """migrates a raw settings dict from an older schema version to current.

    each step upgrades one version; steps run in order.
    """
    raw = dict(raw)
    if from_version < 1:
        raw["version"] = 1
    if from_version < 2:
        # 2: the metrics state file default became "next to settings.json";
        # the old literal default is equivalent inside the docker image.
        metrics = dict(raw.get("metrics") or {})
        if metrics.get("state_file") == _LEGACY_METRICS_STATE_FILE:
            metrics["state_file"] = ""
            raw["metrics"] = metrics
        raw["version"] = 2
    return raw


# ---------------------------------------------------------------------------
# SettingsStore
# ---------------------------------------------------------------------------


def _split_codes(raw: str) -> tuple[str, ...]:
    """splits a comma-separated env var into stripped, non-empty entries."""
    return tuple(code.strip() for code in raw.split(",") if code.strip())


def _hash_admin_password(password: str) -> str:
    """hashes the bootstrap admin password; returns "" when it cannot be used.

    the server's argon2 helper is imported lazily so the cli sync path keeps
    working without argon2-cffi and flask installed.
    """
    try:
        # pylint: disable-next=import-outside-toplevel
        from blackvuesync_v2.server.auth import MIN_PASSWORD_LENGTH, hash_password
    except ImportError as e:
        logger.error(
            "BLACKVUESYNC_ADMIN_PASSWORD ignored: password hashing unavailable (%s); "
            "set the password through the first-run page",
            e,
        )
        return ""
    if len(password) < MIN_PASSWORD_LENGTH:
        logger.error(
            "BLACKVUESYNC_ADMIN_PASSWORD ignored: shorter than %d characters; "
            "set the password through the first-run page",
            MIN_PASSWORD_LENGTH,
        )
        return ""
    logger.info("admin password set from BLACKVUESYNC_ADMIN_PASSWORD")
    return hash_password(password)


class ValidationError(Exception):
    """raised when Settings.validate() returns errors."""

    def __init__(self, errors: list[str]) -> None:
        """initializes with a list of validation error messages."""
        self.errors = errors
        super().__init__("; ".join(errors))


class SettingsStore:
    """thread-safe settings store with atomic persistence and change notifications."""

    def __init__(self, path: Path) -> None:
        """initializes the store and loads or bootstraps settings from path."""
        self._path = path
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Settings, Settings], None]] = []
        self._settings = self._load_or_bootstrap()

    @property
    def path(self) -> Path:
        """returns the settings file path."""
        return self._path

    def get(self) -> Settings:
        """returns the current settings snapshot."""
        with self._lock:
            return self._settings

    def update(self, mutation: Callable[[Settings], Settings]) -> Settings:
        """applies mutation, validates, persists atomically, and notifies listeners.

        only the sections the mutation changes are validated, so a section
        loaded with an invalid value (logged at load time) does not block
        edits to unrelated sections.
        """
        with self._lock:
            old = self._settings
            new = mutation(old)
            errors = [
                error
                for name in _SECTION_FIELDS
                if getattr(new, name) != getattr(old, name)
                for error in getattr(new, name).validate()
            ]
            if not new.auth.session_secret:
                errors.append("auth.session_secret must not be empty")
            if errors:
                raise ValidationError(errors)
            self._save(new)
            self._settings = new
        # snapshots the listeners list so a callback that registers a new
        # listener (via on_change()) cannot mutate the list mid-iteration.
        for listener in list(self._listeners):
            try:
                listener(old, new)
            except Exception:  # pylint: disable=broad-exception-caught
                logger.exception("settings change listener raised an exception")
        return new

    def on_change(self, listener: Callable[[Settings, Settings], None]) -> None:
        """registers a listener called after each successful update."""
        with self._lock:
            self._listeners.append(listener)

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------

    def _save(self, settings: Settings) -> None:
        """writes settings atomically to the store's path with 0600 perms."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        raw = _settings_to_dict(settings)
        try:
            fd = os.open(
                str(tmp_path),
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(raw, f, indent=2)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            # best-effort cleanup of partial temp file; failures are non-fatal
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, self._path)
        # fsync the directory to make the rename durable
        dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _load(self) -> Settings:
        """reads and parses settings from the store's path."""
        # checks file permissions before reading sensitive settings
        file_stat = os.stat(self._path)
        if file_stat.st_mode & 0o077:
            raise PermissionError(
                f"settings file {self._path} has insecure permissions "
                f"(mode {oct(stat.S_IMODE(file_stat.st_mode))}); "
                f"expected 0o600 or stricter"
            )
        with open(self._path, encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
        version = raw.get("version", 1)
        if version < SCHEMA_VERSION:
            raw = migrate(raw, version)
        settings = _settings_from_dict(raw)
        for error in settings.validate():
            logger.warning("settings validation error: %s", error)
        return settings

    def _bootstrap_from_env(self) -> Settings:  # pylint: disable=too-many-locals
        """builds initial settings from environment variables on first run."""
        env = os.environ

        # treats empty string as absent; mirrors blackvuesync.sh's
        # [ -n "${X:-}" ] check so Dockerfile ENV defaults like X="" do not
        # produce parse errors (e.g., int("") -> ValueError).
        def _env(key: str, default: str) -> str:
            val = env.get(key, "")
            return val if val else default

        # warns about retired env vars
        if env.get("CRON"):
            logger.warning(
                "env var CRON is ignored; the new service is always-on "
                "and uses settings.schedule"
            )
        if env.get("RUN_ONCE"):
            logger.warning(
                "env var RUN_ONCE is ignored; the new service is always-on "
                "and uses settings.schedule"
            )

        connection = ConnectionSettings(
            address=_env("ADDRESS", ""),
            timeout_seconds=float(_env("TIMEOUT", "10.0")),
        )

        schedule = ScheduleSettings(
            cron_expression=_env("BLACKVUESYNC_SCHEDULE", "*/15 * * * *"),
            timezone=_env("BLACKVUESYNC_TIMEZONE", "UTC"),
        )

        raw_include = _env("INCLUDE", "")
        raw_exclude = _env("EXCLUDE", "")
        raw_skip_meta = _env("SKIP_METADATA", "")
        sync = SyncSettings(
            priority=_env("PRIORITY", "date"),  # type: ignore[arg-type]
            grouping=_env("GROUPING", "none"),  # type: ignore[arg-type]
            include=_split_codes(raw_include),
            exclude=_split_codes(raw_exclude),
            retry_failed_after=_env("RETRY_FAILED_AFTER", "1d"),
            skip_metadata=tuple(raw_skip_meta) if raw_skip_meta else (),  # type: ignore[arg-type]
            affinity_key=_env("AFFINITY_KEY", "") or None,
        )

        retention = RetentionSettings(
            keep=_env("KEEP", "2w"),
            max_used_disk_percent=int(_env("MAX_USED_DISK", "90")),
        )

        verbose_raw = _env("VERBOSE", "0")
        log_settings = LoggingSettings(
            verbose=int(verbose_raw) if verbose_raw.isdigit() else 0,
            quiet=_env("QUIET", "").lower() in ("1", "true", "yes"),
            format=_env("LOG_FORMAT", "text"),  # type: ignore[arg-type]
        )

        metrics = MetricsSettings(
            file=_env("METRICS_FILE", "") or None,
            pushgateway_url=_env("METRICS_PUSHGATEWAY_URL", "") or None,
            job=_env("METRICS_JOB", "blackvuesync"),
            instance=_env("METRICS_INSTANCE", "") or None,
            state_file=_env("METRICS_STATE_FILE", ""),
        )

        stats = StatsSettings(
            retention_days=int(_env("STATS_RETENTION_DAYS", "365")),
        )

        viewer = ViewerSettings()

        web = WebSettings(
            port=int(_env("BLACKVUESYNC_PORT", "8080")),
        )

        admin_password = _env("BLACKVUESYNC_ADMIN_PASSWORD", "")
        auth = AuthSettings(
            username=_env("BLACKVUESYNC_ADMIN_USERNAME", "admin"),
            password_hash=(
                _hash_admin_password(admin_password) if admin_password else ""
            ),
            session_secret=secrets.token_hex(32),
        )

        settings = Settings(
            connection=connection,
            schedule=schedule,
            sync=sync,
            retention=retention,
            logging=log_settings,
            metrics=metrics,
            stats=stats,
            viewer=viewer,
            web=web,
            auth=auth,
        )

        bootstrapped_fields = [
            k
            for k, v in {
                "ADDRESS": env.get("ADDRESS"),
                "TIMEOUT": env.get("TIMEOUT"),
                "BLACKVUESYNC_SCHEDULE": env.get("BLACKVUESYNC_SCHEDULE"),
                "BLACKVUESYNC_PORT": env.get("BLACKVUESYNC_PORT"),
                "BLACKVUESYNC_ADMIN_USERNAME": env.get("BLACKVUESYNC_ADMIN_USERNAME"),
            }.items()
            if v
        ]
        if bootstrapped_fields:
            logger.info(
                "bootstrapped settings from env vars: %s",
                ", ".join(bootstrapped_fields),
            )

        return settings

    def _load_or_bootstrap(self) -> Settings:
        """loads settings from disk if the file exists; otherwise bootstraps from env.

        a loaded file without a session secret gets a fresh one, persisted
        immediately so sessions survive restarts.
        """
        if self._path.exists():
            settings = self._load()
            if settings.auth.session_secret:
                return settings
            logger.warning("auth.session_secret is empty; generating a new one")
            settings = dataclasses.replace(
                settings,
                auth=dataclasses.replace(
                    settings.auth, session_secret=secrets.token_hex(32)
                ),
            )
            self._save(settings)
            return settings
        settings = self._bootstrap_from_env()
        self._save(settings)
        return settings
