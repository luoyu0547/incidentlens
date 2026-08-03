"""Integration tests for Phase 5 memory governance flow.

Validates the complete knowledge loop:
  - Investigation produces cases
  - Cases are reviewed and confirmed
  - Memory guides subsequent investigations
  - Misleading cases are detected
  - Feedback is recorded
  - Export includes evidence references
  - Reset scopes work correctly
"""

from __future__ import annotations

import httpx
import pytest
from incidentlens_demo.runner import DemoRunner

pytestmark = pytest.mark.integration


async def test_investigation_review_retrieval_feedback_and_export(
    compose_urls: dict[str, str],
) -> None:
    """Full governance flow: investigation -> review -> memory -> feedback -> export."""
    # Step 1: Reset with full scope
    async with httpx.AsyncClient(base_url=compose_urls["control_plane_url"]) as setup_client:
        reset = await setup_client.post("/api/scenarios/reset", params={"scope": "full"})
        assert reset.status_code == 200

    # Step 2: Run investigation
    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
        reset_scope="incident",
    )
    first = await runner.run("payment_delay")
    assert first.status == "passed"

    # Step 3: Review and confirm the case
    async with httpx.AsyncClient(base_url=compose_urls["control_plane_url"]) as client:
        cases = (await client.get("/api/cases", params={"incident_id": first.incident_id})).json()[
            "cases"
        ]
        generated = cases[0]
        assert generated["status"] == "agent_generated"

        reviewed = await client.patch(
            f"/api/cases/{generated['id']}",
            json={
                "expected_version": generated["revision"],
                "actor": "compose-reviewer",
                "reason": "add reviewed remediation",
                "symptom": generated["symptom"],
                "affected_services": generated["affected_services"],
                "root_cause_category": generated["root_cause_category"],
                "root_cause_description": generated["root_cause_description"],
                "key_evidence": generated["key_evidence"],
                "resolution": "remove the injected payment delay",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "draft"

        confirmed = await client.post(
            f"/api/cases/{generated['id']}/confirm",
            json={
                "expected_version": reviewed.json()["revision"],
                "actor": "compose-reviewer",
                "reason": "accepted current evidence",
            },
        )
        assert confirmed.json()["status"] == "human_verified"

        # Step 4: Create a contradictory prior case
        wrong = await client.post(
            "/api/cases",
            json={
                "symptom": generated["symptom"],
                "affected_services": ["payment-service"],
                "root_cause_category": "deployment-regression",
                "root_cause_description": "legacy deployment hypothesis",
                "key_evidence": [
                    {
                        "source_tool": "legacy-review",
                        "content": {"summary": "deployment preceded similar symptoms"},
                    }
                ],
                "resolution": "roll back the deployment",
                "actor": "compose-reviewer",
            },
        )
        wrong_verified = await client.post(
            f"/api/cases/{wrong.json()['id']}/confirm",
            json={
                "expected_version": wrong.json()["revision"],
                "actor": "compose-reviewer",
                "reason": "seed contradictory prior",
            },
        )
        assert wrong_verified.json()["status"] == "human_verified"

        recalled = await client.get(
            "/api/cases/search",
            params={
                "q": generated["symptom"],
                "service": "payment-service",
            },
        )
        assert recalled.status_code == 200
        assert wrong.json()["id"] in {hit["case_id"] for hit in recalled.json()["results"]}, (
            recalled.json()
        )

        # Step 5: Run investigation again - memory should detect misleading case
        memory_runner = DemoRunner(
            control_plane_url=compose_urls["control_plane_url"],
            gateway_url=compose_urls["gateway_url"],
            traffic_count=5,
            compose=True,
            reset_scope="incident",
            cleanup_after_run=False,
        )
        second = await memory_runner.run("payment_delay")
        assert second.status == "passed", second

        # Step 6: Check usage events for misleading detection
        history = (await client.get(f"/api/cases/{wrong.json()['id']}/history")).json()
        assert any(
            event["event_type"] == "misleading" and event["incident_id"] == second.incident_id
            for event in history["usage_events"]
        ), history

        # Step 7: Submit feedback
        feedback = await client.post(
            f"/api/cases/{wrong.json()['id']}/feedback",
            json={
                "rating": "wrong",
                "actor": "compose-reviewer",
                "comment": "current evidence supports downstream timeout",
                "incident_id": second.incident_id,
                "idempotency_key": f"{second.incident_id}:wrong-feedback",
            },
        )
        assert feedback.status_code == 201

        # Step 8: Export investigation
        export = await client.get(f"/api/investigations/{second.incident_id}/export")
        assert export.status_code == 200
        assert export.json()["investigation"]["report"]["evidence_ids"]
        # Ensure no root_cause_label in export
        assert "root_cause_label" not in export.text

        # Step 9: Cleanup with incident scope
        cleanup = await client.post("/api/scenarios/reset", params={"scope": "incident"})
        assert cleanup.status_code == 200
