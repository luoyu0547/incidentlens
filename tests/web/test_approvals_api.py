"""Tests for approval decision HTTP API."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from fastapi.testclient import TestClient
from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    RegistryProposalStatus,
    RegistryUpdateKind,
    StopReason,
    ToolCall,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope


def test_approval_decision_is_single_use(
    client: TestClient, pending_approval: str
) -> None:
    approved = client.post(f"/api/approvals/{pending_approval}/approve")
    repeated = client.post(f"/api/approvals/{pending_approval}/approve")
    assert approved.status_code == 200
    assert repeated.status_code == 409


def test_approval_reject_is_single_use(
    client: TestClient, pending_approval: str
) -> None:
    rejected = client.post(f"/api/approvals/{pending_approval}/reject")
    repeated = client.post(f"/api/approvals/{pending_approval}/reject")
    assert rejected.status_code == 200
    assert repeated.status_code == 409


def test_approvals_list_filters_by_status(
    client: TestClient, pending_approval: str
) -> None:
    response = client.get("/api/approvals", params={"status": "pending"})
    assert response.status_code == 200
    items = response.json()
    assert any(item["approval_id"] == pending_approval for item in items)
    assert all(item["status"] == "pending" for item in items)


def test_approval_view_excludes_canonical_intent(
    client: TestClient, pending_approval: str
) -> None:
    response = client.get("/api/approvals", params={"status": "pending"})
    assert response.status_code == 200
    assert "argv" not in response.text
    assert "intent_sha256" not in response.text


def test_approve_unknown_approval_returns_409(
    client: TestClient,
) -> None:
    response = client.post("/api/approvals/apr-missing/approve")
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# Investigation approval linkage (tool re-execution / resume)
# ---------------------------------------------------------------------------


def _waiting_investigation_stack(client, runtime, registered_project) -> dict:
    """Create a WAITING_APPROVAL run with one approval-gated tool call."""
    now = datetime.now(UTC)
    created = client.post(
        "/api/investigations",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service": "payment-api",
            "symptom": "checkout failures",
        },
    )
    assert created.status_code == 201
    inv_id = created.json()["investigation_id"]
    runtime.investigation_store.transition_investigation_status(
        inv_id, InvestigationStatus.RUNNING, now=now
    )
    runtime.investigation_store.transition_investigation_status(
        inv_id, InvestigationStatus.WAITING_APPROVAL, now=now
    )
    run = AgentRun(
        agent_run_id="run-ap-1",
        investigation_id=inv_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=AgentScope(project_id="payments", target_id="dev-a", scope=LogScope.HOST),
        status=AgentRunStatus.WAITING_APPROVAL,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
        stop_reason=StopReason.PENDING_APPROVAL,
    )
    runtime.investigation_store.create_agent_run(run)

    intent = {
        "kind": "shell",
        "target_id": "dev-a",
        "command": "pip install flask",
        "service": "payment-api",
    }
    approval = asyncio.run(runtime.approvals.request(intent))
    runtime.investigation_store.create_tool_call(
        ToolCall(
            tool_call_id="call-ap-1",
            agent_run_id="run-ap-1",
            tool_name="shell_exec",
            status=ToolCallStatus.WAITING_APPROVAL,
            idempotency_key="call-ap-1",
            planned_at=now,
            approval_id=approval.approval_id,
            arguments={
                "service_name": "payment-api",
                "command": "pip install flask",
                "timeout_seconds": 5,
            },
        )
    )
    return {"investigation_id": inv_id, "approval_id": approval.approval_id}


def test_approving_tool_approval_reexecutes_and_resumes(
    client: TestClient, runtime, registered_project: str
) -> None:
    stack = _waiting_investigation_stack(client, runtime, registered_project)

    response = client.post(f"/api/approvals/{stack['approval_id']}/approve")
    assert response.status_code == 200

    # The approved tool call was re-executed with the exact single-use approval.
    tool_call = runtime.investigation_store.get_tool_call("call-ap-1")
    assert tool_call.status is ToolCallStatus.FAILED
    assert "command exited" in (tool_call.error_redacted or "")
    approval = runtime.approvals.get(stack["approval_id"])
    assert approval.status is ApprovalStatus.CONSUMED
    # The run is no longer parked on the approval (it resumed and failed the
    # empty script, which is a terminal state, not WAITING_APPROVAL).
    run = runtime.investigation_store.get_agent_run("run-ap-1")
    assert run.status is not AgentRunStatus.WAITING_APPROVAL


def test_rejecting_tool_approval_cancels_the_call(
    client: TestClient, runtime, registered_project: str
) -> None:
    stack = _waiting_investigation_stack(client, runtime, registered_project)

    response = client.post(f"/api/approvals/{stack['approval_id']}/reject")
    assert response.status_code == 200

    tool_call = runtime.investigation_store.get_tool_call("call-ap-1")
    assert tool_call.status is ToolCallStatus.CANCELLED
    run = runtime.investigation_store.get_agent_run("run-ap-1")
    assert run.status is not AgentRunStatus.WAITING_APPROVAL


def test_approving_registry_proposal_decides_it(
    client: TestClient, runtime, registered_project: str
) -> None:
    now = datetime.now(UTC)
    created = client.post(
        "/api/investigations",
        json={
            "project_id": "payments",
            "target_id": "dev-a",
            "service": "payment-api",
            "symptom": "checkout failures",
        },
    )
    assert created.status_code == 201
    inv_id = created.json()["investigation_id"]
    run = AgentRun(
        agent_run_id="run-prop-1",
        investigation_id=inv_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=AgentScope(project_id="payments", target_id="dev-a", scope=LogScope.HOST),
        status=AgentRunStatus.CREATED,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=now,
        updated_at=now,
    )
    runtime.investigation_store.create_agent_run(run)

    proposed = asyncio.run(
        runtime.registry_proposals.propose(
            run,
            discovery_evidence_id="ev-discovery-1",
            kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
            service_name="payment-api",
            container="ghost-worker-1",
            now=now,
        )
    )
    created_event = runtime.events.list_after(0)
    assert any(
        event.event_type.value == "registry_proposal.created"
        for event in created_event
    )

    response = client.post(
        f"/api/approvals/{proposed.approval.approval_id}/approve"
    )
    assert response.status_code == 200

    stored = runtime.investigation_store.get_proposal(
        proposed.proposal.proposal_id
    )
    # The FakeTransport cannot confirm the container identity, so the proposal
    # is decided STALE rather than applied; it must no longer be pending.
    assert stored.status is not RegistryProposalStatus.PENDING
    assert runtime.approvals.get(proposed.approval.approval_id).status is (
        ApprovalStatus.CONSUMED
    )
    decided_events = [
        event
        for event in runtime.events.list_after(0)
        if event.event_type.value == "registry_proposal.decided"
    ]
    assert any(
        event.payload.get("proposal_id") == proposed.proposal.proposal_id
        for event in decided_events
    )
