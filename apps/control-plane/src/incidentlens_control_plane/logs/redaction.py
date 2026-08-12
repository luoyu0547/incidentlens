"""Deterministic redaction of sensitive values from log messages.

Redaction is intentionally conservative: it replaces any value that looks
sensitive with a fixed per-class placeholder, so the original secret never
survives into stored or displayed messages.  Processing is single-pass and
ordered so earlier replacements cannot re-trigger later rules (placeholder
tokens are never re-matched).
"""

from __future__ import annotations

import re
from typing import Callable

from incidentlens_control_plane.logs.types import RedactionResult, TruncationInfo

_MAX_MESSAGE_LENGTH = 16 * 1024

_REDACTED_TOKEN = "[REDACTED_TOKEN]"
_REDACTED_PASSWORD = "[REDACTED_PASSWORD]"
_REDACTED_EMAIL = "[REDACTED_EMAIL]"
_REDACTED_IP = "[REDACTED_IP]"
_REDACTED_URL_SECRET = "[REDACTED_URL_SECRET]"

_PASSWORD_KEYS = frozenset({"password", "passwd", "pwd", "secret"})

_PEM_KEY_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.DOTALL,
)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+")
_BASIC_RE = re.compile(r"\bBasic\s+[A-Za-z0-9+/=]+")
_URL_SECRET_RE = re.compile(
    r"[?&](?P<key>secret|token|password|passwd|pwd|api[_-]?key|apikey|"
    r"access[_-]?key|auth|signature|sig|private[_-]?key|client[_-]?secret)=[^&\s]*"
)
# Matches both plain key-value forms (``password=abc``, ``token: abc``) and
# JSON-quoted forms (``"password": "abc"``, ``'token': 'abc'``).  The optional
# quotes around the key and the ``[=:]`` separator (with surrounding
# whitespace) let one rule cover text and structured log lines; the value group
# consumes the surrounding quotes so the replacement never echoes them.
_SECRET_KV_RE = re.compile(
    r"(?P<key>[\"']?"
    r"(?P<bare>\b(?:"
    r"client[_-]?secret|access[_-]?(?:token|key)|api[_-]?key|apikey|auth[_-]?token"
    r"|secret[_-]?key|private[_-]?key|token|password|passwd|pwd|secret"
    r")\b)"
    r"[\"']?)"
    r"\s*[=:]\s*"
    r"(?P<value>(?:\"[^\"]*\"|'[^']*'|(?!\[REDACTED_)[^\s,]+))"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
# RFC 3986 IPv6: either eight hex groups, or a "::" compressed form.  The
# boundary lookarounds are used instead of \b because IPv6 starts/ends with
# non-word characters (colons).  A prose clock time like "10:11:12" has no
# "::" and only three groups, so it does not match.
_IPV6_RE = re.compile(
    r"(?<![\dA-Fa-f:])"
    r"(?:"
    r"(?:[0-9a-fA-F]{1,4}:){7}[0-9a-fA-F]{1,4}"
    r"|"
    r"(?:(?:[0-9a-fA-F]{1,4}:){0,6}[0-9a-fA-F]{1,4})?"
    r"::"
    r"(?:(?:[0-9a-fA-F]{1,4}:){0,5}[0-9a-fA-F]{1,4})?"
    r")"
    r"(?![\dA-Fa-f:])"
)


def _apply(
    pattern: re.Pattern[str],
    text: str,
    repl: Callable[[re.Match[str]], str],
    summary: dict[str, int],
    key: str,
) -> str:
    """Apply ``repl`` for every match of ``pattern`` and record the count."""
    count = 0

    def _counted(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return repl(match)

    new_text = pattern.sub(_counted, text)
    if count:
        summary[key] = summary.get(key, 0) + count
    return new_text


def _url_secret_repl(match: re.Match[str]) -> str:
    prefix = match.group(0)[: match.group(0).index("=") + 1]
    return f"{prefix}{_REDACTED_URL_SECRET}"


def _secret_kv_repl(match: re.Match[str], summary: dict[str, int]) -> str:
    key = match.group("bare").lower()
    if key in _PASSWORD_KEYS:
        summary["password"] = summary.get("password", 0) + 1
        return f"{match.group('bare')}={_REDACTED_PASSWORD}"
    summary["token"] = summary.get("token", 0) + 1
    return f"{match.group('bare')}={_REDACTED_TOKEN}"


def redact_message(
    message: str, *, max_length: int = _MAX_MESSAGE_LENGTH
) -> RedactionResult:
    summary: dict[str, int] = {}
    text = message

    text = _apply(_PEM_KEY_RE, text, lambda _m: _REDACTED_TOKEN, summary, "token")
    text = _apply(_BEARER_RE, text, lambda _m: _REDACTED_TOKEN, summary, "token")
    text = _apply(_BASIC_RE, text, lambda _m: _REDACTED_TOKEN, summary, "token")
    text = _apply(_URL_SECRET_RE, text, _url_secret_repl, summary, "url_secret")
    text = _SECRET_KV_RE.sub(lambda m: _secret_kv_repl(m, summary), text)
    text = _apply(_EMAIL_RE, text, lambda _m: _REDACTED_EMAIL, summary, "email")
    text = _apply(_IPV4_RE, text, lambda _m: _REDACTED_IP, summary, "ip")
    text = _apply(_IPV6_RE, text, lambda _m: _REDACTED_IP, summary, "ip")

    original_length = len(text)
    kept_length = original_length
    truncated = False
    if len(text) > max_length:
        text = text[:max_length]
        kept_length = len(text)
        truncated = True
        summary["truncated"] = 1

    truncation = (
        TruncationInfo(
            original_length=original_length,
            kept_length=kept_length,
            truncated=True,
        )
        if truncated
        else None
    )
    return RedactionResult(
        message_redacted=text,
        summary=summary,
        truncated=truncated,
        truncation=truncation,
    )
