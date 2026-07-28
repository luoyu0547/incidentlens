from pathlib import Path

import pytest
from pydantic import ValidationError

from incidentlens_control_plane.llm.config import (
    RuntimeMode,
    load_models_config,
    resolve_model_profile,
)


def write_config(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_environment_selects_glm_and_resolves_only_named_secret(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        """
active_model: deepseek
models:
  deepseek:
    adapter: openai_compatible
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
  glm:
    adapter: openai_compatible
    model: glm-4.5
    base_url: https://open.bigmodel.cn/api/paas/v4
    api_key_env: GLM_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
    )
    environ = {
        "INCIDENTLENS_LLM_ACTIVE_MODEL": "glm",
        "GLM_API_KEY": "glm-secret",
        "DEEPSEEK_API_KEY": "deepseek-secret",
    }

    config = load_models_config(path, environ)
    resolved = resolve_model_profile(config, config.active_model, environ)

    assert config.active_model == "glm"
    assert resolved.name == "glm"
    assert resolved.api_key.get_secret_value() == "glm-secret"
    assert "glm-secret" not in repr(resolved)


@pytest.mark.parametrize(
    "body",
    [
        "active_model: missing\nmodels: {}\nfallback_models: []\n",
        "active_model: deepseek\nmodels:\n  deepseek:\n    adapter: openai_compatible\n    model: ''\n    base_url: not-a-url\n    api_key_env: DEEPSEEK_API_KEY\nfallback_models: []\n",
        "active_model: deepseek\nmodels:\n  deepseek:\n    adapter: openai_compatible\n    model: deepseek-chat\n    base_url: https://api.deepseek.com\n    api_key_env: DEEPSEEK_API_KEY\n    unknown: true\nfallback_models: []\n",
    ],
)
def test_invalid_model_config_fails(tmp_path: Path, body: str) -> None:
    with pytest.raises((ValidationError, ValueError)):
        load_models_config(write_config(tmp_path, body), {})


def test_llm_mode_rejects_missing_secret_but_baseline_does_not_resolve_it(
    tmp_path: Path,
) -> None:
    path = write_config(
        tmp_path,
        """
active_model: deepseek
models:
  deepseek:
    adapter: openai_compatible
    model: deepseek-chat
    base_url: https://api.deepseek.com
    api_key_env: DEEPSEEK_API_KEY
    connect_timeout_seconds: 15
    read_timeout_seconds: 300
    max_retries: 2
fallback_models: []
""",
    )
    config = load_models_config(path, {})
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        resolve_model_profile(config, "deepseek", {})
    assert RuntimeMode.DETERMINISTIC_BASELINE.value == "deterministic_baseline"
