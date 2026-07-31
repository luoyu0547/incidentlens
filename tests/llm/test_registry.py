"""Tests for the LangChain Model Registry, Fallback, and Canary."""
from pathlib import Path

import httpx
import pytest
from incidentlens_control_plane.llm.config import load_models_config
from incidentlens_control_plane.llm.fallback import is_retryable_transport_error
from incidentlens_control_plane.llm.registry import ModelRegistry
from langchain_core.language_models.chat_models import BaseChatModel


def write_config(tmp_path: Path, body: str) -> Path:
    """Write a temporary models.yaml for testing."""
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_registry_passes_endpoint_model_secret_timeout_and_single_retry_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry should pass correct config to ChatOpenAI."""
    captured: dict = {}

    class SentinelChatModel:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(
        "incidentlens_control_plane.llm.registry.ChatOpenAI",
        SentinelChatModel,
    )
    config = load_models_config(
        write_config(
            tmp_path,
            """
active_model: xfyun-xopglm51
models:
  xfyun-xopglm51:
    adapter: openai_compatible
    model: xopglm51
    base_url: https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
    api_key_env: XFYUN_MAAS_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
        ),
        {},
    )
    registry = ModelRegistry(config, {"XFYUN_MAAS_API_KEY": "secret"})

    registry.get("xfyun-xopglm51")

    assert captured["model"] == "xopglm51"
    assert captured["base_url"] == "https://maas-coding-api.cn-huabei-1.xf-yun.com/v2"
    assert captured["api_key"].get_secret_value() == "secret"
    assert captured["max_retries"] == 2
    assert captured["timeout"].connect == 15
    assert captured["timeout"].read == 300


def test_switching_profile_changes_registry_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Different profiles should create different models."""
    models: list[str] = []

    class SentinelChatModel:
        def __init__(self, **kwargs):
            models.append(kwargs["model"])

    monkeypatch.setattr(
        "incidentlens_control_plane.llm.registry.ChatOpenAI",
        SentinelChatModel,
    )
    config = load_models_config(
        write_config(
            tmp_path,
            """
active_model: xfyun-xopglm51
models:
  xfyun-xopglm51:
    adapter: openai_compatible
    model: xopglm51
    base_url: https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
    api_key_env: XFYUN_MAAS_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
  backup:
    adapter: openai_compatible
    model: backup-model
    base_url: https://backup.example.com/v2
    api_key_env: BACKUP_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
        ),
        {},
    )
    registry = ModelRegistry(
        config,
        {"XFYUN_MAAS_API_KEY": "x", "BACKUP_API_KEY": "b"},
    )

    registry.get("xfyun-xopglm51")
    registry.get("backup")

    assert models == ["xopglm51", "backup-model"]


def test_registry_get_returns_base_chat_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """registry.get() should return a BaseChatModel."""

    config = load_models_config(
        write_config(
            tmp_path,
            """
active_model: xfyun-xopglm51
models:
  xfyun-xopglm51:
    adapter: openai_compatible
    model: xopglm51
    base_url: https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
    api_key_env: XFYUN_MAAS_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
        ),
        {},
    )
    registry = ModelRegistry(config, {"XFYUN_MAAS_API_KEY": "secret"})
    model = registry.get("xfyun-xopglm51")
    assert isinstance(model, BaseChatModel)


def test_registry_identity_returns_correct_metadata(
    tmp_path: Path,
) -> None:
    """registry.identity() should return correct profile metadata."""
    from incidentlens_control_plane.llm.registry import ModelIdentity

    config = load_models_config(
        write_config(
            tmp_path,
            """
active_model: xfyun-xopglm51
models:
  xfyun-xopglm51:
    adapter: openai_compatible
    model: xopglm51
    base_url: https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
    api_key_env: XFYUN_MAAS_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
        ),
        {},
    )
    registry = ModelRegistry(config, {"XFYUN_MAAS_API_KEY": "secret"})
    identity = registry.identity("xfyun-xopglm51")

    assert isinstance(identity, ModelIdentity)
    assert identity.profile == "xfyun-xopglm51"
    assert identity.model == "xopglm51"
    assert identity.endpoint_host == "maas-coding-api.cn-huabei-1.xf-yun.com"


def test_registry_active_profile_and_fallbacks(
    tmp_path: Path,
) -> None:
    """Registry should expose active_profile and fallback_profiles."""
    config = load_models_config(
        write_config(
            tmp_path,
            """
active_model: xfyun-xopglm51
models:
  xfyun-xopglm51:
    adapter: openai_compatible
    model: xopglm51
    base_url: https://maas-coding-api.cn-huabei-1.xf-yun.com/v2
    api_key_env: XFYUN_MAAS_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
  backup:
    adapter: openai_compatible
    model: backup-model
    base_url: https://backup.example.com/v2
    api_key_env: BACKUP_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: [backup]
""",
        ),
        {},
    )
    registry = ModelRegistry(config, {})

    assert registry.active_profile == "xfyun-xopglm51"
    assert registry.fallback_profiles == ("backup",)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectError("down"), True),
        (httpx.ReadTimeout("slow"), True),
        (type("StatusError", (Exception,), {"status_code": 429})(), True),
        (type("StatusError", (Exception,), {"status_code": 503})(), True),
        (type("StatusError", (Exception,), {"status_code": 401})(), False),
        (ValueError("invalid tool args"), False),
    ],
)
def test_fallback_predicate_allows_only_transport_429_and_5xx(exc, expected) -> None:
    """is_retryable_transport_error should correctly identify retryable errors."""
    assert is_retryable_transport_error(exc) is expected


def test_fallback_predicate_with_chained_exceptions() -> None:
    """is_retryable_transport_error should traverse __cause__ and __context__."""
    inner = httpx.ConnectError("connection refused")
    outer = ValueError("outer error")
    outer.__cause__ = inner
    assert is_retryable_transport_error(outer) is True

    inner2 = type("StatusError", (Exception,), {"status_code": 429})()
    outer2 = RuntimeError("runtime error")
    outer2.__context__ = inner2
    assert is_retryable_transport_error(outer2) is True


def test_source_does_not_import_openai_sdk() -> None:
    """Source should not directly import the openai SDK."""
    source_root = Path("apps/control-plane/src/incidentlens_control_plane")
    offenders = [
        path
        for path in source_root.rglob("*.py")
        if "import openai" in path.read_text(encoding="utf-8")
        or "from openai" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
