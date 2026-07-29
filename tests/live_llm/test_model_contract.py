"""Live canary test for verifying model tool-calling and conclusion capability."""
import os
from pathlib import Path

import pytest

from incidentlens_control_plane.llm.canary import run_conclusion_canary, run_model_canary
from incidentlens_control_plane.llm.config import load_models_config
from incidentlens_control_plane.llm.registry import ModelRegistry


@pytest.mark.live_llm
async def test_selected_profile_performs_real_required_tool_call() -> None:
    """Test that the active profile can perform a real tool call."""
    path = Path(os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml"))
    config = load_models_config(path, os.environ)
    profile = config.models[config.active_model]
    if profile.api_key_env not in os.environ:
        pytest.skip(f"missing {profile.api_key_env}")
    if not os.environ[profile.api_key_env].strip():
        pytest.fail(f"{profile.api_key_env} exists but is empty")

    registry = ModelRegistry(config, os.environ)
    result = await run_model_canary(registry, config.active_model)

    assert result.nonce
    assert result.tool_name == "incidentlens_canary"
    assert result.audit_nonce == result.nonce
    assert result.identity == registry.identity(config.active_model)
    assert result.fallback_used is False


@pytest.mark.live_llm
async def test_selected_profile_performs_conclusion_canary() -> None:
    """Test that the active profile can emit a valid RootCauseProposal.

    This canary binds only a synthetic proposal tool, requires a tool call,
    validates all proposal fields with Pydantic, uses synthetic Evidence IDs
    supplied in the prompt, and records only redacted identity and pass/fail
    metadata.

    This canary tests provider capability without asserting an incident root cause.
    """
    path = Path(os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml"))
    config = load_models_config(path, os.environ)
    profile = config.models[config.active_model]
    if profile.api_key_env not in os.environ:
        pytest.skip(f"missing {profile.api_key_env}")
    if not os.environ[profile.api_key_env].strip():
        pytest.fail(f"{profile.api_key_env} exists but is empty")

    registry = ModelRegistry(config, os.environ)
    result = await run_conclusion_canary(registry, config.active_model)

    assert result.root_service
    assert result.cause_code
    assert result.evidence_ids
    assert 0 <= result.confidence <= 1
    assert result.next_action in ("finish", "needs_more_evidence")
    assert result.identity == registry.identity(config.active_model)
    assert result.fallback_used is False
