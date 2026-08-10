from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.types import (
    EvidenceReference,
    InvestigationState,
    ProposedConclusion,
)


def state(**kwargs: object) -> InvestigationState:
    return InvestigationState(
        incident_id="inc-123",
        target_id="prod-a",
        service="orders",
        symptom="checkout requests are failing",
        **kwargs,
    )


def test_guard_stops_requests_when_tool_budget_is_exhausted() -> None:
    allowed, reason = InvestigationGuard().can_request_another_operation(
        state(tool_calls=16)
    )

    assert allowed is False
    assert reason == "tool-call budget exhausted"


def test_guard_rejects_a_conclusion_with_uncollected_evidence() -> None:
    allowed, reason = InvestigationGuard().validate_conclusion(
        state(),
        ProposedConclusion(summary="database is saturated", evidence_ids=("ev-fabricated",)),
    )

    assert allowed is False
    assert reason == "conclusion cites evidence not collected in this investigation"


def test_guard_accepts_a_conclusion_grounded_in_current_evidence() -> None:
    evidence = EvidenceReference(
        evidence_id="ev-1",
        operation_id="op-1",
        summary="orders container reports database pool timeout",
    )
    allowed, reason = InvestigationGuard().validate_conclusion(
        state(evidence=(evidence,)),
        ProposedConclusion(summary="database pool is exhausted", evidence_ids=("ev-1",)),
    )

    assert allowed is True
    assert reason == "conclusion is grounded in current evidence"
