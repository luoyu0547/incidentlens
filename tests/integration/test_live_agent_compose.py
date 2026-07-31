"""Live Agent Compose tests — real model, real tools, real investigation.

These tests require:
  - Docker Compose services running with a configured LLM provider
  - A valid API key in the environment (XFYUN_MAAS_API_KEY, DEEPSEEK_API_KEY, etc.)
  - The INCIDENTLENS_AGENT_MODE=llm_agent environment variable

Run with:
  INCIDENTLENS_AGENT_MODE=llm_agent uv run pytest tests -vv -s
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from incidentlens_control_plane.llm.config import load_models_config, resolve_model_profile

CONTROL_PLANE_URL = os.environ.get("CONTROL_PLANE_URL", "http://localhost:8003")
GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def live_compose_urls() -> dict[str, str]:
    """Verify that the live Compose services are available."""
    config_path = Path(
        os.environ.get("INCIDENTLENS_MODELS_CONFIG", "config/models.yaml")
    )
    config = load_models_config(config_path, os.environ)
    key_name = config.models[config.active_model].api_key_env
    if key_name not in os.environ:
        pytest.skip(f"missing {key_name}")
    if not os.environ[key_name].strip():
        pytest.fail(f"{key_name} exists but is empty")
    resolve_model_profile(config, config.active_model, os.environ)
    return {
        "control_plane_url": CONTROL_PLANE_URL,
        "gateway_url": GATEWAY_URL,
    }


@pytest.mark.integration
@pytest.mark.live_llm
async def test_real_model_completes_payment_delay_investigation(
    live_compose_urls: dict[str, str],
) -> None:
    """One real-model investigation through Compose with downstream-timeout skill."""
    from incidentlens_demo.runner import DemoRunner

    runner = DemoRunner(
        control_plane_url=live_compose_urls["control_plane_url"],
        gateway_url=live_compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
        mode="llm_agent",
    )
    result = await runner.run("payment_delay")
    assert result.status == "passed", result.failure_message
    assert result.model_call_count > 0
    assert result.tool_call_count > 0
    assert "downstream-timeout" in result.loaded_skill_names
    assert result.report is not None
    assert result.report["root_service"] == "payment-service"
    assert result.fallback_used is False
    assert all(
        evidence_id.startswith("ev-")
        for evidence_id in result.report["evidence_ids"]
    )
