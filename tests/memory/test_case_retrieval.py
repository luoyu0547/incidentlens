"""Tests for case memory and FTS retrieval — TDD RED phase.

Tests cover:
  - Search prefers human_verified cases
  - Only human_verified cases are indexed for FTS
  - Search returns candidates that produce unverified hypotheses
  - Historical cases can only generate candidate hypotheses
  - confirm() marks a case as human_verified
  - Non-verified cases are excluded from search
"""

from __future__ import annotations

import pytest
from incidentlens_telemetry.database import create_engine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def repository():
    """Create a CaseRepository backed by an in-memory SQLite DB."""
    from incidentlens_control_plane.memory.repository import CaseRepository

    engine = create_engine("sqlite:///:memory:")
    return CaseRepository(engine)


# ===================================================================
# CORE: Search prefers verified cases
# ===================================================================


class TestSearchPrefersVerified:
    """Core TDD test: search prefers human_verified cases."""

    def test_search_prefers_verified_case(self, repository) -> None:
        """Search should return human_verified cases first."""
        repository.save_case(
            status="human_verified",
            symptom="order timeout",
            service="order-service",
        )
        repository.save_case(
            status="auto_resolved",
            symptom="order timeout",
            service="order-service",
        )
        results = repository.search("timeout", "order-service", None)
        assert len(results) > 0
        assert results[0].status == "human_verified"


# ===================================================================
# FTS INDEXING: only human_verified cases
# ===================================================================


class TestFTSIndexing:
    """Tests for FTS indexing of only human_verified cases."""

    def test_only_verified_cases_in_search(self, repository) -> None:
        """Only human_verified cases should appear in search results."""
        repository.save_case(
            status="human_verified",
            symptom="payment delay",
            service="payment-service",
        )
        repository.save_case(
            status="auto_resolved",
            symptom="payment delay",
            service="payment-service",
        )
        results = repository.search("payment delay", "payment-service", None)
        for result in results:
            assert result.status == "human_verified"

    def test_unverified_case_not_in_search(self, repository) -> None:
        """Cases that are not human_verified should not appear in search."""
        repository.save_case(
            status="auto_resolved",
            symptom="database connection leak",
            service="order-service",
        )
        results = repository.search("database", "order-service", None)
        assert len(results) == 0


# ===================================================================
# CANDIDATE HYPOTHESES: search returns unverified hypotheses
# ===================================================================


class TestCandidateHypotheses:
    """Tests that search results produce candidate (unverified) hypotheses."""

    def test_search_returns_candidate_hypotheses(self, repository) -> None:
        """Search results should produce candidate hypotheses, not confirmed."""
        repository.save_case(
            status="human_verified",
            symptom="order timeout",
            service="order-service",
            root_cause="payment_latency_spike",
        )
        results = repository.search("timeout", "order-service", None)
        # Results should be usable as candidate hypotheses
        for result in results:
            # The result should have info to generate a hypothesis
            # but it should NOT be treated as confirmed
            assert hasattr(result, "status")
            assert result.status == "human_verified"

    def test_historical_cases_generate_candidates_only(self, repository) -> None:
        """Historical cases can only generate candidate hypotheses."""
        repository.save_case(
            status="human_verified",
            symptom="order timeout",
            service="order-service",
            root_cause="payment_latency_spike",
        )
        results = repository.search("timeout", "order-service", None)
        # Even though the case is human_verified, the search result
        # should only produce a candidate hypothesis for the current investigation
        for result in results:
            # The case's root_cause is available but should be used
            # as a candidate, not as a confirmed conclusion
            assert result.root_cause is not None or result.symptom is not None


# ===================================================================
# CONFIRM: mark case as human_verified
# ===================================================================


class TestConfirm:
    """Tests for the confirm() method."""

    def test_confirm_marks_case_verified(self, repository) -> None:
        """confirm() should mark a case as human_verified."""
        case_id = repository.save_case(
            status="pending_review",
            symptom="order timeout",
            service="order-service",
        )
        repository.confirm(case_id)
        # Now search should find it
        results = repository.search("timeout", "order-service", None)
        assert len(results) == 1
        assert results[0].status == "human_verified"

    def test_confirm_idempotent(self, repository) -> None:
        """Confirming an already verified case should be idempotent."""
        case_id = repository.save_case(
            status="human_verified",
            symptom="order timeout",
            service="order-service",
        )
        repository.confirm(case_id)
        results = repository.search("timeout", "order-service", None)
        assert len(results) == 1


# ===================================================================
# SEARCH: by symptom keyword and service
# ===================================================================


class TestSearch:
    """Tests for search functionality."""

    def test_search_by_keyword(self, repository) -> None:
        """Search should match on symptom keywords."""
        repository.save_case(
            status="human_verified",
            symptom="database connection pool exhaustion",
            service="order-service",
        )
        results = repository.search("connection pool", "order-service", None)
        assert len(results) == 1

    def test_search_by_service_filter(self, repository) -> None:
        """Search should filter by service."""
        repository.save_case(
            status="human_verified",
            symptom="timeout",
            service="order-service",
        )
        repository.save_case(
            status="human_verified",
            symptom="timeout",
            service="payment-service",
        )
        results = repository.search("timeout", "order-service", None)
        assert all(r.service == "order-service" for r in results)

    def test_search_no_results(self, repository) -> None:
        """Search with no matches should return empty list."""
        repository.save_case(
            status="human_verified",
            symptom="timeout",
            service="order-service",
        )
        results = repository.search("nonexistent", "order-service", None)
        assert len(results) == 0

    def test_search_by_root_cause(self, repository) -> None:
        """Search should also match on root_cause field."""
        repository.save_case(
            status="human_verified",
            symptom="order timeout",
            service="order-service",
            root_cause="payment_latency_spike",
        )
        results = repository.search("payment_latency", "order-service", None)
        assert len(results) == 1
