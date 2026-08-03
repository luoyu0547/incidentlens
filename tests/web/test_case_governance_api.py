"""Tests for case governance API — TDD RED phase.

Covers:
  - POST /api/cases only creates draft (no client-selected status)
  - Edit-then-confirm uses revision tracking
  - Stale revision returns 409
  - Search exposes mode, scores, and reason
"""

from __future__ import annotations


async def _create_draft(client) -> dict:
    """Helper: create a draft case and return the response body."""
    response = await client.post(
        "/api/cases",
        json={
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "actor": "local-user",
        },
    )
    assert response.status_code == 201
    return response.json()


async def test_post_case_rejects_client_selected_status(case_api_client) -> None:
    """Clients must not be able to set status directly on creation."""
    response = await case_api_client.post(
        "/api/cases",
        json={
            "status": "human_verified",
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "actor": "local-user",
        },
    )
    assert response.status_code == 422


async def test_edit_then_confirm_uses_revision(case_api_client) -> None:
    """Edit increments revision, confirm uses the new revision."""
    created = await _create_draft(case_api_client)
    edited = await case_api_client.patch(
        f"/api/cases/{created['id']}",
        json={
            "expected_version": created["revision"],
            "actor": "reviewer",
            "reason": "correct root cause",
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "root_cause_category": "downstream-timeout",
            "root_cause_description": "payment latency propagated to orders",
            "key_evidence": [{"evidence_id": "ev-1"}],
            "resolution": "remove downstream delay",
        },
    )
    assert edited.status_code == 200
    confirmed = await case_api_client.post(
        f"/api/cases/{created['id']}/confirm",
        json={
            "expected_version": edited.json()["revision"],
            "actor": "reviewer",
            "reason": "evidence checked",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "human_verified"


async def test_stale_revision_is_409(case_api_client) -> None:
    """Optimistic lock violation returns 409 Conflict."""
    created = await _create_draft(case_api_client)
    response = await case_api_client.post(
        f"/api/cases/{created['id']}/reject",
        json={"expected_version": 99, "actor": "reviewer", "reason": "wrong"},
    )
    assert response.status_code == 409


async def test_search_exposes_mode_scores_and_reason(case_api_client) -> None:
    """Search results must include retrieval_mode, scores, and reason."""
    response = await case_api_client.get(
        "/api/cases/search",
        params={"q": "timeout", "service": "order-service"},
    )
    assert response.status_code == 200
    body = response.json()
    assert "results" in body
    # If results are returned, verify the structure
    if body["results"]:
        hit = body["results"][0]
        assert hit["retrieval_mode"] in {"hybrid", "keyword_only"}
        assert set(hit) >= {"lexical_score", "semantic_score", "similarity_reason"}


async def test_create_case_returns_201(case_api_client) -> None:
    """POST /api/cases returns 201 with id and revision."""
    response = await case_api_client.post(
        "/api/cases",
        json={
            "symptom": "memory leak",
            "affected_services": ["payment-service"],
            "actor": "local-user",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert "id" in body
    assert body["status"] == "draft"
    assert body["revision"] == 1


async def test_confirm_requires_required_fields(case_api_client) -> None:
    """Confirm on a draft with missing fields returns 422."""
    created = await _create_draft(case_api_client)
    response = await case_api_client.post(
        f"/api/cases/{created['id']}/confirm",
        json={
            "expected_version": created["revision"],
            "actor": "reviewer",
            "reason": "checking",
        },
    )
    assert response.status_code == 422


async def test_deprecate_from_verified(case_api_client) -> None:
    """Deprecate a human_verified case."""
    created = await _create_draft(case_api_client)
    # Edit with required fields
    edited = await case_api_client.patch(
        f"/api/cases/{created['id']}",
        json={
            "expected_version": created["revision"],
            "actor": "reviewer",
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "root_cause_category": "downstream-timeout",
            "root_cause_description": "payment latency",
            "key_evidence": [{"evidence_id": "ev-1"}],
            "resolution": "fix it",
        },
    )
    # Confirm
    await case_api_client.post(
        f"/api/cases/{created['id']}/confirm",
        json={
            "expected_version": edited.json()["revision"],
            "actor": "reviewer",
            "reason": "ok",
        },
    )
    # Get the verified case to get the latest revision
    verified = await case_api_client.get(f"/api/cases/{created['id']}")
    # Deprecate
    response = await case_api_client.post(
        f"/api/cases/{created['id']}/deprecate",
        json={
            "expected_version": verified.json()["revision"],
            "actor": "reviewer",
            "reason": "superseded",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "deprecated"


async def test_history_includes_feedback_context_and_usage_events(
    case_api_client, case_service
) -> None:
    """Governance history exposes review, feedback, and memory usage audit data."""
    from incidentlens_control_plane.memory.models import CaseUsageEventRow

    created = await _create_draft(case_api_client)
    with case_service.repo.transaction() as session:
        session.add(
            CaseUsageEventRow(
                case_id=created["id"],
                hypothesis_id="hyp-1",
                event_type="misleading",
                idempotency_key="inc-1:misleading",
                investigation_id="inc-1",
                details_json='{"accepted_evidence_ids":["ev-1"]}',
            )
        )

    feedback = await case_api_client.post(
        f"/api/cases/{created['id']}/feedback",
        json={
            "rating": "wrong",
            "actor": "reviewer",
            "incident_id": "inc-1",
            "comment": "current evidence contradicts this case",
            "idempotency_key": "inc-1:feedback",
        },
    )
    assert feedback.status_code == 201

    history = (
        await case_api_client.get(f"/api/cases/{created['id']}/history")
    ).json()
    assert history["feedback"][0]["actor"] == "reviewer"
    assert history["feedback"][0]["incident_id"] == "inc-1"
    assert history["usage_events"][0]["event_type"] == "misleading"
    assert history["usage_events"][0]["incident_id"] == "inc-1"
