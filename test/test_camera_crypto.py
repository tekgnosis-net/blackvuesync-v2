"""tests for the blackvue wi-fi password encoding."""

from __future__ import annotations

from pathlib import Path

import pytest

from blackvuesync_v2.server.camera_crypto import (
    UndecodablePasswordError,
    decrypt_password,
    encrypt_password,
    is_encrypted,
)

FIXTURE = Path(__file__).parent / "fixtures" / "camera" / "dr900x-plus-config.ini"

# the invented plaintexts behind the fixture's encrypted values (fixture README)
PLAINTEXTS = {
    "ap_pw": "demo-cam",
    "sta_pw": "DemoHome-123",
    "sta2_pw": "DemoGarage-2",
    "sta3_pw": "DemoPhone-33",
}


def _fixture_passwords() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in FIXTURE.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in PLAINTEXTS:
            values[key] = value
    return values


def test_fixture_passwords_decrypt_to_known_plaintexts() -> None:
    stored = _fixture_passwords()
    assert set(stored) == set(PLAINTEXTS)
    for key, text in PLAINTEXTS.items():
        assert decrypt_password(stored[key]) == text


def test_encrypt_reproduces_the_camera_encoding() -> None:
    stored = _fixture_passwords()
    for key, text in PLAINTEXTS.items():
        assert encrypt_password(text) == stored[key]


def test_round_trip_unicode_and_empty() -> None:
    for text in ("", "pässwörd-ü", "x" * 32):
        assert decrypt_password(encrypt_password(text)) == text


def test_plain_text_values_pass_through() -> None:
    assert decrypt_password("hunter22") == "hunter22"
    assert decrypt_password("") == ""


def test_is_encrypted_only_for_64_hex_characters() -> None:
    assert is_encrypted("A" * 64)
    assert is_encrypted("0f" * 32)
    assert not is_encrypted("A" * 63)
    assert not is_encrypted("G" * 64)
    assert not is_encrypted("")


def test_password_longer_than_32_bytes_is_refused() -> None:
    with pytest.raises(ValueError):
        encrypt_password("x" * 33)
    with pytest.raises(ValueError):
        encrypt_password("ü" * 17)  # 34 bytes as utf-8


def test_value_that_does_not_decrypt_to_text_is_reported() -> None:
    """review focus 4: another firmware's key yields bytes, not text."""
    with pytest.raises(UndecodablePasswordError):
        decrypt_password("00" * 32)
