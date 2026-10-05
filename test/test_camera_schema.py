"""tests for the camera key descriptors and display rules."""

from __future__ import annotations

from pathlib import Path

from blackvuesync_v2.server.camera_config import parse
from blackvuesync_v2.server.camera_schema import (
    FIELDS,
    MASK,
    TABS,
    build_tabs,
    display_value,
    field_for,
    format_utc_offset,
    is_secret,
)

CONFIG = (
    Path(__file__).parent / "fixtures" / "camera" / "dr900x-plus-config.ini"
).read_bytes()


def _config_ciphertexts() -> list[str]:
    """extracts all password values from the fixture config bytes."""
    text = CONFIG.decode("utf-8", errors="replace")
    ciphertexts: list[str] = []
    for line in text.splitlines():
        if "_pw=" in line:
            ciphertexts.append(line.split("=", 1)[1])
    return ciphertexts


def test_tabs_match_the_blackvue_app() -> None:
    assert [(t.name, t.label, t.section) for t in TABS] == [
        ("basic", "Basic", "Tab1"),
        ("sensitivity", "Sensitivity", "Tab2"),
        ("system", "System", "Tab3"),
        ("wifi", "Wi-Fi", "Wifi"),
        ("cloud", "Cloud", "Cloud"),
    ]


def test_every_dr900x_plus_key_is_described() -> None:
    cfile = parse(CONFIG)
    missing = [
        f"{e.section}.{e.key}"
        for e in cfile.entries
        if (e.section, e.key) not in FIELDS
    ]
    assert missing == []
    assert len(FIELDS) == 85


def test_secret_and_format_flags() -> None:
    assert {f"{s}.{k}" for (s, k), f in FIELDS.items() if f.secret} == {
        "Wifi.ap_pw",
        "Cloud.sta_pw",
        "Cloud.sta2_pw",
        "Cloud.sta3_pw",
    }
    assert {k for (_s, k), f in FIELDS.items() if f.formats_card} == {
        "TimeSet",
        "SetTime",
        "TimeZone",
        "Daylight",
        "GpsSync",
        "ImageSetting",
        "VideoQuality",
    }
    assert is_secret("Cloud", "sta_pw")
    assert not is_secret("Cloud", "sta_ssid")
    assert not is_secret("Tab3", "userString")


def test_utc_offsets() -> None:
    assert format_utc_offset("1000") == "UTC+10:00"
    assert format_utc_offset("-1100") == "UTC-11:00"
    assert format_utc_offset("930") == "UTC+09:30"
    assert format_utc_offset("0") == "UTC+00:00"
    assert format_utc_offset("abc") == "abc"
    assert format_utc_offset("1070") == "1070"


def test_display_values() -> None:
    assert display_value(field_for("Tab1", "TimeZone"), "1000") == "UTC+10:00"
    assert display_value(field_for("Tab1", "VideoQuality"), "0") == "Highest (Extreme)"
    assert display_value(field_for("Tab1", "VideoQuality"), "9") == "9"
    assert display_value(field_for("Tab3", "LowvoltageVolt"), "1190") == "11.9 V"
    assert display_value(field_for("Tab3", "LowvoltageTime"), "0") == "Off"
    assert display_value(field_for("Tab3", "LowvoltageTime"), "12") == "12 h"
    assert display_value(field_for("Tab3", "ScheduledRebootTime"), "3") == "03:00"
    assert display_value(field_for("Tab3", "RECLED"), "1") == "On"
    assert display_value(field_for("Wifi", "WiFiBand"), "0") == "5 GHz"
    assert display_value(field_for("Cloud", "sta_pw"), "ABC") == MASK
    assert display_value(field_for("Cloud", "sta3_pw"), "") == ""


def test_build_tabs_groups_the_fixture() -> None:
    tabs = build_tabs(parse(CONFIG))
    assert [t.name for t in tabs] == [t.name for t in TABS]
    by_name = {t.name: t for t in tabs}
    basic = {f.key: f for f in by_name["basic"].fields}
    assert basic["Tab1.TimeZone"].value == "UTC+10:00"
    assert basic["Tab1.TimeZone"].raw == "1000"
    assert basic["Tab1.TimeZone"].formats_card
    system = by_name["system"]
    assert len(system.not_fitted) == 11
    assert all(f.key.startswith("Tab3.Dsm") for f in system.not_fitted)
    cloud = {f.key: f for f in by_name["cloud"].fields}
    password = cloud["Cloud.sta_pw"]
    assert (password.value, password.raw, password.secret) == (MASK, None, True)
    assert password.has_value
    rendered = repr(tabs)
    for ciphertext in _config_ciphertexts():
        assert ciphertext not in rendered
    assert "DemoHome-123" not in rendered
    for tab in tabs:
        assert tab.other == ()


def test_unknown_keys_and_sections_are_kept_under_other() -> None:
    """review focus 5: nothing a new firmware adds is dropped."""
    cfile = parse(b"[Tab1]\nVOLUME2=7\n[Fancy]\nMode=2\nLevel=1\n")
    tabs = {t.name: t for t in build_tabs(cfile)}
    assert [(f.key, f.label, f.value) for f in tabs["basic"].other] == [
        ("Tab1.VOLUME2", "VOLUME2", "7")
    ]
    assert [f.key for f in tabs["system"].other] == ["Fancy.Mode", "Fancy.Level"]
    assert tabs["system"].other[0].label == "Fancy.Mode"


def test_unknown_password_keys_are_masked() -> None:
    """r4: unknown keys ending with _pw, password, passwd, pwd are masked."""
    cfile = parse(b"[Cloud]\nsta4_pw=ABCDEF0123\n[Tab3]\nAdminPassword=hunter22\n")
    tabs = {t.name: t for t in build_tabs(cfile)}
    sta4 = {f.key: f for f in tabs["cloud"].other}["Cloud.sta4_pw"]
    admin = {f.key: f for f in tabs["system"].other}["Tab3.AdminPassword"]
    assert sta4.secret is True
    assert sta4.value == MASK
    assert sta4.raw is None
    assert admin.secret is True
    assert admin.value == MASK
    assert admin.raw is None
    rendered = repr(tabs)
    assert "ABCDEF0123" not in rendered
    assert "hunter22" not in rendered
    assert is_secret("Cloud", "sta4_pw")
    assert is_secret("Tab3", "AdminPassword")
    assert not is_secret("Cloud", "sta4_ssid")


def test_display_values_never_raise_on_bad_input() -> None:
    """r5: display value formatters handle unicode/invalid digits safely."""
    assert display_value(field_for("Tab3", "LowvoltageVolt"), "--5") == "--5"
    assert display_value(field_for("Tab3", "LowvoltageVolt"), "-100") == "-1 V"
    assert display_value(field_for("Tab3", "ScheduledRebootTime"), "²") == "²"
    assert format_utc_offset("²") == "²"
    assert format_utc_offset("--100") == "--100"
    assert display_value(field_for("Tab3", "LowvoltageVolt"), "") == ""


def test_field_view_key_and_label_use_safe_text() -> None:
    """r6: key and label are safe for html and json with non-utf8 bytes."""
    cfile = parse(b"[Caf\xe9]\nMode=1\n")
    tabs = build_tabs(cfile)
    system = {t.name: t for t in tabs}["system"]
    view = system.other[0]
    assert "�" in view.key
    assert "�" in view.label
    view.key.encode("utf-8")  # should not raise
    view.label.encode("utf-8")  # should not raise
