"""CaseRepository — session-based persistence for the governed case schema.

Public interface:
  - transaction() -> Iterator[Session]
  - add_case(session, row) -> CaseRow
  - get_case(session, case_id) -> CaseRow | None
  - get_by_incident(session, incident_id) -> CaseRow | None
  - add_review(session, row) -> None
  - add_feedback(session, row) -> None
  - add_usage_event(session, row) -> None
  - replace_fts(session, case) -> None
  - remove_fts(session, case_id) -> None

Only ``human_verified`` cases are present in the FTS5 index.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from incidentlens_control_plane.memory.migrations import migrate_case_schema
from incidentlens_control_plane.memory.models import (
    CaseFeedbackRow,
    CaseReviewActionRow,
    CaseRow,
    CaseUsageEventRow,
)


class CaseRepository:
    """Repository for managing the case memory aggregate.

    All mutations go through :meth:`transaction` which yields a SQLAlchemy
    ``Session`` inside a ``BEGIN`` block.  Callers must not commit or roll
    back themselves -- the context manager handles it.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        # Ensure the governed schema exists before any queries.
        migrate_case_schema(engine)

    # ------------------------------------------------------------------
    # Transaction management
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        """Yield a Session inside a transactional block.

        On normal exit the transaction is committed; on exception it is
        rolled back and the exception re-raised.
        """
        with Session(self.engine) as session:
            with session.begin():
                yield session

    # ------------------------------------------------------------------
    # Case CRUD
    # ------------------------------------------------------------------

    def add_case(self, session: Session, row: CaseRow) -> CaseRow:
        """Insert a new case row and return it (with ``id`` populated)."""
        session.add(row)
        session.flush()  # populate row.id without committing
        return row

    def get_case(self, session: Session, case_id: int) -> CaseRow | None:
        """Load a case by primary key, or ``None``."""
        return session.get(CaseRow, case_id)

    def get_by_incident(self, session: Session, incident_id: str) -> CaseRow | None:
        """Load a case by its ``incident_id``, or ``None``."""
        return (
            session.query(CaseRow)
            .filter(CaseRow.incident_id == incident_id)
            .first()
        )

    # ------------------------------------------------------------------
    # Audit / governance tables
    # ------------------------------------------------------------------

    def add_review(self, session: Session, row: CaseReviewActionRow) -> None:
        """Append a review action record."""
        session.add(row)

    def add_feedback(self, session: Session, row: CaseFeedbackRow) -> None:
        """Insert a feedback record (unique on ``idempotency_key``)."""
        session.add(row)

    def add_usage_event(self, session: Session, row: CaseUsageEventRow) -> None:
        """Insert a usage event (unique on ``idempotency_key``)."""
        session.add(row)

    # ------------------------------------------------------------------
    # FTS5 management
    # ------------------------------------------------------------------

    def replace_fts(self, session: Session, case: CaseRow) -> None:
        """Replace the FTS5 index entry for *case*.

        Only ``human_verified`` cases are indexed.  Any prior FTS row for
        this ``case_id`` is deleted first (contentless_delete=1 allows
        ``DELETE`` on a contentless table).
        """
        # Remove existing entry if any
        self.remove_fts(session, case.id)

        if case.status != "human_verified":
            return

        affected = case.affected_services_json or "[]"
        session.execute(
            text(
                "INSERT INTO case_fts("
                "case_id, symptom, affected_services, "
                "root_cause_category, root_cause_description"
                ") VALUES ("
                ":case_id, :symptom, :affected, :rc_cat, :rc_desc"
                ")"
            ),
            {
                "case_id": case.id,
                "symptom": case.symptom,
                "affected": affected,
                "rc_cat": case.root_cause_category,
                "rc_desc": case.root_cause_description,
            },
        )

    def remove_fts(self, session: Session, case_id: int) -> None:
        """Remove the FTS5 index entry for *case_id* (if any)."""
        session.execute(
            text("DELETE FROM case_fts WHERE case_id = :case_id"),
            {"case_id": case_id},
        )
