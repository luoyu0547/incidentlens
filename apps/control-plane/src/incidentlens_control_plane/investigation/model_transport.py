"""The single OpenAI-compatible HTTP/TLS/error boundary.

Every model-backed subsystem — turn generation (``OpenAICompatibleProvider``),
semantic compaction (``OpenAICompatibleCompactor``), and later project-memory
extraction and selection — sends chat-completions payloads through
:class:`OpenAICompatibleTransport`.  No adapter may build a second independent
``urlopen`` path.

The transport owns the ``<base_url>/chat/completions`` join, request
serialization, the verified :mod:`certifi` TLS context, and redacted error
classification:

- 413 / an ``context_length_exceeded`` body raises the preserved
  :class:`~incidentlens_control_plane.investigation.provider.PromptTooLongError`;
- 408/429/5xx and connection timeouts are retryable
  :class:`ModelTransportError` failures;
- TLS certificate-verification and malformed base-configuration failures are
  non-retryable ``ModelTransportError`` failures.

Error messages never include API keys or response bodies.
"""

from __future__ import annotations

import json
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi

from incidentlens_control_plane.investigation.provider import PromptTooLongError


class ModelTransportError(Exception):
    """A redacted, classified failure from the shared model transport.

    ``retryable`` tells the caller whether the request may be retried;
    ``category`` is a stable, non-freeform label for observability and policy
    (for example ``"tls_configuration"``, ``"http_error"``,
    ``"connection"`` or ``"base_configuration"``).  Messages never contain API
    keys or response bodies.
    """

    def __init__(self, message: str, *, retryable: bool, category: str) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.category = category


@dataclass(frozen=True, slots=True)
class OpenAICompatibleConfig:
    """Connection configuration for one OpenAI-compatible endpoint."""

    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 90.0


# Status codes the provider may be able to recover from by retrying the turn.
_RETRYABLE_HTTP_CODES = frozenset({408, 429, 500, 502, 503, 504})


class OpenAICompatibleTransport:
    """One POST path to ``<base_url>/chat/completions`` over verified TLS."""

    def __init__(self, config: OpenAICompatibleConfig) -> None:
        self._config = config
        self._tls_context = ssl.create_default_context(cafile=certifi.where())

    def chat_completions(self, payload: dict[str, object]) -> dict[str, object]:
        """POST ``payload`` and return the parsed JSON response envelope."""
        url = f"{self._config.base_url.rstrip('/')}/chat/completions"
        request = Request(
            url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self._config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(  # noqa: S310
                request,
                timeout=self._config.timeout_seconds,
                context=self._tls_context,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
            if _context_length_exceeded(body):
                raise PromptTooLongError()
            return body
        except HTTPError as exc:
            if exc.code == 413:
                raise PromptTooLongError() from exc
            raise ModelTransportError(
                f"OpenAI-compatible API 请求失败（HTTP {exc.code}）",
                retryable=exc.code in _RETRYABLE_HTTP_CODES,
                category="http_error",
            ) from exc
        except (URLError, OSError, ValueError) as exc:
            if _is_tls_verification_failure(exc):
                raise ModelTransportError(
                    "OpenAI-compatible API TLS 证书校验失败",
                    retryable=False,
                    category="tls_configuration",
                ) from exc
            if isinstance(exc, (TimeoutError, URLError)):
                raise ModelTransportError(
                    "OpenAI-compatible API 连接失败",
                    retryable=True,
                    category="connection",
                ) from exc
            raise ModelTransportError(
                "OpenAI-compatible API 配置无效",
                retryable=False,
                category="base_configuration",
            ) from exc


def _context_length_exceeded(payload: Any) -> bool:
    """Return True when an OpenAI-style error body names a context overflow."""
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    code = error.get("code")
    if isinstance(code, str) and "context_length_exceeded" in code:
        return True
    message = error.get("message")
    return isinstance(message, str) and "context_length_exceeded" in message.lower()


def _is_tls_verification_failure(exc: BaseException) -> bool:
    """Return True when the failure is a certificate-verification error.

    ``urllib`` usually wraps the underlying :class:`ssl.SSLContext` error in a
    :class:`URLError.reason`, but it can also surface the ``SSLError`` directly;
    both shapes are certificate-verification failures and are never retryable.
    """
    for candidate in (exc, getattr(exc, "reason", None)):
        if isinstance(candidate, ssl.SSLCertVerificationError):
            return True
    return False


__all__ = [
    "ModelTransportError",
    "OpenAICompatibleConfig",
    "OpenAICompatibleTransport",
]
