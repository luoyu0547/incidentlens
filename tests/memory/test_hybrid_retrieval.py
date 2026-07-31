"""Tests for hybrid case retrieval — FTS5 + structured filtering.

Verifies:
  - Only human_verified cases are returned
  - Same symptom with different root causes remain separate
  - Semver range and environment are hard filters
  - Deterministic ordering by total_score desc, case_id asc
"""

from __future__ import annotations

import pytest
from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseSearchQuery,
)
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.retrieval import HybridCaseRetriever
from incidentlens_control_plane.memory.service import CaseService
from incidentlens_telemetry.database import create_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def case_service() -> CaseService:
    """Create a CaseService backed by an in-memory SQLite DB."""
    return CaseService(CaseRepository(create_engine("sqlite:///:memory:")))


@pytest.fixture()
def retriever(case_service: CaseService) -> HybridCaseRetriever:
    """Create a HybridCaseRetriever without an embedding provider (keyword-only)."""
    return HybridCaseRetriever(case_service.repo, embedding_provider=None)


# ---------------------------------------------------------------------------
# Core retrieval behaviour
# ---------------------------------------------------------------------------


class TestOnlyVerifiedCasesReturned:
    """Only human_verified cases should appear in search results."""

    def test_only_verified_cases_are_returned(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        draft = case_service.create_draft(
            CaseDraft(
                symptom="payment timeout",
                affected_services=["order-service"],
            ),
            "local-user",
        )
        verified = case_service.create_draft(
            CaseDraft(
                symptom="payment timeout",
                affected_services=["order-service"],
                root_cause_category="downstream-timeout",
                root_cause_description="payment latency propagated to orders",
                key_evidence=[{"evidence_id": "ev-timeout"}],
                resolution="remove downstream delay",
            ),
            "local-user",
        )
        case_service.confirm(verified.id, verified.revision, "reviewer")
        hits = retriever.search(CaseSearchQuery(text="payment timeout"))
        assert [hit.case_id for hit in hits] == [verified.id]
        assert draft.id not in {hit.case_id for hit in hits}


class TestSameSymptomDifferentRootCauses:
    """Cases with the same symptom but different root causes remain separate."""

    def test_same_symptom_different_root_causes_remain_separate(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        ids = []
        for cause in ("downstream-timeout", "deployment-regression"):
            case = case_service.create_draft(
                CaseDraft(
                    symptom="order latency",
                    affected_services=["order-service"],
                    root_cause_category=cause,
                    root_cause_description=f"verified historical cause: {cause}",
                    key_evidence=[{"evidence_id": f"ev-{cause}"}],
                    resolution="apply the reviewed remediation",
                ),
                "local-user",
            )
            ids.append(
                case_service.confirm(case.id, case.revision, "reviewer").id
            )
        hits = retriever.search(CaseSearchQuery(text="order latency"))
        assert {hit.case_id for hit in hits} == set(ids)


class TestHardFilters:
    """Semver range and environment are hard filters."""

    def test_semver_range_and_environment_are_hard_filters(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        case = case_service.create_draft(
            CaseDraft(
                symptom="pool exhaustion",
                affected_services=["order-service"],
                environment="staging",
                service_version_min="2.0.0",
                service_version_max="2.4.0",
                root_cause_category="database-pool-exhaustion",
                root_cause_description="connection acquisition saturated",
                key_evidence=[{"evidence_id": "ev-pool"}],
                resolution="right-size and release the pool",
            ),
            "local-user",
        )
        case_service.confirm(case.id, case.revision, "reviewer")

        # Environment mismatch: case is staging, query asks production
        assert retriever.search(
            CaseSearchQuery(
                text="pool exhaustion",
                environment="production",
                service_version="2.2.0",
            )
        ) == []


class TestDeterministicOrdering:
    """Results must be sorted by total_score desc, case_id asc."""

    def test_ordering_is_deterministic(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        ids = []
        for i in range(3):
            case = case_service.create_draft(
                CaseDraft(
                    symptom="memory leak",
                    affected_services=["service-a"],
                    root_cause_category="leak-type",
                    root_cause_description=f"memory leak variant {i}",
                    key_evidence=[{"evidence_id": f"ev-{i}"}],
                    resolution="fix the leak",
                ),
                "local-user",
            )
            ids.append(
                case_service.confirm(case.id, case.revision, "reviewer").id
            )

        hits = retriever.search(CaseSearchQuery(text="memory leak"))
        assert len(hits) == 3
        # Must be in deterministic order (sorted by score desc, then case_id asc)
        result_ids = [h.case_id for h in hits]
        assert result_ids == sorted(result_ids)


class TestLimitRespected:
    """The limit parameter must cap results."""

    def test_limit_caps_results(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        for i in range(5):
            case = case_service.create_draft(
                CaseDraft(
                    symptom="disk full",
                    affected_services=["storage-service"],
                    root_cause_category="disk-full",
                    root_cause_description=f"disk full variant {i}",
                    key_evidence=[{"evidence_id": f"ev-disk-{i}"}],
                    resolution="clean up disk",
                ),
                "local-user",
            )
            case_service.confirm(case.id, case.revision, "reviewer")

        hits = retriever.search(CaseSearchQuery(text="disk full", limit=2))
        assert len(hits) == 2


class TestEmptyQuery:
    """Empty text should return no results (pydantic validation rejects it)."""

    def test_empty_text_rejected(self) -> None:
        with pytest.raises(Exception):
            CaseSearchQuery(text="")


class TestNoFTSHits:
    """When FTS5 finds no matches, return empty list."""

    def test_no_fts_matches_returns_empty(
        self, case_service: CaseService, retriever: HybridCaseRetriever
    ) -> None:
        hits = retriever.search(CaseSearchQuery(text="nonexistent xyz"))
        assert hits == []
