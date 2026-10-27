"""describes the dashcam's config.ini keys for the camera settings panes.

labels follow the blackvue dr900x plus manual; value codes come from kumar's
camera, the manual and the dr900s config table (design spec, appendix a). a key
without a descriptor is still shown, as a plain field in an "Other" group.
"""

from __future__ import annotations

import dataclasses
import re

from blackvuesync_v2.server.camera_config import CONTROL_RE, CameraFile, safe_text
from blackvuesync_v2.server.camera_crypto import encrypt_password

MASK = "•" * 8

_ASCII_INT_RE = re.compile(r"-?[0-9]+")
_ASCII_HOUR_RE = re.compile(r"[0-9]{1,2}")
_ASCII_OFFSET_RE = re.compile(r"[+-]?[0-9]{1,4}")
_ASCII_TIME_RE = re.compile(r"[0-9]{4}")


def _is_password_key(key: str) -> bool:
    """returns whether a key name suggests it holds a password."""
    lower_key = key.lower()
    return (
        lower_key.endswith("_pw")
        or "password" in lower_key
        or "passwd" in lower_key
        or "pwd" in lower_key
    )


@dataclasses.dataclass(frozen=True)
class CameraField:
    """how one config.ini key is labelled and displayed."""

    # pylint: disable=too-many-instance-attributes
    section: str
    key: str
    label: str
    widget: str = "number"  # select, toggle, number, text, password, hour, readonly
    options: tuple[tuple[str, str], ...] = ()  # (raw value, label)
    unit: str = ""
    scale: float = 1.0
    secret: bool = False
    formats_card: bool = False  # the manual: changing it formats the microsd card
    read_only: bool = False
    not_fitted: bool = False  # hardware absent on the dr900x plus
    help: str = ""
    minimum: int | None = None  # inclusive bound for number widgets
    maximum: int | None = None


@dataclasses.dataclass(frozen=True)
class CameraTab:
    """one camera settings tab and the config.ini section behind it."""

    name: str
    label: str
    section: str


TABS: tuple[CameraTab, ...] = (
    CameraTab("basic", "Basic", "Tab1"),
    CameraTab("sensitivity", "Sensitivity", "Tab2"),
    CameraTab("system", "System", "Tab3"),
    CameraTab("wifi", "Wi-Fi", "Wifi"),
    CameraTab("cloud", "Cloud", "Cloud"),
)


def _t(section: str, key: str, label: str, **kw: object) -> CameraField:
    return CameraField(section, key, label, "toggle", **kw)  # type: ignore[arg-type]


def _n(section: str, key: str, label: str, **kw: object) -> CameraField:
    return CameraField(section, key, label, "number", **kw)  # type: ignore[arg-type]


def _s(
    section: str,
    key: str,
    label: str,
    options: tuple[tuple[str, str], ...],
    **kw: object,
) -> CameraField:
    return CameraField(section, key, label, "select", options, **kw)  # type: ignore[arg-type]


_SENSOR_AXES = (("1", "up and down"), ("2", "side to side"), ("3", "front and back"))
_VOICES = (
    ("STARTVOICE", "power on"),
    ("NORMALSTARTVOICE", "starting normal recording"),
    ("EVENTSTARTVOICE", "starting event recording"),
    ("CHANGERECORDMODEVOICE", "changing recording mode"),
    ("ENDVOICE", "power off"),
    ("SPEEDALERTVOICE", "speed alert"),
    ("ACCELERATIONVOICE", "rapid acceleration"),
    ("HARSHBRAKINGVOICE", "harsh braking"),
    ("SHARPTURNVOICE", "sharp turn"),
    ("CHANGECONFIGVOICE", "settings changed"),
    ("CLOUDVOICE", "cloud related"),
    ("PARKINGEVENTVOICE", "impact detected in parking mode"),
)
_DSM = (
    ("DsmDetectBox", "detection box"),
    ("DsmDrowsy", "drowsiness"),
    ("DsmDistracted", "distraction"),
    ("DsmUndetected", "driver not detected"),
    ("DsmCalling", "phone call"),
    ("DsmMaskOff", "mask off"),
    ("DsmSmoking", "smoking"),
    ("DsmParkingMode", "parking mode"),
    ("DsmLed", "LED"),
    ("DsmSensitivity", "sensitivity"),
    ("DsmVolume", "volume"),
)

_FIELDS: tuple[CameraField, ...] = (
    # basic ([Tab1])
    _s(
        "Tab1",
        "TimeSet",
        "Time source",
        (("0", "Sync with GPS"), ("1", "Manual")),
        formats_card=True,
    ),
    CameraField(
        "Tab1",
        "SetTime",
        "Manual time",
        "text",
        formats_card=True,
        help="24-hour HHMM, used when the time source is manual",
    ),
    CameraField(
        "Tab1",
        "TimeZone",
        "Time zone",
        "text",
        formats_card=True,
        help="UTC offset as HHMM, like 1000 or -1100; minutes 00, 30 or 45",
    ),
    _t("Tab1", "Daylight", "Daylight saving time", formats_card=True),
    _t("Tab1", "GpsSync", "Sync time with GPS", formats_card=True),
    # minimum and maximum stay unset: the firmware's range is uncalibrated
    _n(
        "Tab1",
        "ImageSetting",
        "Image setting",
        formats_card=True,
        help="0 is the highest setting",
    ),
    _s(
        "Tab1",
        "VideoQuality",
        "Image quality",
        (("0", "Highest (Extreme)"), ("1", "Highest"), ("2", "High"), ("3", "Normal")),
        formats_card=True,
    ),
    _t("Tab1", "NormalRecord", "Normal recording"),
    _s(
        "Tab1",
        "AutoParking",
        "Parking mode",
        (("1", "Motion & Impact detection"), ("2", "Time lapse")),
    ),
    _t("Tab1", "RearParkingMode", "Rear camera recording in parking mode"),
    _t("Tab1", "VoiceRecord", "Voice recording"),
    _t("Tab1", "DateDisplay", "Date and time display"),
    _s("Tab1", "SpeedUnit", "Speed unit", (("0", "km/h"), ("1", "MPH"), ("2", "Off"))),
    CameraField(
        "Tab1",
        "RecordTime",
        "Video segment length",
        "readonly",
        (("1", "1 minute"),),
        read_only=True,
    ),
    _t("Tab1", "LockEvent", "Lock event files"),
    _t("Tab1", "OverwriteLock", "Overwrite locked event files when full"),
    _s("Tab1", "FrontRotate", "Front camera rotation", (("0", "Off"), ("1", "180°"))),
    _s(
        "Tab1",
        "RearRotate",
        "Rear camera orientation",
        (("0", "Default"), ("1", "Rotate 180°"), ("2", "Mirror")),
    ),
    _t("Tab1", "UseGpsInfo", "GPS location recording"),
    # sensitivity ([Tab2])
    *(
        _n(
            "Tab2",
            f"NORMALSENSOR{n}",
            f"G-sensor, normal mode: {axis}",
            minimum=0,
            maximum=10,
        )
        for n, axis in _SENSOR_AXES
    ),
    *(
        _n(
            "Tab2",
            f"PARKINGSENSOR{n}",
            f"G-sensor, parking mode: {axis}",
            minimum=0,
            maximum=10,
        )
        for n, axis in _SENSOR_AXES
    ),
    _n(
        "Tab2",
        "MOTIONSENSOR",
        "Motion detection, parking mode",
        minimum=0,
        maximum=10,
    ),
    _n(
        "Tab2",
        "FrontMotionRegion",
        "Front motion detection regions",
        help="65535 means all regions",
    ),
    _n(
        "Tab2",
        "RearMotionRegion",
        "Rear motion detection regions",
        help="65535 means all regions",
    ),
    # system ([Tab3])
    _t("Tab3", "RECLED", "Recording status LED"),
    _t("Tab3", "NORMALLED", "Front security LED, normal mode"),
    _t("Tab3", "PARKINGLED", "Front security LED, parking mode"),
    _t("Tab3", "RearLED", "Rear security LED"),
    _t("Tab3", "LTELED", "LTE LED, parking mode"),
    _t("Tab3", "WifiLED", "Wi-Fi LED, parking mode"),
    _t("Tab3", "BTLED", "Bluetooth LED, parking mode"),
    _s(
        "Tab3",
        "PSENSOR",
        "Proximity sensor",
        (("0", "Off"), ("1", "Manual recording"), ("2", "Voice recording")),
    ),
    *(_t("Tab3", key, f"Voice guidance: {what}") for key, what in _VOICES),
    _n("Tab3", "VOLUME", "Volume", minimum=0, maximum=10),
    _t("Tab3", "ScheduledReboot", "Scheduled reboot"),
    CameraField("Tab3", "ScheduledRebootTime", "Scheduled reboot time", "hour"),
    _n(
        "Tab3",
        "EventSpeedUnit",
        "Speed alert unit",
        help="value codes not confirmed yet",
    ),
    _n(
        "Tab3",
        "AlertLimit",
        "Speed alert limit",
        help="in the speed unit from the Basic tab",
    ),
    _t("Tab3", "Battery", "Battery protection", help="hardwired installations only"),
    _n(
        "Tab3",
        "LowvoltageTime",
        "Low-voltage cut-off timer",
        unit="h",
        options=(("0", "Off"),),
        help="hours until power-off: up to 12 on 12 V, 48 on 24 V",
    ),
    _n("Tab3", "LowvoltageVolt", "Low-voltage cut-off, 12 V", unit="V", scale=0.01),
    _n(
        "Tab3", "LowvoltageVoltHeavy", "Low-voltage cut-off, 24 V", unit="V", scale=0.01
    ),
    CameraField(
        "Tab3", "userString", "User text overlay", "text", help="up to 20 characters"
    ),
    _n("Tab3", "AccelLimit", "Rapid acceleration alert threshold"),
    _n("Tab3", "HarshLimit", "Harsh braking alert threshold"),
    _n("Tab3", "SharpLimit", "Sharp turn alert threshold"),
    _t("Tab3", "BTPair", "Bluetooth pairing"),
    *(
        _n("Tab3", key, f"Driver monitoring: {what}", not_fitted=True)
        for key, what in _DSM
    ),
    # wi-fi ([Wifi])
    CameraField("Wifi", "ap_ssid", "Camera hotspot name", "text"),
    CameraField("Wifi", "ap_pw", "Camera hotspot password", "password", secret=True),
    _s("Wifi", "WiFiBand", "Wi-Fi band", (("0", "5 GHz"), ("1", "2.4 GHz"))),
    _t(
        "Wifi",
        "WifiSleepMode",
        "Wi-Fi auto turn off",
        help="turns Wi-Fi off after 10 minutes without use",
    ),
    # cloud ([Cloud])
    _t("Cloud", "CloudService", "Cloud service"),
    *(
        field
        for n, suffix in ((1, ""), (2, "2"), (3, "3"))
        for field in (
            CameraField("Cloud", f"sta{suffix}_ssid", f"Home network {n} name", "text"),
            CameraField(
                "Cloud",
                f"sta{suffix}_pw",
                f"Home network {n} password",
                "password",
                secret=True,
            ),
        )
    ),
    CameraField(
        "Cloud",
        "CloudSettingVersion",
        "Cloud settings version",
        "readonly",
        read_only=True,
    ),
)

FIELDS: dict[tuple[str, str], CameraField] = {(f.section, f.key): f for f in _FIELDS}


def field_for(section: str, key: str) -> CameraField:
    """returns the descriptor for a key, or a password/text field for unknown keys."""
    if (section, key) in FIELDS:
        return FIELDS[(section, key)]
    if _is_password_key(key):
        return CameraField(section, key, key, "password", secret=True)
    return CameraField(section, key, key, "text")


def is_secret(section: str, key: str) -> bool:
    """returns whether a key holds a password."""
    return field_for(section, key).secret


def format_utc_offset(raw: str) -> str:
    """renders a signed HHMM offset ("1000", "-1100", "930") as "UTC+10:00"."""
    text = raw.strip()
    if not _ASCII_OFFSET_RE.fullmatch(text):
        return raw
    sign = "-" if text.startswith("-") else "+"
    digits = text.lstrip("+-")
    hours, minutes = divmod(int(digits), 100)
    if minutes >= 60:
        return raw
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def display_value(field: CameraField, raw: str) -> str:
    """renders a raw value for reading; passwords are masked."""
    if field.secret:
        return MASK if raw else ""
    value = raw
    if field.widget == "toggle":
        value = {"0": "Off", "1": "On"}.get(raw, raw)
    elif field.key == "TimeZone":
        value = format_utc_offset(raw)
    elif field.widget == "hour" and _ASCII_HOUR_RE.fullmatch(raw):
        value = f"{int(raw):02d}:00"
    else:
        labels = dict(field.options)
        if raw in labels:
            value = labels[raw]
        elif field.scale != 1.0:
            if _ASCII_INT_RE.fullmatch(raw):
                value = f"{int(raw) * field.scale:g} {field.unit}".strip()
        elif field.unit and raw and _ASCII_INT_RE.fullmatch(raw):
            value = f"{raw} {field.unit}"
    return value


@dataclasses.dataclass(frozen=True)
class FieldView:
    """one key ready for display; raw is None for passwords."""

    # pylint: disable=too-many-instance-attributes
    key: str  # "Section.key"
    label: str
    value: str
    raw: str | None
    secret: bool
    has_value: bool
    formats_card: bool
    help: str
    widget: str = "readonly"
    options: tuple[tuple[str, str], ...] = ()
    minimum: int | None = None
    maximum: int | None = None
    maxlength: int | None = None
    editable: bool = (
        False  # writable key (unknown keys as text): the panes render a control
    )


@dataclasses.dataclass(frozen=True)
class TabView:
    """one tab's keys: described, unknown ("Other") and hardware not fitted."""

    name: str
    label: str
    section: str
    fields: tuple[FieldView, ...]
    other: tuple[FieldView, ...]
    not_fitted: tuple[FieldView, ...]


def _view(section: str, key: str, raw: str, known: bool) -> FieldView:
    field = field_for(section, key)
    label = (
        field.label
        if known or section in {t.section for t in TABS}
        else f"{section}.{key}"
    )
    full_key = f"{section}.{key}"
    return FieldView(
        key=safe_text(full_key),
        label=safe_text(label),
        value=safe_text(display_value(field, raw)),
        raw=None if field.secret else safe_text(raw),
        secret=field.secret,
        has_value=bool(raw),
        formats_card=field.formats_card,
        help=field.help,
        widget=field.widget,
        options=tuple((safe_text(v), safe_text(t)) for v, t in field.options),
        minimum=field.minimum,
        maximum=field.maximum,
        maxlength=_TEXT_LIMITS.get((section, key)),
        editable=not field.read_only and field.widget != "readonly",
    )


def build_tabs(cfile: CameraFile) -> list[TabView]:
    """groups the file's keys into the five tabs; unknown sections go to System."""
    tab_sections = {t.section for t in TABS}
    groups: dict[str, dict[str, list[FieldView]]] = {
        t.section: {"fields": [], "other": [], "not_fitted": []} for t in TABS
    }
    seen: set[tuple[str, str]] = set()
    for entry in cfile.entries:
        if (entry.section, entry.key) in seen:
            continue  # the first occurrence is what the camera uses
        seen.add((entry.section, entry.key))
        known = (entry.section, entry.key) in FIELDS
        view = _view(entry.section, entry.key, entry.value, known)
        target = entry.section if entry.section in tab_sections else "Tab3"
        if known and field_for(entry.section, entry.key).not_fitted:
            groups[target]["not_fitted"].append(view)
        elif known:
            groups[target]["fields"].append(view)
        else:
            groups[target]["other"].append(view)
    return [
        TabView(
            name=t.name,
            label=t.label,
            section=t.section,
            fields=tuple(groups[t.section]["fields"]),
            other=tuple(groups[t.section]["other"]),
            not_fitted=tuple(groups[t.section]["not_fitted"]),
        )
        for t in TABS
    ]


FORMAT_KEYS: frozenset[tuple[str, str]] = frozenset(
    (f.section, f.key) for f in _FIELDS if f.formats_card
)
_INT_RE = re.compile(r"-?[0-9]{1,6}")
_TEXT_LIMITS = {("Tab3", "userString"): 20}


def _valid_offset(value: str) -> bool:
    """returns whether value is a utc offset in HHMM (-1200 to +1400, :00/:30/:45)."""
    if not _ASCII_OFFSET_RE.fullmatch(value):
        return False
    number = int(value)
    return -1200 <= number <= 1400 and abs(number) % 100 in (0, 30, 45)


def _normal_offset(value: str) -> str:
    """writes an offset in the camera's own form: no plus, four digits."""
    sign = "-" if value.startswith("-") else ""
    return sign + value.lstrip("+-").zfill(4)


def _valid_clock(value: str) -> bool:
    """returns whether value is an empty or a 24-hour HHMM time."""
    if value == "":
        return True
    if not _ASCII_TIME_RE.fullmatch(value):
        return False
    return int(value[:2]) <= 23 and int(value[2:]) <= 59


def validate_change(  # pylint: disable=too-many-return-statements,too-many-branches
    section: str, key: str, value: str
) -> list[str]:
    """returns the problems with setting section.key to value (empty = ok)."""
    name = f"{section}.{key}"
    if CONTROL_RE.search(value):
        return [f"{name}: control characters are not allowed"]
    field = field_for(section, key)
    if field.read_only:
        return [f"{name}: read-only"]
    if field.secret:
        if value == "" and key != "ap_pw":
            return []
        return (
            []
            if 8 <= len(value.encode("utf-8")) <= 32
            else [f"{name}: 8 to 32 characters"]
        )
    if field.key == "TimeZone":
        if _valid_offset(value):
            return []
        return [f"{name}: must be a UTC offset like 1000 or -1100"]
    if field.key == "SetTime":
        if _valid_clock(value):
            return []
        return [f"{name}: must be a 24-hour time like 0930"]
    if field.widget == "select":
        allowed = [raw for raw, _label in field.options]
        return [] if value in allowed else [f"{name}: not one of {', '.join(allowed)}"]
    if field.widget == "toggle":
        return [] if value in ("0", "1") else [f"{name}: must be 0 or 1"]
    if field.widget == "hour":
        ok = _ASCII_HOUR_RE.fullmatch(value) is not None and int(value) <= 23
        return [] if ok else [f"{name}: must be an hour from 0 to 23"]
    if field.widget == "number":
        if not _INT_RE.fullmatch(value):
            return [f"{name}: must be a whole number"]
        low, high = field.minimum, field.maximum
        if low is not None and high is not None and not low <= int(value) <= high:
            return [f"{name}: must be between {low} and {high}"]
        return []
    if key.endswith("_ssid") and len(value.encode("utf-8")) > 32:
        return [f"{name}: at most 32 bytes"]
    limit = _TEXT_LIMITS.get((section, key))
    if limit is not None and len(value) > limit:
        return [f"{name}: at most {limit} characters"]
    return []


def prepare_changes(
    raw_changes: dict[str, str],
) -> tuple[dict[tuple[str, str], str], list[str]]:
    """validates "Section.key" -> value pairs and encodes passwords for the file."""
    changes: dict[tuple[str, str], str] = {}
    errors: list[str] = []
    for full_key, value in raw_changes.items():
        section, dot, key = full_key.partition(".")
        if not dot or not section or not key:
            errors.append(f"{full_key}: expected Section.key")
            continue
        problems = validate_change(section, key, str(value))
        if problems:
            errors.extend(problems)
            continue
        if is_secret(section, key) and value != "":
            changes[(section, key)] = encrypt_password(str(value))
        elif (section, key) == ("Tab1", "TimeZone"):
            changes[(section, key)] = _normal_offset(str(value))
        else:
            changes[(section, key)] = str(value)
    return changes, errors


__all__ = [
    "FIELDS",
    "FORMAT_KEYS",
    "MASK",
    "TABS",
    "CameraField",
    "CameraTab",
    "FieldView",
    "TabView",
    "build_tabs",
    "display_value",
    "field_for",
    "format_utc_offset",
    "is_secret",
    "prepare_changes",
    "validate_change",
]
