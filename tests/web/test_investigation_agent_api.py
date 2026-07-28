"""Tests for async investigation API routes — TDD RED phase.

Tests cover:
  - POST /api/investigations/start awaits engine and exposes runtime identity
  - Secret redaction prevents model secrets from leaking into SSE/audit
  - Model timeout is reflected in error fields without being report_ready
"""

from __future__ import annotations

import json

from incidentlens_control_plane.agent.middleware import redact_sensitive_payload


async def test_start_awaits_engine_and_exposes_runtime_identity(
    agent_api_client,
) -> None:
    """Start endpoint must return mode, model_profile, and checkpoint ID."""
    response = await agent_api_client.post(
        "/api/investigations/start",
        json={"service": "order-service", "error_rate": 0.17},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["mode"] == "llm_agent"
    assert body["model_profile"] == "deepseek"
    assert body["last_checkpoint_id"]


def test_sse_and_audit_never_expose_model_secret() -> None:
    """Secret redaction must strip sensitive values from nested payloads."""
    secret = "super-secret-key"
    safe = redact_sensitive_payload(
        {
            "api_key": secret,
            "Authorization": f"Bearer {secret}",
            "nested": {"token": secret, "model": "deepseek-chat"},
        },
        secret_values={secret},
    )
    payload = json.dumps(safe)
    assert secret not in payload
    assert "Bearer" not in payload
    assert safe["nested"]["model"] == "deepseek-chat"


async def test_model_timeout_is_not_returned_as_success(
    agent_api_client,
    fake_agent_engine,
) -> None:
    """Model timeout error must be reflected without marking status as report_ready."""
    fake_agent_engine.state.last_error_code = "model_timeout"
    response = await agent_api_client.post("/api/investigations/inc-api/round")
    assert response.status_code == 200
    assert response.json()["last_error_code"] == "model_timeout"
    assert response.json()["status"] != "report_ready"
