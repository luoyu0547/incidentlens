"""Hybrid case retrieval — FTS5 + optional semantic scoring.

Combines lexical (FTS5/BM25) and semantic (cosine similarity) signals
with structured filter matching to produce explainable ranked results.
Degrades to keyword-only when the embedding provider is unavailable.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from packaging.version import InvalidVersion, Version
from sqlalchemy import text

from incidentlens_control_plane.memory.domain import (
    CaseSearchHit,
    CaseSearchQuery,
    CaseSnapshot,
    CaseStatus,
)
from incidentlens_control_plane.memory.embedding import (
    EmbeddingProvider,
    EmbeddingUnavailableError,
    cosine_similarity,
)
from incidentlens_control_plane.memory.models import (
    CaseRow,
)
from incidentlens_control_plane.memory.repository import CaseRepository

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

HYBRID_WEIGHTS: dict[str, float] = {
    "lexical": 0.45,
    "semantic": 0.35,
    "filter": 0.15,
    "feedback": 0.05,
}

KEYWORD_WEIGHTS: dict[str, float] = {
    "lexical": 0.70,
    "filter": 0.25,
    "feedback": 0.05,
}

FTS_CANDIDATE_CAP = 20


# ---------------------------------------------------------------------------
# Version matching
# ---------------------------------------------------------------------------


def version_matches(case: CaseSnapshot, requested: str) -> bool:
    """Check whether *requested* version falls within the case's version constraints."""
    if not requested:
        return True
    if case.service_version_exact:
        return requested == case.service_version_exact
    if not case.service_version_min and not case.service_version_max:
        return True
    try:
        current = Version(requested.removeprefix("v"))
        lower = (
            Version(case.service_version_min.removeprefix("v"))
            if case.service_version_min
            else None
        )
        upper = (
            Version(case.service_version_max.removeprefix("v"))
            if case.service_version_max
            else None
        )
    except InvalidVersion:
        return False
    return (lower is None or current >= lower) and (upper is None or current <= upper)


# ---------------------------------------------------------------------------
# FTS query normalization
# ---------------------------------------------------------------------------


def _normalize_fts_query(raw: str) -> str:
    """Normalize user text into quoted tokens for FTS5 MATCH.

    Strips special FTS5 characters and wraps each word in double quotes
    so that MATCH treats them as exact tokens.
    """
    # Remove FTS5 special characters
    cleaned = re.sub(r'[":*(){}\[\]^~\\/]', " ", raw)
    tokens = cleaned.split()
    if not tokens:
        return '""'
    return " ".join(f'"{t}"' for t in tokens)


# ---------------------------------------------------------------------------
# Filter matching
# ---------------------------------------------------------------------------


def _service_matches(snapshot: CaseSnapshot, service: str | None) -> bool:
    if service is None:
        return True
    return service in snapshot.affected_services


def _category_matches(snapshot: CaseSnapshot, category: str | None) -> bool:
    if category is None:
        return True
    return snapshot.root_cause_category == category


def _environment_matches(snapshot: CaseSnapshot, environment: str | None) -> bool:
    if environment is None:
        return True
    return snapshot.environment == environment


def _version_filter_matches(snapshot: CaseSnapshot, version: str | None) -> bool:
    if version is None:
        return True
    return version_matches(snapshot, version)


def _applicability_check(snapshot: CaseSnapshot) -> bool:
    """Verify the case has no inapplicability conditions that would disqualify it.

    This is a basic sanity check — cases with empty conditions pass.
    """
    # Cases with explicit inapplicability conditions that match the query
    # context would be filtered here. For now, all verified cases pass.
    return True


# ---------------------------------------------------------------------------
# Row -> snapshot conversion
# ---------------------------------------------------------------------------


def _row_to_snapshot(row: CaseRow) -> CaseSnapshot:
    """Convert an ORM row to a CaseSnapshot read model."""
    affected = json.loads(row.affected_services_json) if row.affected_services_json else []
    evidence = json.loads(row.key_evidence_json) if row.key_evidence_json else []
    path = json.loads(row.investigation_path_json) if row.investigation_path_json else []
    invalid = json.loads(row.invalid_hypotheses_json) if row.invalid_hypotheses_json else []
    advice = json.loads(row.remediation_advice_json) if row.remediation_advice_json else []
    applic = json.loads(
        row.applicability_conditions_json
    ) if row.applicability_conditions_json else []
    inapplic = json.loads(
        row.inapplicability_conditions_json
    ) if row.inapplicability_conditions_json else []
    return CaseSnapshot(
        id=row.id,
        revision=row.revision,
        status=CaseStatus(row.status),
        incident_id=row.incident_id,
        source_reference=row.source_reference,
        symptom=row.symptom,
        affected_services=affected,
        root_cause_category=row.root_cause_category,
        root_cause_description=row.root_cause_description,
        key_evidence=evidence,
        investigation_path=path,
        invalid_hypotheses=invalid,
        resolution=row.resolution,
        remediation_advice=advice,
        applicability_conditions=applic,
        inapplicability_conditions=inapplic,
        environment=row.environment,
        service_version_exact=row.service_version_exact,
        service_version_min=row.service_version_min,
        service_version_max=row.service_version_max,
        source_report_json=row.source_report_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


# ---------------------------------------------------------------------------
# HybridCaseRetriever
# ---------------------------------------------------------------------------


@dataclass
class _FTSResult:
    """Raw FTS5 match result."""

    case_id: int
    bm25_rank: float


class HybridCaseRetriever:
    """Hybrid retriever combining FTS5 lexical search with optional semantic scoring.

    When the embedding provider is available, produces hybrid results with
    lexical, semantic, and filter scores. When unavailable, degrades to
    keyword-only with lexical + filter scoring.
    """

    def __init__(
        self,
        repository: CaseRepository,
        embedding_provider: EmbeddingProvider | None = None,
    ) -> None:
        self.repo = repository
        self.embedding = embedding_provider
        self.last_degradation_reason: str | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, query: CaseSearchQuery) -> list[CaseSearchHit]:
        """Execute hybrid search against verified cases.

        Returns a list of CaseSearchHit sorted by total_score descending,
        then case_id ascending for deterministic ordering.
        """
        self.last_degradation_reason = None

        # Step 1: FTS5 lexical search
        fts_results = self._fts_search(query)

        if not fts_results:
            return []

        # Step 2: Load case snapshots for FTS candidates
        candidates = self._load_candidates(fts_results)

        # Step 3: Apply structured filters
        filtered = self._apply_filters(candidates, query)

        if not filtered:
            return []

        # Step 4: Compute scores
        retrieval_mode, degradation_reason = self._determine_mode()
        if degradation_reason:
            self.last_degradation_reason = degradation_reason

        hits = self._score_and_rank(filtered, fts_results, query, retrieval_mode)

        return hits[:query.limit]

    # ------------------------------------------------------------------
    # FTS5 search
    # ------------------------------------------------------------------

    def _fts_search(self, query: CaseSearchQuery) -> list[_FTSResult]:
        """Search FTS5 index and return raw results with BM25 rank."""
        fts_query = _normalize_fts_query(query.text)
        results: list[_FTSResult] = []

        with self.repo.transaction() as session:
            rows = session.execute(
                text(
                    "SELECT case_id, bm25(case_fts) AS rank "
                    "FROM case_fts "
                    "WHERE case_fts MATCH :query "
                    "ORDER BY rank "
                    "LIMIT :cap"
                ),
                {"query": fts_query, "cap": FTS_CANDIDATE_CAP},
            ).fetchall()

            for row in rows:
                results.append(
                    _FTSResult(case_id=row[0], bm25_rank=row[1])
                )

        return results

    # ------------------------------------------------------------------
    # Candidate loading
    # ------------------------------------------------------------------

    def _load_candidates(
        self, fts_results: list[_FTSResult]
    ) -> dict[int, tuple[CaseSnapshot, float]]:
        """Load CaseSnapshot for each FTS candidate. Returns {case_id: (snapshot, bm25_rank)}."""
        case_ids = [r.case_id for r in fts_results]
        bm25_by_id = {r.case_id: r.bm25_rank for r in fts_results}

        candidates: dict[int, tuple[CaseSnapshot, float]] = {}

        with self.repo.transaction() as session:
            for case_id in case_ids:
                row = session.get(CaseRow, case_id)
                if row is not None and row.status == CaseStatus.HUMAN_VERIFIED:
                    candidates[case_id] = (_row_to_snapshot(row), bm25_by_id[case_id])

        return candidates

    # ------------------------------------------------------------------
    # Structured filter application
    # ------------------------------------------------------------------

    def _apply_filters(
        self,
        candidates: dict[int, tuple[CaseSnapshot, float]],
        query: CaseSearchQuery,
    ) -> dict[int, tuple[CaseSnapshot, float]]:
        """Apply hard structured filters. Removes non-matching candidates."""
        result: dict[int, tuple[CaseSnapshot, float]] = {}
        for case_id, (snapshot, bm25) in candidates.items():
            if not _service_matches(snapshot, query.service):
                continue
            if not _category_matches(snapshot, query.root_cause_category):
                continue
            if not _environment_matches(snapshot, query.environment):
                continue
            if not _version_filter_matches(snapshot, query.service_version):
                continue
            if not _applicability_check(snapshot):
                continue
            result[case_id] = (snapshot, bm25)
        return result

    # ------------------------------------------------------------------
    # Mode determination
    # ------------------------------------------------------------------

    def _determine_mode(self) -> tuple[str, str | None]:
        """Determine retrieval mode and degradation reason."""
        if self.embedding is None:
            return "keyword_only", "no_embedding_provider"

        # Try a test embedding to check availability
        try:
            self.embedding.embed_query("__probe__")
        except EmbeddingUnavailableError as exc:
            return "keyword_only", exc.reason

        return "hybrid", None

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_and_rank(
        self,
        candidates: dict[int, tuple[CaseSnapshot, float]],
        fts_results: list[_FTSResult],
        query: CaseSearchQuery,
        retrieval_mode: str,
    ) -> list[CaseSearchHit]:
        """Compute scores and build ranked hits."""
        if not candidates:
            return []

        # Normalize BM25 ranks to [0, 1] within the candidate set
        bm25_values = [bm25 for _, (_, bm25) in candidates.items()]
        min_bm25 = min(bm25_values) if bm25_values else 0.0
        max_bm25 = max(bm25_values) if bm25_values else 0.0
        bm25_range = max_bm25 - min_bm25

        # Get semantic embeddings if hybrid
        query_embedding: list[float] | None = None
        doc_embeddings: dict[int, list[float]] = {}
        if retrieval_mode == "hybrid" and self.embedding is not None:
            try:
                query_embedding = self.embedding.embed_query(query.text)
                doc_texts = [
                    f"{snap.symptom} {snap.root_cause_category} {snap.root_cause_description}"
                    for snap, _ in candidates.values()
                ]
                doc_embs = self.embedding.embed_documents(doc_texts)
                for case_id, emb in zip(candidates.keys(), doc_embs, strict=True):
                    doc_embeddings[case_id] = emb
            except (EmbeddingUnavailableError, Exception):
                retrieval_mode = "keyword_only"
                self.last_degradation_reason = "embedding_during_scoring"

        # Determine which weight scheme to use
        weights = HYBRID_WEIGHTS if retrieval_mode == "hybrid" else KEYWORD_WEIGHTS

        hits: list[CaseSearchHit] = []

        for case_id, (snapshot, bm25) in candidates.items():
            # Lexical score (normalized BM25)
            if bm25_range > 0:
                lexical_score = (bm25 - min_bm25) / bm25_range
            else:
                lexical_score = 1.0  # single candidate

            # Semantic score
            semantic_score = 0.0
            if (
                retrieval_mode == "hybrid"
                and query_embedding is not None
                and case_id in doc_embeddings
            ):
                try:
                    raw_sim = cosine_similarity(query_embedding, doc_embeddings[case_id])
                    # Map from [-1, 1] to [0, 1]
                    semantic_score = (raw_sim + 1.0) / 2.0
                except Exception:
                    semantic_score = 0.0

            # Filter score: count matching filter dimensions
            filter_score = self._compute_filter_score(snapshot, query)

            # Feedback score: placeholder — future enhancement
            feedback_score = 0.0

            # Total weighted score
            total_score = (
                weights["lexical"] * lexical_score
                + weights.get("semantic", 0.0) * semantic_score
                + weights["filter"] * filter_score
                + weights["feedback"] * feedback_score
            )

            # Build similarity reason
            reason = self._build_reason(
                snapshot, query, lexical_score, semantic_score, filter_score
            )

            hits.append(
                CaseSearchHit(
                    case_id=case_id,
                    case_snapshot=snapshot,
                    lexical_score=round(lexical_score, 4),
                    semantic_score=round(semantic_score, 4),
                    filter_score=round(filter_score, 4),
                    total_score=round(total_score, 4),
                    retrieval_mode=retrieval_mode,
                    similarity_reason=reason,
                )
            )

        # Sort by (-total_score, case_id) for deterministic ordering
        hits.sort(key=lambda h: (-h.total_score, h.case_id))
        return hits

    # ------------------------------------------------------------------
    # Filter scoring
    # ------------------------------------------------------------------

    def _compute_filter_score(
        self, snapshot: CaseSnapshot, query: CaseSearchQuery
    ) -> float:
        """Compute filter match score as fraction of active filters matched."""
        matches = 0
        total = 0

        if query.service is not None:
            total += 1
            if _service_matches(snapshot, query.service):
                matches += 1
        if query.root_cause_category is not None:
            total += 1
            if _category_matches(snapshot, query.root_cause_category):
                matches += 1
        if query.environment is not None:
            total += 1
            if _environment_matches(snapshot, query.environment):
                matches += 1
        if query.service_version is not None:
            total += 1
            if _version_filter_matches(snapshot, query.service_version):
                matches += 1

        return matches / total if total > 0 else 1.0

    # ------------------------------------------------------------------
    # Explanation building
    # ------------------------------------------------------------------

    def _build_reason(
        self,
        snapshot: CaseSnapshot,
        query: CaseSearchQuery,
        lexical_score: float,
        semantic_score: float,
        filter_score: float,
    ) -> str:
        """Build a human-readable similarity explanation."""
        parts: list[str] = []

        # Symptom match
        if query.text.lower() in snapshot.symptom.lower():
            parts.append(f'symptom matched "{query.text}"')

        # Service match
        if query.service and query.service in snapshot.affected_services:
            parts.append(f"service matched {query.service}")

        # Category match
        if query.root_cause_category:
            if snapshot.root_cause_category == query.root_cause_category:
                parts.append(f"category matched {query.root_cause_category}")

        # Version match
        if query.service_version:
            if snapshot.service_version_exact:
                if query.service_version == snapshot.service_version_exact:
                    parts.append(f"version {query.service_version} exact match")
            elif snapshot.service_version_min or snapshot.service_version_max:
                v_min = snapshot.service_version_min or "*"
                v_max = snapshot.service_version_max or "*"
                parts.append(
                    f"version {query.service_version} within [{v_min},{v_max}]"
                )

        # Environment match
        if query.environment and snapshot.environment == query.environment:
            parts.append(f"environment matched {query.environment}")

        # Semantic score
        if semantic_score > 0:
            parts.append(f"semantic score {semantic_score:.2f}")

        return "; ".join(parts) if parts else "matched by full-text search"
