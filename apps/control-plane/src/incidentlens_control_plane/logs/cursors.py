"""Opaque cursor codec for product log history."""

from __future__ import annotations

import base64
import binascii
import re

_CURSOR_PREFIX = "lc1_"
_CURSOR_BODY = re.compile(r"^[A-Za-z0-9_-]{11}$")


def encode_log_cursor(sequence: int) -> str:
    """Encode a non-negative stream sequence as an opaque product cursor."""
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 0:
        raise ValueError("log sequence must be a non-negative integer")
    try:
        integer_bytes = sequence.to_bytes(8, "big", signed=False)
    except OverflowError:
        raise ValueError("log sequence is out of range") from None
    encoded = base64.urlsafe_b64encode(integer_bytes).decode("ascii").rstrip("=")
    return f"{_CURSOR_PREFIX}{encoded}"


def decode_log_cursor(cursor: str) -> int:
    """Decode an opaque product cursor, rejecting malformed tokens."""
    if not isinstance(cursor, str) or not cursor.startswith(_CURSOR_PREFIX):
        raise ValueError("not a product log cursor")
    encoded = cursor[len(_CURSOR_PREFIX) :]
    if not _CURSOR_BODY.fullmatch(encoded):
        raise ValueError("malformed product log cursor")
    try:
        integer_bytes = base64.urlsafe_b64decode(encoded + "=")
    except (binascii.Error, ValueError):
        raise ValueError("malformed product log cursor") from None
    if len(integer_bytes) != 8:
        raise ValueError("malformed product log cursor length")
    return int.from_bytes(integer_bytes, "big", signed=False)


__all__ = ["decode_log_cursor", "encode_log_cursor"]
