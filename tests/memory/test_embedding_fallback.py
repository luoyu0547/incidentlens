"""Tests for embedding fallback and hybrid scoring behaviour.

Verifies:
  - Hybrid result exposes component scores (lexical, semantic, filter)
  - Embedding failure degrades to keyword_only
  - DisabledEmbeddingProvider always raises EmbeddingUnavailableError
  - cosine_similarity handles dimension mismatch
  - cosine_similarity returns 0.0 for zero vectors
"""

from __future__ import annotations

import pytest
from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseSearchQuery,
)
from incidentlens_control_plane.memory.embedding import (
    DisabledEmbeddingProvider,
    EmbeddingDimensionError,
    EmbeddingIdentity,
    EmbeddingUnavailableError,
    cosine_similarity,
)
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.retrieval import HybridCaseRetriever
from incidentlens_control_plane.memory.service import CaseService
from incidentlens_telemetry.database import create_engine

# ---------------------------------------------------------------------------
# Fake embedding providers
# ---------------------------------------------------------------------------


class FakeEmbeddingProvider:
    """Deterministic fake for testing hybrid scoring."""

    identity = EmbeddingIdentity(provider="fake", model="fake-v1", dimension=2)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "timeout" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    """Embedding provider that always fails on embed_query."""

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailableError("provider_timeout")


class ProbeFailingProvider(FakeEmbeddingProvider):
    """Embedding provider that fails on the probe call (first call only)."""

    def __init__(self) -> None:
        self._call_count = 0

    def embed_query(self, text: str) -> list[float]:
        self._call_count += 1
        if text == "__probe__":
            raise EmbeddingUnavailableError("probe_failed")
        return [1.0, 0.0]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def case_service() -> CaseService:
    return CaseService(CaseRepository(create_engine("sqlite:///:memory:")))


@pytest.fixture()
def keyword_case_repository(case_service: CaseService) -> CaseRepository:
    return case_service.repo


@pytest.fixture()
def hybrid_retriever(case_service: CaseService) -> HybridCaseRetriever:
    """Retriever with a working fake embedding provider."""
    return HybridCaseRetriever(case_service.repo, embedding_provider=FakeEmbeddingProvider())


@pytest.fixture()
def verified_case(case_service: CaseService):
    """A verified case with 'timeout' in the symptom."""
    case = case_service.create_draft(
        CaseDraft(
            symptom="payment timeout",
            affected_services=["order-service"],
            root_cause_category="downstream-timeout",
            root_cause_description="payment latency propagated",
            key_evidence=[{"evidence_id": "ev-1"}],
            resolution="remove delay",
        ),
        "local-user",
    )
    return case_service.confirm(case.id, case.revision, "reviewer")


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    """Tests for the cosine_similarity helper."""

    def test_identical_vectors_return_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)

    def test_orthogonal_vectors_return_zero(self) -> None:
        assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)

    def test_opposite_vectors_return_negative_one(self) -> None:
        assert cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(-1.0)

    def test_dimension_mismatch_raises(self) -> None:
        with pytest.raises(EmbeddingDimensionError):
            cosine_similarity([1.0, 0.0], [1.0, 0.0, 0.0])

    def test_zero_vectors_return_zero(self) -> None:
        assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


# ---------------------------------------------------------------------------
# DisabledEmbeddingProvider
# ---------------------------------------------------------------------------


class TestDisabledEmbeddingProvider:
    """DisabledEmbeddingProvider always raises EmbeddingUnavailableError."""

    def test_embed_documents_raises(self) -> None:
        provider = DisabledEmbeddingProvider()
        with pytest.raises(EmbeddingUnavailableError, match="embedding_not_configured"):
            provider.embed_documents(["test"])

    def test_embed_query_raises(self) -> None:
        provider = DisabledEmbeddingProvider()
        with pytest.raises(EmbeddingUnavailableError, match="embedding_not_configured"):
            provider.embed_query("test")

    def test_identity_is_disabled(self) -> None:
        provider = DisabledEmbeddingProvider()
        assert provider.identity.provider == "disabled"
        assert provider.identity.dimension == 0


# ---------------------------------------------------------------------------
# Hybrid result component scores
# ---------------------------------------------------------------------------


class TestHybridComponentScores:
    """Hybrid results must expose individual component scores."""

    def test_hybrid_result_exposes_component_scores(
        self,
        hybrid_retriever: HybridCaseRetriever,
        verified_case,
    ) -> None:
        hits = hybrid_retriever.search(CaseSearchQuery(text="timeout"))
        assert len(hits) >= 1
        hit = hits[0]
        assert hit.case_id == verified_case.id
        assert hit.retrieval_mode == "hybrid"
        assert 0 <= hit.lexical_score <= 1
        assert 0 <= hit.semantic_score <= 1
        assert 0 <= hit.filter_score <= 1
        assert hit.total_score > 0
        assert hit.similarity_reason


# ---------------------------------------------------------------------------
# Embedding failure degradation
# ---------------------------------------------------------------------------


class TestEmbeddingFallback:
    """Embedding failures must degrade to keyword_only gracefully."""

    def test_embedding_failure_returns_keyword_only(
        self, keyword_case_repository: CaseRepository, verified_case
    ) -> None:
        retriever = HybridCaseRetriever(
            keyword_case_repository,
            FailingEmbeddingProvider(),
        )
        hits = retriever.search(CaseSearchQuery(text="timeout"))
        assert hits
        assert all(hit.retrieval_mode == "keyword_only" for hit in hits)
        assert retriever.last_degradation_reason == "provider_timeout"

    def test_no_embedding_provider_is_keyword_only(
        self, keyword_case_repository: CaseRepository, verified_case
    ) -> None:
        retriever = HybridCaseRetriever(
            keyword_case_repository,
            embedding_provider=None,
        )
        hits = retriever.search(CaseSearchQuery(text="timeout"))
        assert hits
        assert all(hit.retrieval_mode == "keyword_only" for hit in hits)
        assert retriever.last_degradation_reason == "no_embedding_provider"


class TestFilterScoring:
    """Filter score reflects how many active filters matched."""

    def test_filter_score_perfect_when_no_filters(
        self, hybrid_retriever: HybridCaseRetriever, verified_case
    ) -> None:
        hits = hybrid_retriever.search(CaseSearchQuery(text="timeout"))
        assert hits[0].filter_score == 1.0

    def test_filter_score_with_matching_service(
        self, hybrid_retriever: HybridCaseRetriever, verified_case
    ) -> None:
        hits = hybrid_retriever.search(
            CaseSearchQuery(text="timeout", service="order-service")
        )
        assert len(hits) >= 1
        assert hits[0].filter_score == 1.0

    def test_filter_score_zero_with_nonmatching_service(
        self, hybrid_retriever: HybridCaseRetriever, verified_case
    ) -> None:
        hits = hybrid_retriever.search(
            CaseSearchQuery(text="timeout", service="other-service")
        )
        assert hits == []
