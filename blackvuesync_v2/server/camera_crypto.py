"""blackvue wi-fi password encoding used in the camera's config.ini.

the camera stores ap_pw and sta*_pw as uppercase hex of the password,
zero-padded to 32 bytes and encrypted with aes-128-cbc under a fixed key and
iv built into the blackvue app. the key and iv were published by the 2023 cve
research (github.com/eyJhb/blackvue-cve-2023, software/wifi-decrypt), so the
encoding hides nothing from anyone who has read it; the app still treats the
values as secrets. a value that is not 64 hex characters is plain text.
"""

from __future__ import annotations

import re

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_KEY = bytes.fromhex("331248f9789959003729fa90cf16882f")
_IV = bytes.fromhex("82931267f734a879b3c4c4b15fda4be7")
_BLOCK = 32  # the camera pads every password to 32 bytes
_ENCRYPTED_RE = re.compile(r"[0-9A-Fa-f]{64}")


class UndecodablePasswordError(ValueError):
    """the stored value looks encrypted but does not decrypt to text."""


def _cipher() -> Cipher[modes.CBC]:
    return Cipher(algorithms.AES(_KEY), modes.CBC(_IV))


def is_encrypted(value: str) -> bool:
    """returns whether a stored value has the camera's encrypted shape."""
    return bool(_ENCRYPTED_RE.fullmatch(value))


def decrypt_password(value: str) -> str:
    """returns the password text; plain-text values come back unchanged."""
    if not is_encrypted(value):
        return value
    decryptor = _cipher().decryptor()
    plain = decryptor.update(bytes.fromhex(value)) + decryptor.finalize()
    try:
        text = plain.rstrip(b"\0").decode("utf-8")
    except UnicodeDecodeError as error:
        raise UndecodablePasswordError("value does not decrypt to text") from error
    if not text.isprintable():
        raise UndecodablePasswordError("value does not decrypt to text")
    return text


def encrypt_password(text: str) -> str:
    """returns the camera's stored form of a password of at most 32 bytes."""
    raw = text.encode("utf-8")
    if len(raw) > _BLOCK:
        raise ValueError(f"password is {len(raw)} bytes; the camera allows {_BLOCK}")
    encryptor = _cipher().encryptor()
    sealed = encryptor.update(raw.ljust(_BLOCK, b"\0")) + encryptor.finalize()
    return sealed.hex().upper()


__all__ = [
    "UndecodablePasswordError",
    "decrypt_password",
    "encrypt_password",
    "is_encrypted",
]
