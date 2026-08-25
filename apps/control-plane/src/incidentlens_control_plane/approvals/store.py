"""SQLite-backed approval store with single-use consumption guarantee."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta

from incidentlens_control_plane.approvals.types import (
    ApprovalDownstreamStatus,
    ApprovalRecord,
    ApprovalStatus,
)


class ApprovalNotFound(Exception):
    """Raised when an approval record is not found."""


class ApprovalExpired(Exception):
    """Raised when a pending approval has passed its decision TTL."""


class ApprovalAlreadyDecided(Exception):
    """Raised when an approval is no longer pending."""


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

_SELECT_COLUMNS = """
    approval_id,
    intent_sha256,
    intent_json,
    intent_summary,
    status,
    target_id,
    service,
    session_id,
    investigation_id,
    agent_run_id,
    tool_call_id,
    changeset_id,
    proposal_id,
    risk,
    preview_json,
    created_at,
    expires_at,
    decided_at,
    consumed_at,
    decision_actor,
    decision_reason,
    downstream_status,
    downstream_error_code,
    downstream_updated_at
"""


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _preview_json(preview: Mapping[str, object] | None) -> str:
    return json.dumps(
        dict(preview or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _safe_preview_from_intent(intent: Mapping[str, object]) -> dict[str, object]:
    preview = intent.get("preview")
    if isinstance(preview, Mapping):
        return {str(key): value for key, value in preview.items()}

    result: dict[str, object] = {}
    if isinstance(intent.get("security_preview"), str):
        result["preview"] = intent["security_preview"]
    if isinstance(intent.get("impact"), str):
        result["impact"] = intent["impact"]
    if isinstance(intent.get("verification_plan"), str):
        result["verification"] = intent["verification_plan"]
    if isinstance(intent.get("rollback_plan"), str):
        result["rollback"] = intent["rollback_plan"]
    return result


def _initial_downstream_status(
    *,
    session_id: str | None,
    investigation_id: str | None,
    agent_run_id: str | None,
    tool_call_id: str | None,
    changeset_id: str | None,
    proposal_id: str | None,
) -> ApprovalDownstreamStatus:
    if any(
        value
        for value in (
            session_id,
            investigation_id,
            agent_run_id,
            tool_call_id,
            changeset_id,
            proposal_id,
        )
    ):
        return ApprovalDownstreamStatus.PENDING
    return ApprovalDownstreamStatus.NOT_APPLICABLE


def _derive_risk(intent: Mapping[str, object], stored: str | None) -> str:
    intent_risk = _string(intent.get("risk"))
    if intent_risk is not None:
        if stored is None or stored == "approval_required":
            return intent_risk
    return stored or intent_risk or "approval_required"


def _derive_downstream_status(
    *,
    intent: Mapping[str, object],
    session_id: str | None,
    investigation_id: str | None,
    agent_run_id: str | None,
    tool_call_id: str | None,
    changeset_id: str | None,
    proposal_id: str | None,
    stored_status: str | None,
    downstream_updated_at: str | None,
) -> str:
    expected = _initial_downstream_status(
        session_id=session_id,
        investigation_id=investigation_id,
        agent_run_id=agent_run_id,
        tool_call_id=tool_call_id,
        changeset_id=changeset_id,
        proposal_id=proposal_id,
    ).value
    if stored_status is None:
        return expected
    if (
        stored_status == ApprovalDownstreamStatus.NOT_APPLICABLE.value
        and expected == ApprovalDownstreamStatus.PENDING.value
        and downstream_updated_at is None
        and (
            _string(intent.get("session_id"))
            or _string(intent.get("investigation_id"))
            or _string(intent.get("agent_run_id"))
            or _string(intent.get("tool_call_id"))
            or _string(intent.get("changeset_id"))
            or _string(intent.get("proposal_id"))
        )
    ):
        return expected
    return stored_status


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
                    target_id TEXT,
                    service TEXT,
                    session_id TEXT,
                    investigation_id TEXT,
                    agent_run_id TEXT,
                    tool_call_id TEXT,
                    changeset_id TEXT,
                    proposal_id TEXT,
                    risk TEXT NOT NULL DEFAULT 'approval_required',
                    preview_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    consumed_at TEXT,
                    decision_actor TEXT,
                    decision_reason TEXT,
                    downstream_status TEXT NOT NULL DEFAULT 'not_applicable',
                    downstream_error_code TEXT,
                    downstream_updated_at TEXT
                )
                """
            )
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(approvals)").fetchall()
            }
            additions = {
                "target_id": "TEXT",
                "service": "TEXT",
                "session_id": "TEXT",
                "investigation_id": "TEXT",
                "agent_run_id": "TEXT",
                "tool_call_id": "TEXT",
                "changeset_id": "TEXT",
                "proposal_id": "TEXT",
                "risk": "TEXT NOT NULL DEFAULT 'approval_required'",
                "preview_json": "TEXT NOT NULL DEFAULT '{}'",
                "decision_actor": "TEXT",
                "decision_reason": "TEXT",
                "downstream_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
                "downstream_error_code": "TEXT",
                "downstream_updated_at": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE approvals ADD COLUMN {name} {definition}")

            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_status_created "
                "ON approvals(status, created_at, approval_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_target_created "
                "ON approvals(target_id, created_at, approval_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_session_created "
                "ON approvals(session_id, created_at, approval_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_approvals_investigation_created "
                "ON approvals(investigation_id, created_at, approval_id)"
            )
            self._backfill_rows(conn)
            conn.commit()

    def _backfill_rows(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute(
            """
            SELECT approval_id, intent_json, target_id, service, session_id,
                   investigation_id, agent_run_id, tool_call_id, changeset_id,
                   proposal_id, risk, preview_json, downstream_status,
                   downstream_updated_at
            FROM approvals
            """
        ).fetchall()
        for row in rows:
            approval_id = str(row[0])
            intent = json.loads(str(row[1]))
            target_id = _string(row[2]) or _string(intent.get("target_id"))
            service = _string(row[3]) or _string(intent.get("service"))
            session_id = _string(row[4]) or _string(intent.get("session_id"))
            investigation_id = _string(row[5]) or _string(intent.get("investigation_id"))
            agent_run_id = _string(row[6]) or _string(intent.get("agent_run_id"))
            tool_call_id = _string(row[7]) or _string(intent.get("tool_call_id"))
            changeset_id = _string(row[8]) or _string(intent.get("changeset_id"))
            proposal_id = _string(row[9]) or _string(intent.get("proposal_id"))
            risk = _derive_risk(intent, _string(row[10]))
            preview = _string(row[11])
            if preview is None or preview == "{}":
                preview = _preview_json(_safe_preview_from_intent(intent))
            downstream_status = _derive_downstream_status(
                intent=intent,
                session_id=session_id,
                investigation_id=investigation_id,
                agent_run_id=agent_run_id,
                tool_call_id=tool_call_id,
                changeset_id=changeset_id,
                proposal_id=proposal_id,
                stored_status=_string(row[12]),
                downstream_updated_at=_string(row[13]),
            )
            conn.execute(
                """
                UPDATE approvals
                SET target_id = ?, service = ?, session_id = ?, investigation_id = ?,
                    agent_run_id = ?, tool_call_id = ?, changeset_id = ?,
                    proposal_id = ?, risk = ?, preview_json = ?, downstream_status = ?
                WHERE approval_id = ?
                """,
                (
                    target_id,
                    service,
                    session_id,
                    investigation_id,
                    agent_run_id,
                    tool_call_id,
                    changeset_id,
                    proposal_id,
                    risk,
                    preview,
                    downstream_status,
                    approval_id,
                ),
            )

    def create_request(
        self,
        approval_id: str,
        intent_sha256: str,
        intent: Mapping[str, object],
        intent_summary: str,
        now: datetime,
        ttl_minutes: int = DEFAULT_TTL_MINUTES,
        *,
        target_id: str | None = None,
        service: str | None = None,
        session_id: str | None = None,
        investigation_id: str | None = None,
        agent_run_id: str | None = None,
        tool_call_id: str | None = None,
        changeset_id: str | None = None,
        proposal_id: str | None = None,
        risk: str = "approval_required",
        preview: Mapping[str, object] | None = None,
    ) -> ApprovalRecord:
        """Create a new approval request in PENDING status."""
        created_at = now.astimezone(UTC)
        expires_at = created_at + timedelta(minutes=ttl_minutes)
        downstream_status = _initial_downstream_status(
            session_id=session_id,
            investigation_id=investigation_id,
            agent_run_id=agent_run_id,
            tool_call_id=tool_call_id,
            changeset_id=changeset_id,
            proposal_id=proposal_id,
        )
        record = ApprovalRecord(
            approval_id=approval_id,
            intent_sha256=intent_sha256,
            intent=dict(intent),
            intent_summary=intent_summary,
            status=ApprovalStatus.PENDING,
            target_id=target_id or _string(intent.get("target_id")),
            service=service or _string(intent.get("service")),
            session_id=session_id or _string(intent.get("session_id")),
            investigation_id=investigation_id or _string(intent.get("investigation_id")),
            agent_run_id=agent_run_id or _string(intent.get("agent_run_id")),
            tool_call_id=tool_call_id or _string(intent.get("tool_call_id")),
            changeset_id=changeset_id or _string(intent.get("changeset_id")),
            proposal_id=proposal_id or _string(intent.get("proposal_id")),
            risk=risk or _string(intent.get("risk")) or "approval_required",
            preview=dict(preview or _safe_preview_from_intent(intent)),
            created_at=created_at,
            expires_at=expires_at,
            downstream_status=downstream_status,
        )

        with self._connection_factory() as conn:
            conn.execute(
                """
                INSERT INTO approvals
                    (
                        approval_id, intent_sha256, intent_json, intent_summary, status,
                        target_id, service, session_id, investigation_id, agent_run_id,
                        tool_call_id, changeset_id, proposal_id, risk, preview_json,
                        created_at, expires_at, decided_at, consumed_at, decision_actor,
                        decision_reason, downstream_status, downstream_error_code,
                        downstream_updated_at
                    )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.approval_id,
                    record.intent_sha256,
                    json.dumps(
                        record.intent,
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ),
                    record.intent_summary,
                    record.status.value,
                    record.target_id,
                    record.service,
                    record.session_id,
                    record.investigation_id,
                    record.agent_run_id,
                    record.tool_call_id,
                    record.changeset_id,
                    record.proposal_id,
                    record.risk,
                    _preview_json(record.preview),
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                    None,
                    None,
                    None,
                    None,
                    record.downstream_status.value,
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
                f"SELECT {_SELECT_COLUMNS} FROM approvals WHERE approval_id = ?",
                (approval_id,),
            )
            row = cursor.fetchone()

        if row is None:
            return None

        return self._record_from_row(row)

    def list(
        self,
        status: ApprovalStatus | None = None,
        *,
        target_id: str | None = None,
        session_id: str | None = None,
        investigation_id: str | None = None,
        allowed_target_ids: frozenset[str] | None = None,
    ) -> tuple[ApprovalRecord, ...]:
        """List approval records, optionally filtered, oldest first."""
        items: list[ApprovalRecord] = []
        after_created_at: datetime | None = None
        after_approval_id: str | None = None
        while True:
            rows, has_more = self.list_page(
                status=status,
                target_id=target_id,
                session_id=session_id,
                investigation_id=investigation_id,
                allowed_target_ids=allowed_target_ids,
                limit=1_000,
                after_created_at=after_created_at,
                after_approval_id=after_approval_id,
            )
            items.extend(rows)
            if not has_more or not rows:
                return tuple(items)
            after_created_at = rows[-1].created_at
            after_approval_id = rows[-1].approval_id

    def list_page(
        self,
        *,
        status: ApprovalStatus | None,
        target_id: str | None,
        session_id: str | None,
        investigation_id: str | None,
        allowed_target_ids: frozenset[str] | None,
        limit: int,
        after_created_at: datetime | None,
        after_approval_id: str | None,
    ) -> tuple[tuple[ApprovalRecord, ...], bool]:
        where: list[str] = []
        params: list[object] = []
        if status is not None:
            where.append("status = ?")
            params.append(status.value)
        if target_id is not None:
            where.append("target_id = ?")
            params.append(target_id)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if investigation_id is not None:
            where.append("investigation_id = ?")
            params.append(investigation_id)
        if allowed_target_ids is not None:
            if not allowed_target_ids:
                return (), False
            placeholders = ",".join("?" for _ in allowed_target_ids)
            where.append(f"target_id IN ({placeholders})")
            params.extend(sorted(allowed_target_ids))
        if after_created_at is not None and after_approval_id is not None:
            where.append("(created_at > ? OR (created_at = ? AND approval_id > ?))")
            stamp = after_created_at.astimezone(UTC).isoformat()
            params.extend((stamp, stamp, after_approval_id))

        query = f"SELECT {_SELECT_COLUMNS} FROM approvals"
        if where:
            query += " WHERE " + " AND ".join(where)
        query += " ORDER BY created_at, approval_id LIMIT ?"
        params.append(limit + 1)

        with self._connection_factory() as conn:
            rows = conn.execute(query, tuple(params)).fetchall()

        has_more = len(rows) > limit
        materialized = tuple(self._record_from_row(row) for row in rows[:limit])
        return materialized, has_more

    @staticmethod
    def _record_from_row(row: Sequence[object]) -> ApprovalRecord:
        intent = json.loads(str(row[2]))
        preview_raw = _string(row[14]) or "{}"
        try:
            preview = json.loads(preview_raw)
        except json.JSONDecodeError:
            preview = {}
        downstream_updated_at = datetime.fromisoformat(str(row[23])) if row[23] else None
        downstream_status = _derive_downstream_status(
            intent=intent,
            session_id=_string(row[7]) or _string(intent.get("session_id")),
            investigation_id=_string(row[8]) or _string(intent.get("investigation_id")),
            agent_run_id=_string(row[9]) or _string(intent.get("agent_run_id")),
            tool_call_id=_string(row[10]) or _string(intent.get("tool_call_id")),
            changeset_id=_string(row[11]) or _string(intent.get("changeset_id")),
            proposal_id=_string(row[12]) or _string(intent.get("proposal_id")),
            stored_status=_string(row[21]),
            downstream_updated_at=(
                downstream_updated_at.isoformat() if downstream_updated_at else None
            ),
        )
        try:
            parsed_downstream_status = ApprovalDownstreamStatus(downstream_status)
        except ValueError:
            parsed_downstream_status = ApprovalDownstreamStatus.NOT_APPLICABLE

        return ApprovalRecord(
            approval_id=str(row[0]),
            intent_sha256=str(row[1]),
            intent=intent,
            intent_summary=str(row[3]),
            status=ApprovalStatus(str(row[4])),
            target_id=_string(row[5]) or _string(intent.get("target_id")),
            service=_string(row[6]) or _string(intent.get("service")),
            session_id=_string(row[7]) or _string(intent.get("session_id")),
            investigation_id=_string(row[8]) or _string(intent.get("investigation_id")),
            agent_run_id=_string(row[9]) or _string(intent.get("agent_run_id")),
            tool_call_id=_string(row[10]) or _string(intent.get("tool_call_id")),
            changeset_id=_string(row[11]) or _string(intent.get("changeset_id")),
            proposal_id=_string(row[12]) or _string(intent.get("proposal_id")),
            risk=_derive_risk(intent, _string(row[13])),
            preview=preview if isinstance(preview, dict) else {},
            created_at=datetime.fromisoformat(str(row[15])),
            expires_at=datetime.fromisoformat(str(row[16])),
            decided_at=datetime.fromisoformat(str(row[17])) if row[17] else None,
            consumed_at=datetime.fromisoformat(str(row[18])) if row[18] else None,
            decision_actor=_string(row[19]),
            decision_reason=_string(row[20]),
            downstream_status=parsed_downstream_status,
            downstream_error_code=_string(row[22]),
            downstream_updated_at=downstream_updated_at,
        )

    def approve(
        self,
        approval_id: str,
        now: datetime,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRecord:
        """Transition an approval to APPROVED status."""
        return self._decide(
            approval_id,
            ApprovalStatus.APPROVED,
            now,
            actor=actor,
            reason=reason,
        )

    def reject(
        self,
        approval_id: str,
        now: datetime,
        *,
        actor: str | None = None,
        reason: str | None = None,
    ) -> ApprovalRecord:
        """Transition an approval to REJECTED status."""
        return self._decide(
            approval_id,
            ApprovalStatus.REJECTED,
            now,
            actor=actor,
            reason=reason,
        )

    def _decide(
        self,
        approval_id: str,
        target_status: ApprovalStatus,
        now: datetime,
        *,
        actor: str | None,
        reason: str | None,
    ) -> ApprovalRecord:
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            current = conn.execute(
                "SELECT status, expires_at FROM approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if current is None:
                raise ApprovalNotFound(f"Approval '{approval_id}' not found")
            current_status = ApprovalStatus(str(current[0]))
            expires_at = datetime.fromisoformat(str(current[1]))
            if current_status is not ApprovalStatus.PENDING:
                raise ApprovalAlreadyDecided(
                    f"Approval '{approval_id}' is already {current_status.value}"
                )
            if now_utc >= expires_at:
                raise ApprovalExpired(
                    f"Approval '{approval_id}' expired at {expires_at.isoformat()}"
                )

            cursor = conn.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?, decision_actor = ?,
                    decision_reason = ?, downstream_error_code = NULL
                WHERE approval_id = ? AND status = ? AND expires_at > ?
                """,
                (
                    target_status.value,
                    now_utc.isoformat(),
                    actor,
                    reason,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                    now_utc.isoformat(),
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                refreshed = self.get(approval_id)
                if refreshed is None:
                    raise ApprovalNotFound(f"Approval '{approval_id}' not found")
                if refreshed.status is not ApprovalStatus.PENDING:
                    raise ApprovalAlreadyDecided(
                        f"Approval '{approval_id}' is already {refreshed.status.value}"
                    )
                raise ApprovalExpired(
                    f"Approval '{approval_id}' expired at {refreshed.expires_at.isoformat()}"
                )

        return self.get(approval_id)  # type: ignore[return-value]

    def mark_downstream(
        self,
        approval_id: str,
        status: ApprovalDownstreamStatus,
        *,
        error_code: str | None = None,
        now: datetime | None = None,
    ) -> ApprovalRecord:
        """Record best-effort downstream processing independently of decision."""
        stamp = (now or datetime.now(UTC)).astimezone(UTC)
        with self._connection_factory() as conn:
            conn.execute(
                """
                UPDATE approvals
                SET downstream_status = ?, downstream_error_code = ?,
                    downstream_updated_at = ?
                WHERE approval_id = ?
                """,
                (status.value, error_code, stamp.isoformat(), approval_id),
            )
            conn.commit()
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
