"""Time-based one-time passwords, RFC 6238. The app code uses SHA-256, 8 digits, and a 30-second step."""

import base64
import binascii
import hashlib
import hmac
import struct

STEP_SECONDS = 30
DIGITS = 8


def decode_secret(base32: str) -> bytes:
    """The key bytes of a base32 secret. Spaces, lower case, and missing padding are accepted."""
    text = "".join(base32.split()).upper()
    try:
        key = base64.b32decode(text + "=" * (-len(text) % 8))
    except binascii.Error as exc:
        raise ValueError("Must be base32 text.") from exc
    if not key:
        raise ValueError("Must not be empty.")
    return key


def code_at(key: bytes, at: float) -> str:
    digest = hmac.new(key, struct.pack(">Q", int(at // STEP_SECONDS)), hashlib.sha256).digest()
    offset = digest[-1] & 0x0F
    number: int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return f"{number % 10**DIGITS:0{DIGITS}d}"


def is_valid(key: bytes, code: str, at: float, *, steps: int = 1) -> bool:
    """True if `code` is the code of the current step or of one of the `steps` before or after it."""
    given = code.encode()
    valid = False
    # Each candidate is compared, and each compare takes the same time, so the time tells nothing about the code.
    for drift in range(-steps, steps + 1):
        valid |= hmac.compare_digest(code_at(key, at + drift * STEP_SECONDS).encode(), given)
    return valid
