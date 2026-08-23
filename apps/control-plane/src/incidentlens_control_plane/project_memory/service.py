"""Deterministic safety validation and advisory selection for Project Memory.

The service is the only admission point for extracted Project Memory.  Each entry
must be verified and evidence-backed: empty provenance, evidence outside the
investigation's owned set, an unverified-hypothesis kind, secret-like values,
oversized fact fields and project identity unrelated to the source investigation
are all rejected with a stable, specific message before anything is persisted.
Service names are normalized (lowercased, whitespace-stripped, deduplicated) and
a re-confirmation of an already-active fact supersedes the older active row
instead of duplicating it.

Selection is deterministic and bounded.  ``render_relevant`` ranks ACTIVE entries
by (a) exact normalized service overlap with the query scope, then (b) symptom
term overlap against the bounded fact text, then (c) recency of
``last_confirmed_at``, and returns at most five entries rendered with their
provenance and the advisory marker.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from incidentlens_control_plane.investigation.types import Investigation
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryEntry,
    ProjectMemoryKind,
    ProjectMemoryRejected,
)

MAX_FACT_LENGTH = 2_000
MAX_SERVICE_NAME_LENGTH = 120
_MAX_RANK_CANDIDATES = 500
_MAX_SELECTED = 100

# API-key-shaped / credential patterns.  Every match rejects the entry; the
# patterns intentionally stay conservative so a verified fact is never lost.
_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|client[_-]?secret|secret|passwd|password|credential)\s*[:=]\s*\S+"),
    re.compile(r"(?i)\b(bearer|authorization)\b\s*[:=]?\s*[a-zA-Z0-9\-_.]{8,}"),
    re.compile(r"(?i)-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(sk|pk)(?:_|-)[a-zA-Z0-9]{16,}\b"),
)


def _normalize_service_names(services: Iterable[str]) -> tuple[str, ...]:
    """Normalize service names: strip, lowercase, drop blanks, dedupe in order."""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in services:
        name = raw.strip().lower()
        if name and name not in seen:
            seen.add(name)
            normalized.append(name)
    return tuple(normalized)


def _tokenize(text: str) -> frozenset[str]:
    """Return the set of lowercase alphanumeric terms in ``text``."""
    return frozenset(re.findall(r"[a-z0-9]+", text.lower()))


def _contains_secret(text: str) -> bool:
    return any(pattern.search(text) is not None for pattern in _SECRET_PATTERNS)


class ProjectMemoryService:
    """Validates, persists and selects project-scoped Project Memory records."""

    def __init__(self, store: ProjectMemoryStore) -> None:
        self._store = store

    @property
    def store(self) -> ProjectMemoryStore:
        return self._store

    # -- admission ------------------------------------------------------------

    def accept_extracted(
        self,
        entries: Iterable[ProjectMemoryEntry],
        investigation: Investigation,
        owned_evidence_ids: Iterable[str],
    ) -> tuple[ProjectMemoryEntry, ...]:
        """Validate an extraction batch atomically, then persist ACTIVE entries.

        ``owned_evidence_ids`` is the set of evidence ref ids collected by
        ``investigation``; every cited evidence id must be inside it.  When any
        entry fails a rule the whole batch is rejected with the first stable
        message and nothing is persisted.  An already-active entry for the same
        normalized (project, services, fact) is superseded rather than
        duplicated.
        """
        normalized = tuple(self._normalize_entry(entry) for entry in entries)
        owned = frozenset(owned_evidence_ids)
        for entry in normalized:
            self._validate(entry, investigation, owned)
        return tuple(
            self._persist(entry, investigation) for entry in normalized
        )

    def _normalize_entry(self, entry: ProjectMemoryEntry) -> ProjectMemoryEntry:
        """Return a validated entry with normalized, bounded service names."""
        normalized = _normalize_service_names(entry.service_names)[:32]
        return ProjectMemoryEntry.model_validate(
            {**entry.model_dump(), "service_names": normalized}
        )

    def _validate(
        self,
        entry: ProjectMemoryEntry,
        investigation: Investigation,
        owned_evidence_ids: frozenset[str],
    ) -> None:
        if not entry.source_investigation_id.strip() or not entry.evidence_ids:
            raise ProjectMemoryRejected(
                f"empty provenance: memory {entry.memory_id} has no source "
                "investigation or cited evidence"
            )
        if entry.kind is ProjectMemoryKind.UNVERIFIED_HYPOTHESIS:
            raise ProjectMemoryRejected(
                f"unverified hypothesis kind: memory {entry.memory_id} is not "
                "a verified outcome"
            )
        if entry.project_id != investigation.project_id:
            raise ProjectMemoryRejected(
                f"unrelated project identity: memory {entry.memory_id} targets "
                f"{entry.project_id}, not investigation project "
                f"{investigation.project_id}"
            )
        if any(len(name) > MAX_SERVICE_NAME_LENGTH for name in entry.service_names):
            raise ProjectMemoryRejected(
                f"oversized service name: memory {entry.memory_id} exceeds "
                f"{MAX_SERVICE_NAME_LENGTH} characters"
            )
        if _contains_secret(entry.fact):
            raise ProjectMemoryRejected(
                f"secret-like value: memory {entry.memory_id} fact looks like "
                "a credential"
            )
        if len(entry.fact) > MAX_FACT_LENGTH:
            raise ProjectMemoryRejected(
                f"oversized fact: memory {entry.memory_id} fact exceeds "
                f"{MAX_FACT_LENGTH} characters"
            )
        foreign = sorted(set(entry.evidence_ids) - owned_evidence_ids)
        if foreign:
            raise ProjectMemoryRejected(
                f"foreign evidence: memory {entry.memory_id} cites evidence "
                "not owned by the investigation"
            )

    def _persist(
        self, entry: ProjectMemoryEntry, investigation: Investigation
    ) -> ProjectMemoryEntry:
        """Persist one validated entry; supersede an already-active duplicate."""
        existing = self._find_active_duplicate(entry, investigation.project_id)
        if existing is not None:
            self._store.supersede(existing.memory_id)
        return self._store.upsert(entry)

    def _find_active_duplicate(
        self, entry: ProjectMemoryEntry, project_id: str
    ) -> ProjectMemoryEntry | None:
        """Return the ACTIVE record with the same (project, services, fact)."""
        signature = (project_id, frozenset(entry.service_names), entry.fact)
        for candidate in self._store.list_active(project_id, limit=_MAX_RANK_CANDIDATES):
            candidate_signature = (
                candidate.project_id,
                frozenset(candidate.service_names),
                candidate.fact,
            )
            if candidate_signature == signature:
                return candidate
        return None

    # -- bounded deterministic fallback selection ----------------------------

    def select_relevant(
        self,
        project_id: str,
        symptom: str,
        services: Iterable[str],
        limit: int = 5,
    ) -> tuple[ProjectMemoryEntry, ...]:
        """Rank ACTIVE memories for a project by relevance and recency.

        Sort key is (exact service overlap, symptom term overlap,
        ``last_confirmed_at`` desc, ``memory_id``) so the order is fully
        deterministic.  Returns at most ``limit`` entries.
        """
        if not 1 <= limit <= _MAX_SELECTED:
            raise ValueError("limit must be between 1 and 100")
        query_services = frozenset(_normalize_service_names(services))
        symptom_terms = _tokenize(symptom)

        def score(entry: ProjectMemoryEntry) -> tuple[int, int, float, str]:
            service_overlap = len(frozenset(entry.service_names) & query_services)
            fact_terms = _tokenize(entry.fact)
            term_overlap = len(symptom_terms & fact_terms)
            return (
                -service_overlap,
                -term_overlap,
                -entry.last_confirmed_at.timestamp(),
                entry.memory_id,
            )

        ranked = sorted(
            self._store.list_active(project_id, limit=_MAX_RANK_CANDIDATES),
            key=score,
        )
        return tuple(ranked[:limit])

    def render_relevant(
        self,
        project_id: str,
        symptom: str,
        services: Iterable[str],
        limit: int = 5,
    ) -> str:
        """Render the top relevant memories as a bounded advisory attachment.

        The text keeps explicit provenance (source investigation, evidence)
        and the literal advisory marker; empty when nothing is selected.
        """
        selected = self.select_relevant(project_id, symptom, services, limit=limit)
        if not selected:
            return ""
        lines = ["Project memory (advisory; revalidate current environment.)", ""]
        for entry in selected:
            services_text = (
                ", ".join(entry.service_names)
                if entry.service_names
                else "any service"
            )
            evidence_text = ", ".join(entry.evidence_ids)
            lines.append(
                f"- {entry.kind.value}: {entry.fact} (services: {services_text}) "
                f"[source investigation {entry.source_investigation_id}, "
                f"evidence {evidence_text}; last confirmed "
                f"{entry.last_confirmed_at.isoformat()}]"
            )
        return "\n".join(lines)
