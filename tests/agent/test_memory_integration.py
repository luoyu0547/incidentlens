"""Tests for InvestigationMemoryCoordinator — the bridge between agent runtimes and governed memory.

Verifies:
  - prepare() creates candidate hypotheses with traceable IDs and usage events
  - finalize() classifies priors as validated or misleading
  - Usage counts and list are correct
  - Idempotency: repeated operations produce no duplicates
  - Materialization failure is caught and recorded
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from incidentlens_contracts.models import (
    HypothesisStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.agent.state import (
    InvestigationAuditStore,
    InvestigationState,
)
from incidentlens_control_plane.memory.domain import (
    CaseSearchHit,
    CaseSearchQuery,
    CaseSnapshot,
    CaseStatus,
    UsageEventType,
)
from incidentlens_control_plane.memory.integration import (
    InvestigationMemoryCoordinator,
    historical_hypothesis_id,
)
from incidentlens_control_plane.memory.models import CaseUsageEventRow
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.service import CaseService
from sqlalchemy import create_engine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_snapshot(
    *,
    case_id: int = 1,
    symptom: str = "payment timeout",
    root_cause_category: str = "downstream-timeout",
    affected_services: list[str] | None = None,
) -> CaseSnapshot:
    """Create a minimal CaseSnapshot for testing."""
    return CaseSnapshot(
        id=case_id,
        revision=1,
        status=CaseStatus.HUMAN_VERIFIED,
        symptom=symptom,
        affected_services=affected_services or ["order-service"],
        root_cause_category=root_cause_category,
        root_cause_description=root_cause_category,
        created_at=datetime.now(tz=timezone.utc),
        updated_at=datetime.now(tz=timezone.utc),
    )


def _make_search_hit(
    *,
    case_id: int = 1,
    snapshot: CaseSnapshot | None = None,
    total_score: float = 0.8,
    retrieval_mode: str = "keyword_only",
    similarity_reason: str = "symptom matched",
) -> CaseSearchHit:
    """Create a minimal CaseSearchHit for testing."""
    snap = snapshot or _make_snapshot(case_id=case_id)
    return CaseSearchHit(
        case_id=case_id,
        case_snapshot=snap,
        lexical_score=0.7,
        semantic_score=0.5,
        filter_score=1.0,
        total_score=total_score,
        retrieval_mode=retrieval_mode,
        similarity_reason=similarity_reason,
    )


class FakeRetriever:
    """Fake HybridCaseRetriever that returns predetermined hits."""

    def __init__(self, hits: list[CaseSearchHit] | None = None) -> None:
        self._hits = hits or []
        self.last_degradation_reason: str | None = None
        self.last_query: CaseSearchQuery | None = None

    def search(self, query: CaseSearchQuery) -> list[CaseSearchHit]:
        self.last_query = query
        return self._hits


def _report_ready_state(
    *,
    incident_id: str = "inc-current",
    root_cause: str = "downstream-timeout",
    evidence_ids: list[str] | None = None,
) -> InvestigationState:
    """Create a report_ready InvestigationState for testing finalize."""
    return InvestigationState(
        incident_id=incident_id,
        status=InvestigationStatus.REPORT_READY,
        current_round=3,
        alert={"service": "order-service", "symptom": "payment timeout"},
        report={
            "root_service": "order-service",
            "root_cause": root_cause,
            "evidence_ids": evidence_ids or ["ev-1", "ev-2"],
            "findings": [],
        },
        phase="generate_report",
    )


def _needs_more_evidence_state(
    incident_id: str = "inc-current",
) -> InvestigationState:
    """Create a needs_more_evidence InvestigationState."""
    return InvestigationState(
        incident_id=incident_id,
        status=InvestigationStatus.NEEDS_MORE_EVIDENCE,
        current_round=8,
        alert={"service": "order-service", "symptom": "payment timeout"},
        phase="verify_root_cause",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine():
    """In-memory SQLite engine for testing."""
    eng = create_engine("sqlite:///:memory:")
    return eng


@pytest.fixture
def repository(engine) -> CaseRepository:
    return CaseRepository(engine)


@pytest.fixture
def case_service(repository) -> CaseService:
    return CaseService(repository)


@pytest.fixture
def audit_store(engine) -> InvestigationAuditStore:
    return InvestigationAuditStore(engine)


@pytest.fixture
def coordinator(repository, case_service, audit_store) -> InvestigationMemoryCoordinator:
    retriever = FakeRetriever(hits=[])
    return InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )


# ---------------------------------------------------------------------------
# Tests: prepare()
# ---------------------------------------------------------------------------


def test_prepare_turns_each_hit_into_a_candidate_with_traceable_id(
    repository, case_service, audit_store
) -> None:
    """Each search hit becomes a candidate hypothesis with a deterministic ID,
    and usage events are recorded for recalled and adopted."""
    snapshot = _make_snapshot(case_id=42, root_cause_category="downstream-timeout")
    hit = _make_search_hit(case_id=42, snapshot=snapshot)
    retriever = FakeRetriever(hits=[hit])
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    prepared = coordinator.prepare(
        "inc-current",
        {"service": "order-service", "symptom": "payment timeout"},
    )

    assert prepared.retrieved_cases
    case = prepared.retrieved_cases[0]
    hypothesis = prepared.hypotheses[0]
    assert case["hypothesis_id"] == hypothesis.id
    assert hypothesis.status == HypothesisStatus.ACTIVE
    assert hypothesis.confidence == 0.3
    assert coordinator.usage_counts("inc-current") == {
        "recalled": 1,
        "adopted": 1,
        "validated": 0,
        "misleading": 0,
    }
    with repository.transaction() as session:
        persisted = [
            row.event_type
            for row in (
                session.query(CaseUsageEventRow)
                .filter(CaseUsageEventRow.investigation_id == "inc-current")
                .all()
            )
        ]
    assert persisted == ["recalled", "adopted"]
    assert [event.event_type.value for event in case_service.list_usage("inc-current")] == [
        "recalled",
        "adopted",
    ]


def test_prepare_returns_empty_for_empty_alert(repository, case_service, audit_store) -> None:
    """Empty alert produces no hits, no hypotheses, no events."""
    retriever = FakeRetriever(hits=[])
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )
    prepared = coordinator.prepare("inc-empty", {})
    assert prepared.retrieved_cases == []
    assert prepared.hypotheses == []


def test_prepare_does_not_overconstrain_symptom_with_numeric_alert_signal(
    repository, case_service, audit_store
) -> None:
    retriever = FakeRetriever()
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    coordinator.prepare(
        "inc-current",
        {
            "service": "payment-service",
            "symptom": "Elevated payment latency",
            "error_rate": 1.0,
        },
    )

    assert retriever.last_query is not None
    assert retriever.last_query.text == "Elevated payment latency"


def test_prepare_deterministic_hypothesis_ids(repository, case_service, audit_store) -> None:
    """Hypothesis IDs are deterministic UUID5, not random."""
    hyp_id_1 = historical_hypothesis_id("inc-1", 42)
    hyp_id_2 = historical_hypothesis_id("inc-1", 42)
    assert hyp_id_1 == hyp_id_2
    # Different inputs produce different IDs
    hyp_id_3 = historical_hypothesis_id("inc-2", 42)
    assert hyp_id_1 != hyp_id_3
    hyp_id_4 = historical_hypothesis_id("inc-1", 99)
    assert hyp_id_1 != hyp_id_4


# ---------------------------------------------------------------------------
# Tests: finalize()
# ---------------------------------------------------------------------------


def test_guarded_terminal_report_validates_matching_prior(
    repository, case_service, audit_store
) -> None:
    """When report root cause matches a prior's category, it is validated."""
    snapshot = _make_snapshot(case_id=1, root_cause_category="downstream-timeout")
    hit = _make_search_hit(case_id=1, snapshot=snapshot)
    retriever = FakeRetriever(hits=[hit])
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    # First prepare to populate retrieved_cases
    coordinator.prepare(
        "inc-current",
        {"service": "order-service", "symptom": "payment timeout"},
    )

    state = _report_ready_state(root_cause="downstream-timeout")
    state.retrieved_cases = [
        {
            "case_id": 1,
            "root_cause_category": "downstream-timeout",
            "hypothesis_id": historical_hypothesis_id("inc-current", 1),
        }
    ]

    finalized = coordinator.finalize(state)

    assert finalized.case_id is not None
    assert finalized.case_status == "agent_generated"
    assert coordinator.usage_counts(state.incident_id)["validated"] == 1


def test_guarded_different_cause_marks_adopted_prior_misleading(
    repository, case_service, audit_store
) -> None:
    """When report root cause differs from a prior's category, it is misleading
    and the accepted evidence IDs are recorded in event details."""
    snapshot = _make_snapshot(case_id=1, root_cause_category="downstream-timeout")
    hit = _make_search_hit(case_id=1, snapshot=snapshot)
    retriever = FakeRetriever(hits=[hit])
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    # First prepare
    coordinator.prepare(
        "inc-current",
        {"service": "order-service", "symptom": "payment timeout"},
    )

    state = _report_ready_state(root_cause="deployment-regression")
    state.retrieved_cases = [
        {
            "case_id": 1,
            "root_cause_category": "downstream-timeout",
            "hypothesis_id": historical_hypothesis_id("inc-current", 1),
        }
    ]

    coordinator.finalize(state)

    events = coordinator.list_usage(state.incident_id)
    misleading = [event for event in events if event.event_type == UsageEventType.MISLEADING]
    assert len(misleading) >= 1
    assert misleading[0].details["accepted_evidence_ids"] == state.report["evidence_ids"]


def test_finalize_unchanged_for_non_terminal(repository, case_service, audit_store) -> None:
    """Non-terminal states pass through unchanged."""
    coordinator = InvestigationMemoryCoordinator(
        retriever=FakeRetriever(),
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )
    state = InvestigationState(
        incident_id="inc-1",
        status=InvestigationStatus.INVESTIGATING,
        alert={"service": "order-service"},
    )
    result = coordinator.finalize(state)
    assert result is state
    assert result.case_id is None


def test_finalize_unchanged_for_needs_more_evidence(repository, case_service, audit_store) -> None:
    """needs_more_evidence states pass through unchanged."""
    coordinator = InvestigationMemoryCoordinator(
        retriever=FakeRetriever(),
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )
    state = _needs_more_evidence_state()
    result = coordinator.finalize(state)
    assert result is state
    assert result.case_id is None


def test_finalize_materialization_failure_returns_unchanged(repository, audit_store) -> None:
    """A materialization error is caught, recorded, and the state is unchanged."""

    # Create a CaseService with a broken materialization (non-report_ready state)
    # We'll use a mock that raises on materialize
    class BrokenCaseService:
        def materialize_from_investigation(self, state):
            raise RuntimeError("storage unavailable")

    coordinator = InvestigationMemoryCoordinator(
        retriever=FakeRetriever(),
        case_service=BrokenCaseService(),  # type: ignore[arg-type]
        repository=repository,
        audit_store=audit_store,
    )

    state = _report_ready_state()
    state.retrieved_cases = [
        {
            "case_id": 1,
            "root_cause_category": "downstream-timeout",
            "hypothesis_id": "hist-1",
        }
    ]

    result = coordinator.finalize(state)

    # State should be unchanged
    assert result.case_id is None
    assert result.case_status is None

    # Audit entry should exist
    audits = audit_store.list_for_incident("inc-current", "case_materialization_failed")
    assert len(audits) == 1
    assert audits[0]["details"]["error_code"] == "case_storage_error"


# ---------------------------------------------------------------------------
# Tests: idempotency
# ---------------------------------------------------------------------------


def test_repeated_finalize_produces_one_case_and_one_terminal_event(
    repository, case_service, audit_store
) -> None:
    """Repeated finalize calls on the same report_ready state produce
    exactly one case and one terminal usage event per prior."""
    snapshot = _make_snapshot(case_id=1, root_cause_category="downstream-timeout")
    hit = _make_search_hit(case_id=1, snapshot=snapshot)
    retriever = FakeRetriever(hits=[hit])
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    state = _report_ready_state()
    state.retrieved_cases = [
        {
            "case_id": 1,
            "root_cause_category": "downstream-timeout",
            "hypothesis_id": historical_hypothesis_id("inc-current", 1),
        }
    ]

    # First finalize
    result1 = coordinator.finalize(state)
    case_id_1 = result1.case_id

    # Second finalize with the same state
    result2 = coordinator.finalize(state)
    case_id_2 = result2.case_id

    # Should be the same case (idempotent materialization)
    assert case_id_1 == case_id_2

    # Terminal events should not duplicate
    validated = [
        e for e in coordinator.list_usage("inc-current") if e.event_type == UsageEventType.VALIDATED
    ]
    assert len(validated) == 1


# ---------------------------------------------------------------------------
# Tests: degradation recording
# ---------------------------------------------------------------------------


def test_prepare_records_degradation_when_retriever_reports_reason(
    repository, case_service, audit_store
) -> None:
    """When the retriever reports a degradation reason, it is recorded as an audit entry."""
    retriever = FakeRetriever(hits=[])
    retriever.last_degradation_reason = "no_embedding_provider"
    coordinator = InvestigationMemoryCoordinator(
        retriever=retriever,
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    coordinator.prepare(
        "inc-degraded",
        {"service": "order-service", "symptom": "timeout"},
    )

    audits = audit_store.list_for_incident("inc-degraded", "memory_retrieval_degraded")
    assert len(audits) == 1
    assert audits[0]["details"]["reason_code"] == "no_embedding_provider"


# ---------------------------------------------------------------------------
# Tests: needs_more_evidence never creates a case
# ---------------------------------------------------------------------------


def test_needs_more_evidence_never_creates_case(repository, case_service, audit_store) -> None:
    """A needs_more_evidence terminal state never materializes a case."""
    coordinator = InvestigationMemoryCoordinator(
        retriever=FakeRetriever(),
        case_service=case_service,
        repository=repository,
        audit_store=audit_store,
    )

    state = _needs_more_evidence_state()
    result = coordinator.finalize(state)

    assert result.case_id is None
    assert result.case_status is None
    assert coordinator.usage_counts("inc-current") == {
        "recalled": 0,
        "adopted": 0,
        "validated": 0,
        "misleading": 0,
    }
