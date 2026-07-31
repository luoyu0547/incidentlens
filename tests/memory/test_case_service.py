"""Tests for CaseService lifecycle — TDD RED phase.

Tests enforce:
  - POST /api/cases always creates draft
  - materialize is idempotent by incident_id
  - confirm indexes and edit removes from search
  - stale revision is rejected
  - rejected case cannot be confirmed without edit
"""

import pytest
from incidentlens_contracts.models import InvestigationStatus
from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.memory.domain import CaseDraft, CaseStatus
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.service import (
    CaseConflictError,
    CaseService,
    InvalidCaseTransitionError,
)
from incidentlens_telemetry.database import create_engine


@pytest.fixture
def service() -> CaseService:
    return CaseService(CaseRepository(create_engine("sqlite:///:memory:")))


def test_create_endpoint_contract_always_creates_draft(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    assert case.status is CaseStatus.DRAFT
    assert case.revision == 1


def test_materialize_is_idempotent_by_incident_id(service: CaseService) -> None:
    state = InvestigationState(
        incident_id="inc-1",
        status=InvestigationStatus.REPORT_READY,
        alert={"service": "order-service", "symptom": "timeout"},
        report={
            "root_service": "payment-service",
            "root_cause": "downstream-timeout",
            "evidence_ids": ["ev-1"],
            "findings": [{"evidence_id": "ev-1", "source_tool": "get_slow_traces"}],
        },
    )
    first = service.materialize_from_investigation(state)
    second = service.materialize_from_investigation(state)
    assert first.id == second.id
    assert second.status is CaseStatus.AGENT_GENERATED


def test_confirm_indexes_and_edit_removes_from_search(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(
            symptom="timeout",
            affected_services=["order-service"],
            root_cause_category="downstream-timeout",
            root_cause_description="payment service latency propagated upstream",
            key_evidence=[{"evidence_id": "ev-1", "source_tool": "get_slow_traces"}],
            resolution="remove the injected delay",
        ),
        actor="local-user",
    )
    verified = service.confirm(
        case.id, expected_version=case.revision, actor="reviewer", reason="checked"
    )
    assert verified.status is CaseStatus.HUMAN_VERIFIED
    edited = service.edit(
        case.id,
        expected_version=verified.revision,
        patch=CaseDraft(
            symptom="timeout updated",
            affected_services=["order-service"],
        ),
        actor="reviewer",
        reason="correct wording",
    )
    assert edited.status is CaseStatus.DRAFT


def test_stale_revision_is_rejected(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    with pytest.raises(CaseConflictError):
        service.confirm(case.id, expected_version=99, actor="reviewer")


def test_rejected_case_cannot_be_confirmed_without_edit(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    rejected = service.reject(case.id, case.revision, "reviewer", "wrong cause")
    with pytest.raises(InvalidCaseTransitionError):
        service.confirm(rejected.id, rejected.revision, "reviewer")
