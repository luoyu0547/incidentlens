"""CaseRepository — persist, search, and confirm investigation cases.

Public interface:
  - save_case(status, symptom, service, ...) -> int  (returns case_id)
  - search(keyword, service, root_cause) -> list[CaseSearchResult]
  - confirm(case_id) -> None

Design:
  - Only `human_verified` cases are indexed for FTS search
  - Search returns candidates that produce unverified hypotheses
  - Historical cases can only generate candidate hypotheses (never confirmed)
"""

from __future__ import annotations

import re

from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from incidentlens_control_plane.memory.models import CaseBase, CaseFTSRow, CaseRow

# ---------------------------------------------------------------------------
# Pydantic model for search results
# ---------------------------------------------------------------------------


class CaseSearchResult(BaseModel):
    """A case search result returned by CaseRepository.search()."""

    id: int
    status: str
    symptom: str
    service: str
    root_cause: str
    resolution: str = ""
    evidence_summary: str = ""


# ---------------------------------------------------------------------------
# CaseRepository
# ---------------------------------------------------------------------------


class CaseRepository:
    """Repository for managing investigation cases with FTS search.

    Only `human_verified` cases are indexed for search.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        CaseBase.metadata.create_all(engine)

    def save_case(
        self,
        *,
        status: str = "pending_review",
        symptom: str = "",
        service: str = "",
        root_cause: str = "",
        resolution: str = "",
        evidence_summary: str = "",
    ) -> int:
        """Save a new case and return its ID.

        If the case status is `human_verified`, it will be indexed for FTS search.
        """
        with Session(self._engine) as session:
            row = CaseRow(
                status=status,
                symptom=symptom,
                service=service,
                root_cause=root_cause,
                resolution=resolution,
                evidence_summary=evidence_summary,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            case_id = row.id

        # Index for FTS if human_verified
        if status == "human_verified":
            self._index_for_fts(case_id, symptom, service, root_cause)

        return case_id

    def search(
        self,
        keyword: str,
        service: str | None = None,
        root_cause: str | None = None,
    ) -> list[CaseSearchResult]:
        """Search for human_verified cases matching the keyword.

        Only returns `human_verified` cases.
        Search matches on symptom and root_cause fields.
        Results are ranked by relevance (exact match > partial match).
        """
        keyword_lower = keyword.lower().strip()
        if not keyword_lower:
            return []

        with Session(self._engine) as session:
            # Use the FTS index for efficient search
            # Only return human_verified cases
            stmt = (
                select(CaseFTSRow)
                .where(CaseFTSRow.keyword.contains(keyword_lower))
            )
            if service:
                stmt = stmt.where(CaseFTSRow.service == service)

            fts_rows = list(session.scalars(stmt))

            if not fts_rows:
                # Fallback: search directly in case_memory table
                stmt2 = select(CaseRow).where(CaseRow.status == "human_verified")
                if service:
                    stmt2 = stmt2.where(CaseRow.service == service)
                all_cases = list(session.scalars(stmt2))

                results = []
                for case in all_cases:
                    searchable = f"{case.symptom} {case.root_cause}".lower()
                    if keyword_lower in searchable:
                        results.append(
                            CaseSearchResult(
                                id=case.id,
                                status=case.status,
                                symptom=case.symptom,
                                service=case.service,
                                root_cause=case.root_cause,
                                resolution=case.resolution,
                                evidence_summary=case.evidence_summary,
                            )
                        )
                return results

            # Get case IDs from FTS results
            case_ids = list({row.case_id for row in fts_rows})

            # Load full cases
            stmt3 = select(CaseRow).where(
                CaseRow.id.in_(case_ids),
                CaseRow.status == "human_verified",
            )
            if service:
                stmt3 = stmt3.where(CaseRow.service == service)

            cases = list(session.scalars(stmt3))

            # Rank by relevance
            results = []
            for case in cases:
                searchable = f"{case.symptom} {case.root_cause}".lower()
                # Exact match in root_cause gets higher rank
                is_exact = keyword_lower in (case.root_cause or "").lower()
                results.append(
                    (
                        not is_exact,  # False (exact) sorts before True
                        CaseSearchResult(
                            id=case.id,
                            status=case.status,
                            symptom=case.symptom,
                            service=case.service,
                            root_cause=case.root_cause,
                            resolution=case.resolution,
                            evidence_summary=case.evidence_summary,
                        ),
                    )
                )

            results.sort(key=lambda x: x[0])
            return [r[1] for r in results]

    def confirm(self, case_id: int) -> None:
        """Mark a case as human_verified and index it for FTS search."""
        with Session(self._engine) as session:
            stmt = select(CaseRow).where(CaseRow.id == case_id)
            case = session.scalars(stmt).first()
            if case is None:
                raise ValueError(f"Case not found: {case_id}")

            case.status = "human_verified"
            session.commit()
            session.refresh(case)

            # Index for FTS
            self._index_for_fts(
                case.id, case.symptom, case.service, case.root_cause
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _index_for_fts(
        self,
        case_id: int,
        symptom: str,
        service: str,
        root_cause: str,
    ) -> None:
        """Index a case for full-text search.

        Extracts keywords from symptom and root_cause fields
        and stores them in the FTS index table.
        """
        # Extract keywords from text
        text = f"{symptom} {root_cause}".lower()
        keywords = self._extract_keywords(text)

        with Session(self._engine) as session:
            # Remove existing FTS entries for this case
            session.query(CaseFTSRow).filter(
                CaseFTSRow.case_id == case_id
            ).delete()

            # Add new FTS entries
            for keyword in keywords:
                session.add(
                    CaseFTSRow(
                        case_id=case_id,
                        keyword=keyword,
                        service=service,
                    )
                )
            session.commit()

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """Extract searchable keywords from text.

        Splits on non-alphanumeric characters, removes short words,
        and returns unique tokens plus some bigrams.
        """
        # Split into tokens
        tokens = re.findall(r"[a-z0-9_]+", text.lower())
        # Remove very short tokens
        tokens = [t for t in tokens if len(t) >= 2]
        # Add bigrams for multi-word matching
        bigrams = []
        for i in range(len(tokens) - 1):
            bigrams.append(f"{tokens[i]} {tokens[i+1]}")
        # Return unique keywords
        return list(set(tokens + bigrams))
