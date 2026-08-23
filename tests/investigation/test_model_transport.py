"""Tests for the shared OpenAI-compatible model transport.

Covers: the single verified ``certifi`` TLS context, redacted error
classification (non-retryable certificate / base-configuration failures,
retryable HTTP / connection failures), prompt-too-long classification
(413 / context-length body), and the POST / response-envelope contract.  The
transport is the one HTTP/TLS/error boundary shared by the provider, compactor,
and (later) project-memory adapters.
"""

from __future__ import annotations

import json
import ssl
from urllib.error import HTTPError, URLError

import incidentlens_control_plane.investigation.model_transport as model_transport
import pytest
from incidentlens_control_plane.investigation.model_transport import (
    ModelTransportError,
    OpenAICompatibleConfig,
    OpenAICompatibleTransport,
)
from incidentlens_control_plane.investigation.provider import PromptTooLongError


@pytest.fixture
def config() -> OpenAICompatibleConfig:
    return OpenAICompatibleConfig(
        api_key="test-key",
        base_url="https://llm.example.com/v1",
        model="spark-x",
    )


def http_error(status: int) -> HTTPError:
    return HTTPError(
        url="https://llm.example.com/v1/chat/completions",
        code=status,
        msg="error",
        hdrs={},
        fp=None,
    )


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def _raise_after(exception: BaseException):
    def fail(request, *, timeout, context):
        raise exception

    return fail


# ---------------------------------------------------------------------------
# TLS: one verified certifi context for every request
# ---------------------------------------------------------------------------


def test_transport_uses_certifi_context(monkeypatch, config) -> None:
    opened = {}

    def fake_urlopen(request, *, timeout, context):
        opened["context"] = context
        return _FakeResponse(b'{"choices": []}')

    monkeypatch.setattr(model_transport, "urlopen", fake_urlopen)
    OpenAICompatibleTransport(config).chat_completions({"model": config.model})
    assert opened["context"].get_ca_certs()


def test_transport_requires_hostname_verification(monkeypatch, config) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        model_transport,
        "urlopen",
        lambda request, *, timeout, context: (
            captured.__setitem__("context", context) or _FakeResponse(b"{}")
        ),
    )
    OpenAICompatibleTransport(config).chat_completions({"model": config.model})
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_certificate_failure_is_non_retryable(monkeypatch, config) -> None:
    monkeypatch.setattr(
        model_transport,
        "urlopen",
        _raise_after(ssl.SSLCertVerificationError("certificate verify failed")),
    )
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.category == "tls_configuration"
    assert exc.value.retryable is False


def test_url_error_wrapping_cert_failure_is_tls_configuration(
    monkeypatch, config
) -> None:
    monkeypatch.setattr(
        model_transport,
        "urlopen",
        _raise_after(
            URLError(ssl.SSLCertVerificationError("certificate verify failed"))
        ),
    )
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.category == "tls_configuration"
    assert exc.value.retryable is False


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def test_http_413_is_prompt_too_long(monkeypatch, config) -> None:
    monkeypatch.setattr(model_transport, "urlopen", _raise_after(http_error(413)))
    with pytest.raises(PromptTooLongError):
        OpenAICompatibleTransport(config).chat_completions({})


def test_context_length_exceeded_body_is_prompt_too_long(monkeypatch, config) -> None:
    body = json.dumps(
        {"error": {"code": "context_length_exceeded", "message": "context too long"}}
    ).encode("utf-8")
    monkeypatch.setattr(
        model_transport,
        "urlopen",
        lambda request, *, timeout, context: _FakeResponse(body),
    )
    with pytest.raises(PromptTooLongError):
        OpenAICompatibleTransport(config).chat_completions({})


def test_http_429_is_retryable(monkeypatch, config) -> None:
    monkeypatch.setattr(model_transport, "urlopen", _raise_after(http_error(429)))
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.retryable is True
    assert exc.value.category == "http_error"


def test_http_500_is_retryable(monkeypatch, config) -> None:
    monkeypatch.setattr(model_transport, "urlopen", _raise_after(http_error(500)))
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.retryable is True


def test_http_400_unknown_is_not_retryable(monkeypatch, config) -> None:
    monkeypatch.setattr(model_transport, "urlopen", _raise_after(http_error(400)))
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.retryable is False


def test_timeout_is_retryable_connection(monkeypatch, config) -> None:
    monkeypatch.setattr(
        model_transport, "urlopen", _raise_after(TimeoutError("timed out"))
    )
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.retryable is True
    assert exc.value.category == "connection"


def test_malformed_base_url_is_non_retryable_config(monkeypatch, config) -> None:
    monkeypatch.setattr(
        model_transport, "urlopen", _raise_after(ValueError("unknown url type"))
    )
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert exc.value.retryable is False
    assert exc.value.category == "base_configuration"


def test_error_message_never_leaks_api_key(monkeypatch, config) -> None:
    monkeypatch.setattr(model_transport, "urlopen", _raise_after(http_error(429)))
    with pytest.raises(ModelTransportError) as exc:
        OpenAICompatibleTransport(config).chat_completions({})
    assert config.api_key not in str(exc.value)


# ---------------------------------------------------------------------------
# Request / response contract
# ---------------------------------------------------------------------------


def test_chat_completions_posts_one_verified_tls_request(monkeypatch, config) -> None:
    body = json.dumps(
        {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    ).encode("utf-8")
    captured: dict[str, object] = {}

    def fake_urlopen(request, *, timeout, context):
        captured["request"] = request
        captured["timeout"] = timeout
        captured["context"] = context
        return _FakeResponse(body)

    monkeypatch.setattr(model_transport, "urlopen", fake_urlopen)
    result = OpenAICompatibleTransport(config).chat_completions(
        {"model": config.model}
    )

    assert result == {"choices": [{"message": {"content": "{\"ok\": true}"}}]}
    assert captured["timeout"] == config.timeout_seconds
    context = captured["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    request = captured["request"]
    assert request.get_method() == "POST"
    assert request.full_url == "https://llm.example.com/v1/chat/completions"
    headers = {key.lower(): value for key, value in request.headers.items()}
    assert headers["authorization"] == f"Bearer {config.api_key}"
    assert headers["content-type"] == "application/json"
