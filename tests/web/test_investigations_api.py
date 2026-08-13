"""Tests for the investigation HTTP API."""

from __future__ import annotations

from datetime import UTC, datetime

from incidentlens_control_plane.investigation.fake_provider import StopStep
from incidentlens_control_plane.investigation.provider import Conclusion, StopSignal
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    Checkpoint,
    EvidenceReference,
    Hypothesis,
    HypothesisStatus,
    RegistryProposalStatus,
    RegistryUpdateKind,
    RegistryUpdateProposal,
    StopReason,
    ToolCall,
    ToolCallStatus,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope

NOW = datetime.now(UTC)

CREATE_PAYLOAD = {
    "project_id": "payments",
    "target_id": "dev-a",
    "service": "payment-api",
    "symptom": "checkout requests are failing",
}

SCOPE_PAYLOAD = {
    "project_id": "payments",
    "target_id": "dev-a",
    "scope": "host",
}


def _create(client, **overrides) -> dict:
    resp = client.post("/api/investigations", json={**CREATE_PAYLOAD, **overrides})
    assert resp.status_code == 201
    return resp.json()


def _host_scope() -> AgentScope:
    return AgentScope(project_id="payments", target_id="dev-a", scope=LogScope.HOST)


def _make_parent_run(
    runtime,
    investigation_id: str,
    *,
    run_id: str = "run-api-1",
    status: AgentRunStatus = AgentRunStatus.CREATED,
    stop_reason: StopReason | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_run_id=run_id,
        investigation_id=investigation_id,
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=_host_scope(),
        status=status,
        budget=AgentBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
        stop_reason=stop_reason,
    )
    runtime.investigation_store.create_agent_run(run)
    return run


def _script_completed_run(runtime, run_id: str, investigation_id: str) -> None:
    """Seed redacted evidence and a grounded COMPLETED stop for a run."""
    investigation = runtime.investigation_store.get_investigation(investigation_id)
    ref = runtime.evidence_service.record_validation_result(
        agent_run_id=run_id,
        incident_id=investigation.incident_id,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_ref="seed",
        validator="test",
        passed=True,
        detail="seed evidence for the run",
        created_by="test",
        now=NOW,
    )
    run = runtime.investigation_store.get_agent_run(run_id)
    run = run.model_copy(
        update={
            "evidence": (
                EvidenceReference(
                    evidence_id=ref.evidence_ref_id,
                    operation_id="seed",
                    summary="seed evidence",
                ),
            )
        }
    )
    runtime.investigation_store.update_agent_run(run)
    runtime.fake_provider.set_script(
        run_id,
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED, summary="investigation complete"
                ),
                conclusion=Conclusion(
                    summary="root cause identified",
                    evidence_ids=(ref.evidence_ref_id,),
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# create / list / get
# ---------------------------------------------------------------------------


def test_create_and_get_investigation(client) -> None:
    created = _create(client)
    assert created["status"] == "created"
    assert created["project_id"] == "payments"
    assert created["target_id"] == "dev-a"
    assert created["service"] == "payment-api"

    fetched = client.get(f"/api/investigations/{created['investigation_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["investigation_id"] == created["investigation_id"]
    assert fetched.json()["status"] == "created"


def test_create_rejects_unknown_fields(client) -> None:
    resp = client.post(
        "/api/investigations", json={**CREATE_PAYLOAD, "host": "dev-a.example.test"}
    )
    assert resp.status_code == 422


def test_create_rejects_credential_and_provider_secret_fields(client) -> None:
    for extra in ("ssh_key", "credential", "provider_secret"):
        resp = client.post("/api/investigations", json={**CREATE_PAYLOAD, extra: "x"})
        assert resp.status_code == 422, f"field {extra!r} must be rejected"


def test_list_investigations_filters_by_status_and_project(client) -> None:
    _create(client)
    _create(client, incident_id="inc-other")

    by_project = client.get(
        "/api/investigations", params={"project_id": "payments"}
    )
    assert by_project.status_code == 200
    assert len(by_project.json()) == 2

    by_status = client.get("/api/investigations", params={"status": "created"})
    assert by_status.status_code == 200
    assert all(item["status"] == "created" for item in by_status.json())

    by_incident = client.get("/api/investigations", params={"incident_id": "inc-other"})
    assert by_incident.status_code == 200
    assert len(by_incident.json()) == 1


def test_get_missing_investigation_returns_404(client) -> None:
    resp = client.get("/api/investigations/inv-missing")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# start / cancel / resume
# ---------------------------------------------------------------------------


def test_start_runs_parent_to_completion(client, runtime, registered_project) -> None:
    created = _create(client)
    _make_parent_run(runtime, created["investigation_id"], run_id="run-api-1")
    _script_completed_run(runtime, "run-api-1", created["investigation_id"])

    resp = client.post(
        f"/api/investigations/{created['investigation_id']}/start",
        json={"scope": SCOPE_PAYLOAD},
    )
    assert resp.status_code == 200
    run = resp.json()
    assert run["investigation_id"] == created["investigation_id"]
    assert run["status"] == "completed"
    assert run["stop_reason"] == "completed"

    fetched = client.get(f"/api/investigations/{created['investigation_id']}")
    assert fetched.json()["status"] == "completed"


def test_start_unknown_investigation_returns_404(client, registered_project) -> None:
    resp = client.post(
        "/api/investigations/inv-missing/start",
        json={"scope": SCOPE_PAYLOAD},
    )
    assert resp.status_code == 404


def test_start_requires_scope_and_rejects_extra_fields(client, registered_project) -> None:
    created = _create(client)
    resp = client.post(
        f"/api/investigations/{created['investigation_id']}/start", json={}
    )
    assert resp.status_code == 422
    resp = client.post(
        f"/api/investigations/{created['investigation_id']}/start",
        json={"scope": SCOPE_PAYLOAD, "host": "dev-a"},
    )
    assert resp.status_code == 422


def test_cancel_parks_created_investigation(client, runtime, registered_project) -> None:
    created = _create(client)
    resp = client.post(f"/api/investigations/{created['investigation_id']}/cancel")
    assert resp.status_code == 200
    # A never-started investigation is cancelled outright (CREATED -> CANCELLED).
    assert resp.json()["status"] == "cancelled"
    assert resp.json()["stop_reason"] == "cancelled"


def test_resume_paused_run(client, runtime, registered_project) -> None:
    created = _create(client)
    runtime.investigation_store.transition_investigation_status(
        created["investigation_id"], InvestigationStatus.RUNNING, now=NOW
    )
    runtime.investigation_store.transition_investigation_status(
        created["investigation_id"], InvestigationStatus.PAUSED_BUDGET, now=NOW
    )
    _make_parent_run(
        runtime,
        created["investigation_id"],
        run_id="run-api-1",
        status=AgentRunStatus.PAUSED_BUDGET,
        stop_reason=StopReason.BUDGET_ROUNDS,
    )
    _script_completed_run(runtime, "run-api-1", created["investigation_id"])

    resp = client.post(
        f"/api/investigations/{created['investigation_id']}/resume",
        json={"agent_run_id": "run-api-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


def test_resume_run_from_other_investigation_returns_404(client, runtime) -> None:
    created = _create(client)
    _make_parent_run(runtime, created["investigation_id"], run_id="run-api-1")
    other = _create(client)
    resp = client.post(
        f"/api/investigations/{other['investigation_id']}/resume",
        json={"agent_run_id": "run-api-1"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# run / entity queries
# ---------------------------------------------------------------------------


def _seed_entity_records(runtime, investigation_id: str, run_id: str) -> None:
    store = runtime.investigation_store
    now = NOW

    store.create_tool_call(
        ToolCall(
            tool_call_id="call-api-1",
            agent_run_id=run_id,
            tool_name="log_query",
            status=ToolCallStatus.SUCCEEDED,
            idempotency_key="call-api-1",
            planned_at=now,
            finished_at=now,
            output_bytes=42,
            evidence_ids=(),
        )
    )
    store.append_checkpoint(
        Checkpoint(
            checkpoint_id="cp-api-1",
            agent_run_id=run_id,
            sequence=1,
            status=AgentRunStatus.RUNNING,
            round_number=1,
            usage=UsageCounters(rounds=1, tool_calls=1),
            created_at=now,
        )
    )
    store.create_hypothesis(
        Hypothesis(
            hypothesis_id="hyp-api-1",
            agent_run_id=run_id,
            summary="db pool exhaustion",
            status=HypothesisStatus.PROPOSED,
            created_at=now,
            updated_at=now,
        )
    )
    store.create_conclusion(
        run_id,
        investigation_id,
        Conclusion(
            summary="root cause identified",
            facts=("checkout fails on high concurrency",),
            evidence_ids=(),
        ),
        now=now,
    )

    # A child run delegated by the parent run.
    store.create_agent_run(
        AgentRun(
            agent_run_id="child-api-1",
            investigation_id=investigation_id,
            parent_run_id=run_id,
            kind=AgentRunKind.CHILD,
            scope=_host_scope(),
            status=AgentRunStatus.COMPLETED,
            budget=AgentBudget(),
            usage=UsageCounters(rounds=1),
            created_at=now,
            updated_at=now,
            completed_at=now,
            stop_reason=StopReason.COMPLETED,
        )
    )


def test_run_and_entity_queries(client, runtime, registered_project) -> None:
    created = _create(client)
    _make_parent_run(runtime, created["investigation_id"], run_id="run-api-1")
    _seed_entity_records(runtime, created["investigation_id"], "run-api-1")
    inv_id = created["investigation_id"]

    runs = client.get(f"/api/investigations/{inv_id}/runs")
    assert runs.status_code == 200
    assert {run["agent_run_id"] for run in runs.json()} >= {
        "run-api-1",
        "child-api-1",
    }

    run = client.get(f"/api/investigations/{inv_id}/runs/run-api-1")
    assert run.status_code == 200
    assert run.json()["agent_run_id"] == "run-api-1"

    children = client.get(f"/api/investigations/{inv_id}/runs/run-api-1/children")
    assert children.status_code == 200
    assert {item["agent_run_id"] for item in children.json()} == {"child-api-1"}

    tool_calls = client.get(
        f"/api/investigations/{inv_id}/runs/run-api-1/tool-calls"
    )
    assert tool_calls.status_code == 200
    assert tool_calls.json()[0]["tool_call_id"] == "call-api-1"
    assert "arguments" not in tool_calls.text

    checkpoints = client.get(
        f"/api/investigations/{inv_id}/runs/run-api-1/checkpoints"
    )
    assert checkpoints.status_code == 200
    assert checkpoints.json()[0]["checkpoint_id"] == "cp-api-1"

    hypotheses = client.get(f"/api/investigations/{inv_id}/hypotheses")
    assert hypotheses.status_code == 200
    assert hypotheses.json()[0]["hypothesis_id"] == "hyp-api-1"

    conclusions = client.get(f"/api/investigations/{inv_id}/conclusions")
    assert conclusions.status_code == 200
    assert conclusions.json()[0]["summary"] == "root cause identified"

    missing_run = client.get(f"/api/investigations/{inv_id}/runs/nope")
    assert missing_run.status_code == 404


def test_proposals_and_evidence_queries(client, runtime, registered_project) -> None:
    created = _create(client)
    _make_parent_run(runtime, created["investigation_id"], run_id="run-api-1")
    inv_id = created["investigation_id"]
    now = NOW

    # A pending registry proposal.
    runtime.investigation_store.create_proposal(
        RegistryUpdateProposal(
            proposal_id="prop-api-1",
            investigation_id=inv_id,
            agent_run_id="run-api-1",
            kind=RegistryUpdateKind.CONTAINER_REGISTRATION,
            discovery_evidence_id="ev-discovery-1",
            proposed_project_id="payments",
            proposed_target_id="dev-a",
            proposed_service_name="payment-api",
            proposed_container_name="ghost-worker-1",
            status=RegistryProposalStatus.PENDING,
            created_at=now,
        )
    )

    proposals = client.get(f"/api/investigations/{inv_id}/proposals")
    assert proposals.status_code == 200
    assert proposals.json()[0]["proposal_id"] == "prop-api-1"

    # Evidence collected by the run, keyed by the investigation's incident id.
    investigation = runtime.investigation_store.get_investigation(inv_id)
    runtime.evidence_service.record_validation_result(
        agent_run_id="run-api-1",
        incident_id=investigation.incident_id,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_ref="probe",
        validator="test",
        passed=True,
        detail="probe result",
        created_by="agent",
        now=now,
    )
    evidence = client.get(f"/api/investigations/{inv_id}/evidence")
    assert evidence.status_code == 200
    assert any(
        item["source_ref"] == "probe" for item in evidence.json()
    )
    assert all("content_redacted" in item for item in evidence.json())
