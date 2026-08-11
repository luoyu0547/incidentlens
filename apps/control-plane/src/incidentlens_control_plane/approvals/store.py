"""SQLite-backed approval store with single-use consumption guarantee."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from incidentlens_control_plane.approvals.types import ApprovalRecord, ApprovalStatus


class ApprovalNotFound(Exception):
    """Raised when an approval record is not found."""


class ApprovalUnavailable(Exception):
    """Raised when an approval cannot be consumed (expired, rejected, or already consumed)."""


def canonical_intent(intent: Mapping[str, object]) -> bytes:
    """Canonicalize an intent mapping to sorted compact JSON bytes."""
    return json.dumps(
        intent,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def intent_sha256(intent: Mapping[str, object]) -> str:
    """Compute the SHA-256 hex digest of the canonical intent."""
    return hashlib.sha256(canonical_intent(intent)).hexdigest()


DEFAULT_TTL_MINUTES = 15


class ApprovalStore:
    """SQLite-backed store for exact, single-use approvals."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the approvals table if it doesn't exist."""
        with self._connection_factory() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    intent_sha256 TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    intent_summary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    consumed_at TEXT
                )
                """
            )
            conn.commit()

    def create_request(
        self,
        approval_id: str,
        intent_sha256: str,
        intent: Mapping[str, object],
        intent_summary: str,
        now: datetime,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
    ) -> ApprovalRecord:
        """Create a new approval request in PENDING status."""
        created_at = now.astimezone(UTC)
        expires_at = created_at + timedelta(minutes=ttl_minutes)
        record = ApprovalRecord(
            approval_id=approval_id,
            intent_sha256=intent_sha256,
            intent=dict(intent),
            intent_summary=intent_summary,
            status=ApprovalStatus.PENDING,
            created_at=created_at,
            expires_at=expires_at,
        )

        with self._connection_factory() as conn:
            conn.execute(
                """
                INSERT INTO approvals
                    (approval_id, intent_sha256, intent_json, intent_summary,
                     status, created_at, expires_at, decided_at, consumed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.approval_id,
                    record.intent_sha256,
                    json.dumps(record.intent),
                    record.intent_summary,
                    record.status.value,
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                    None,
                    None,
                ),
            )
            conn.commit()

        return record

    def get(self, approval_id: str) -> ApprovalRecord | None:
        """Retrieve an approval record by ID."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                SELECT approval_id, intent_sha256, intent_json, intent_summary,
                       status, created_at, expires_at, decided_at, consumed_at
                FROM approvals WHERE approval_id = ?
                """,
                (approval_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return ApprovalRecord(
            approval_id=row[0],
            intent_sha256=row[1],
            intent=json.loads(row[2]),
            intent_summary=row[3],
            status=ApprovalStatus(row[4]),
            created_at=datetime.fromisoformat(row[5]),
            expires_at=datetime.fromisoformat(row[6]),
            decided_at=datetime.fromisoformat(row[7]) if row[7] else None,
            consumed_at=datetime.fromisoformat(row[8]) if row[8] else None,
        )

    def approve(self, approval_id: str, now: datetime) -> ApprovalRecord:
        """Transition an approval to APPROVED status."""
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?
                WHERE approval_id = ? AND status = ?
                """,
                (
                    ApprovalStatus.APPROVED.value,
                    now_utc.isoformat(),
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            conn.commit()

            if cursor.rowcount == 0:
                raise ApprovalNotFound(
                    f"Approval '{approval_id}' not found or not in pending status"
                )

        return self.get(approval_id)  # type: ignore[return-value]

    def reject(self, approval_id: str, now: datetime) -> ApprovalRecord:
        """Transition an approval to REJECTED status."""
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?
                WHERE approval_id = ? AND status = ?
                """,
                (
                    ApprovalStatus.REJECTED.value,
                    now_utc.isoformat(),
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            conn.commit()

            if cursor.rowcount == 0:
                raise ApprovalNotFound(
                    f"Approval '{approval_id}' not found or not in pending status"
                )

        return self.get(approval_id)  # type: ignore[return-value]

    def consume(self, approval_id: str, now: datetime) -> ApprovalRecord:
        """Atomically consume an approved approval (single-use guarantee).

        Uses a transaction with WHERE clause to guarantee only one successful
        consumption per approval ID.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, consumed_at = ?
                WHERE approval_id = ?
                  AND status = ?
                  AND consumed_at IS NULL
                  AND expires_at > ?
                """,
                (
                    ApprovalStatus.CONSUMED.value,
                    now_utc.isoformat(),
                    approval_id,
                    ApprovalStatus.APPROVED.value,
                    now_utc.isoformat(),
                ),
            )
            conn.commit()

            if cursor.rowcount == 0:
                raise ApprovalUnavailable(
                    f"Approval '{approval_id}' cannot be consumed:"
                    " not approved, already consumed, or expired"
                )

        return self.get(approval_id)  # type: ignore[return-value]
