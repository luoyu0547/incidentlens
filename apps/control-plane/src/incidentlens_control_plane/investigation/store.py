"""SQLite persistence for the Phase 4 investigation domain.

Follows the runtime.db / sqlite3 conventions of the other stores (projects,
approvals, evidence, logs): an idempotent ``migrate()``, validated Pydantic
JSON in every ``record_json`` column, and conditional UPDATEs for state
transitions so a transition or cancellation request is atomic.  Checkpoints
and round summaries are strictly append-only per ``(agent_run_id, sequence)``
/ ``(agent_run_id, round_number)``.

Only validated domain contracts ever reach a JSON column: every ``record_json``
value is ``model.model_dump_json()`` of the corresponding immutable Pydantic
model, and every read re-validates with ``model_validate_json``.  Raw
transcripts and hidden reasoning never enter this store.
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field

from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    AGENT_RUN_TERMINAL,
    HYPOTHESIS_STATE_MACHINE,
    INVESTIGATION_STATE_MACHINE,
    INVESTIGATION_TERMINAL,
    TOOL_CALL_STATE_MACHINE,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentRun,
    AgentRunKind,
    Checkpoint,
    Conclusion,
    DelegatedTaskPackage,
    Hypothesis,
    HypothesisStatus,
    Investigation,
    ProviderUsage,
    RegistryProposalStatus,
    RegistryUpdateProposal,
    StopReason,
    ToolCall,
    UsageCounters,
)


class InvestigationNotFound(Exception):
    """Raised when an investigation row is missing."""


class AgentRunNotFound(Exception):
    """Raised when an agent-run row is missing."""


class ToolCallNotFound(Exception):
    """Raised when a tool-call row is missing."""


class HypothesisNotFound(Exception):
    """Raised when a hypothesis row is missing."""


class DelegatedTaskNotFound(Exception):
    """Raised when a delegated-task row is missing."""


class ProposalNotFound(Exception):
    """Raised when a registry-update-proposal row is missing."""


class AlreadyExists(Exception):
    """Raised when a create targets an id that already exists."""


class ConcurrentModification(Exception):
    """Raised when a conditional update matched no row because status moved."""


class CheckpointConflict(Exception):
    """Raised when a checkpoint already exists for (run, sequence)."""


class RoundConflict(Exception):
    """Raised when a round summary already exists for (run, round_number)."""


class AgentRound(BaseModel):
    """A validated, immutable summary of one model turn in an agent run.

    The round summary deliberately carries no hidden reasoning or raw provider
    transcript: it records the post-turn run status, the provider usage for the
    turn, the cumulative counters, and the stop decision.  ``status`` is the
    agent-run status after the turn, matching the checkpoint snapshot written
    for the same round where one exists.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_run_id: str = Field(min_length=1, max_length=120)
    round_number: int = Field(ge=1)
    status: AgentRunStatus
    provider_usage: ProviderUsage
    usage: UsageCounters
    stop_reason: StopReason | None = None
    created_at: datetime


_INVESTIGATION_COLUMNS = (
    "investigation_id",
    "incident_id",
    "project_id",
    "status",
    "record_json",
    "created_at",
    "updated_at",
)

_AGENT_RUN_COLUMNS = (
    "agent_run_id",
    "investigation_id",
    "parent_run_id",
    "kind",
    "status",
    "record_json",
    "created_at",
    "updated_at",
)

_AGENT_ROUND_COLUMNS = (
    "agent_run_id",
    "round_number",
    "status",
    "record_json",
    "created_at",
)

_CHECKPOINT_COLUMNS = (
    "agent_run_id",
    "sequence",
    "record_json",
    "created_at",
)

_TOOL_CALL_COLUMNS = (
    "tool_call_id",
    "agent_run_id",
    "status",
    "record_json",
    "created_at",
    "updated_at",
)

_HYPOTHESIS_COLUMNS = (
    "hypothesis_id",
    "agent_run_id",
    "investigation_id",
    "status",
    "record_json",
    "created_at",
    "updated_at",
)

_CONCLUSION_COLUMNS = (
    "conclusion_id",
    "agent_run_id",
    "investigation_id",
    "record_json",
    "created_at",
)

_DELEGATED_TASK_COLUMNS = (
    "child_run_id",
    "parent_run_id",
    "investigation_id",
    "record_json",
    "created_at",
)

_PROPOSAL_COLUMNS = (
    "proposal_id",
    "investigation_id",
    "agent_run_id",
    "status",
    "record_json",
    "created_at",
    "decided_at",
)

_IN_FLIGHT_TOOL_STATUSES: frozenset[ToolCallStatus] = frozenset(
    {
        ToolCallStatus.PLANNED,
        ToolCallStatus.WAITING_APPROVAL,
        ToolCallStatus.RUNNING,
    }
)

# A proposal may only be decided while pending, and only to a *different*
# status — a self-transition would let a decision be replayed.
_PROPOSAL_DECISION_TRANSITIONS: dict[
    RegistryProposalStatus, frozenset[RegistryProposalStatus]
] = {
    RegistryProposalStatus.PENDING: frozenset(
        {
            RegistryProposalStatus.APPROVED,
            RegistryProposalStatus.REJECTED,
            RegistryProposalStatus.STALE,
        }
    ),
    RegistryProposalStatus.APPROVED: frozenset(),
    RegistryProposalStatus.REJECTED: frozenset(),
    RegistryProposalStatus.STALE: frozenset(),
}


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _derive_validated(model: BaseModel, updates: dict[str, object]) -> BaseModel:
    """Derive a new validated contract from ``model`` plus ``updates``.

    ``model_copy(update=...)`` skips validation, so a derived model built that
    way could persist an invalid value (duplicate or empty evidence ids,
    negative output bytes, an over-long error summary) that fails on the next
    read/recovery.  Merging the base dump with the updates and re-validating
    every field keeps the persisted ``record_json`` a valid contract.
    """
    return type(model).model_validate({**model.model_dump(), **updates})


class InvestigationStore:
    """SQLite-backed store for investigation, run and recovery state."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the investigation tables and indexes if they don't exist."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS investigations (
                    investigation_id TEXT PRIMARY KEY,
                    incident_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_investigations_project_status
                    ON investigations(project_id, status);
                CREATE INDEX IF NOT EXISTS idx_investigations_status
                    ON investigations(status);
                CREATE INDEX IF NOT EXISTS idx_investigations_incident
                    ON investigations(incident_id);

                CREATE TABLE IF NOT EXISTS agent_runs (
                    agent_run_id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    parent_run_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_runs_investigation_status
                    ON agent_runs(investigation_id, status);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_parent_status
                    ON agent_runs(parent_run_id, status);
                CREATE INDEX IF NOT EXISTS idx_agent_runs_status
                    ON agent_runs(status);

                CREATE TABLE IF NOT EXISTS agent_rounds (
                    agent_run_id TEXT NOT NULL,
                    round_number INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (agent_run_id, round_number)
                );

                CREATE TABLE IF NOT EXISTS agent_checkpoints (
                    agent_run_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (agent_run_id, sequence)
                );

                CREATE TABLE IF NOT EXISTS tool_calls (
                    tool_call_id TEXT PRIMARY KEY,
                    agent_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tool_calls_run_status
                    ON tool_calls(agent_run_id, status);
                CREATE INDEX IF NOT EXISTS idx_tool_calls_status
                    ON tool_calls(status);

                CREATE TABLE IF NOT EXISTS hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    agent_run_id TEXT NOT NULL,
                    investigation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_hypotheses_run
                    ON hypotheses(agent_run_id);
                CREATE INDEX IF NOT EXISTS idx_hypotheses_investigation_status
                    ON hypotheses(investigation_id, status);

                CREATE TABLE IF NOT EXISTS conclusions (
                    conclusion_id TEXT PRIMARY KEY,
                    agent_run_id TEXT NOT NULL,
                    investigation_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_conclusions_run
                    ON conclusions(agent_run_id);
                CREATE INDEX IF NOT EXISTS idx_conclusions_investigation
                    ON conclusions(investigation_id);

                CREATE TABLE IF NOT EXISTS delegated_tasks (
                    child_run_id TEXT PRIMARY KEY,
                    parent_run_id TEXT NOT NULL,
                    investigation_id TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_delegated_tasks_parent
                    ON delegated_tasks(parent_run_id);
                CREATE INDEX IF NOT EXISTS idx_delegated_tasks_investigation
                    ON delegated_tasks(investigation_id);

                CREATE TABLE IF NOT EXISTS registry_update_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    investigation_id TEXT NOT NULL,
                    agent_run_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_proposals_investigation_status
                    ON registry_update_proposals(investigation_id, status);
                CREATE INDEX IF NOT EXISTS idx_proposals_status
                    ON registry_update_proposals(status);
                """
            )
            conn.commit()

    # -- investigations -------------------------------------------------------

    def create_investigation(self, investigation: Investigation) -> Investigation:
        """Persist a new investigation; raise AlreadyExists on a duplicate id."""
        record_json = investigation.model_dump_json()
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO investigations ({", ".join(_INVESTIGATION_COLUMNS)})
                    VALUES ({_placeholders(len(_INVESTIGATION_COLUMNS))})
                    """,
                    (
                        investigation.investigation_id,
                        investigation.incident_id,
                        investigation.project_id,
                        investigation.status.value,
                        record_json,
                        _iso(investigation.created_at),
                        _iso(investigation.updated_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"investigation already exists: {investigation.investigation_id}"
                ) from exc
        return investigation

    def get_investigation(self, investigation_id: str) -> Investigation:
        """Return the investigation with the given id, or raise InvestigationNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_INVESTIGATION_COLUMNS)}
                FROM investigations WHERE investigation_id = ?
                """,
                (investigation_id,),
            ).fetchone()
        if row is None:
            raise InvestigationNotFound(
                f"investigation not found: {investigation_id}"
            )
        return Investigation.model_validate_json(row[4])

    def list_investigations(
        self,
        *,
        project_id: str | None = None,
        status: InvestigationStatus | None = None,
        incident_id: str | None = None,
    ) -> tuple[Investigation, ...]:
        """Return investigations filtered by project, status or incident."""
        clauses: list[str] = []
        params: list[object] = []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        if incident_id is not None:
            clauses.append("incident_id = ?")
            params.append(incident_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_INVESTIGATION_COLUMNS)}
                FROM investigations {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(Investigation.model_validate_json(row[4]) for row in rows)

    def list_non_terminal_investigations(self) -> tuple[Investigation, ...]:
        """Return investigations not yet in a terminal status (startup query)."""
        terminal = tuple(status.value for status in INVESTIGATION_TERMINAL)
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_INVESTIGATION_COLUMNS)}
                FROM investigations
                WHERE status NOT IN ({_placeholders(len(terminal))})
                ORDER BY created_at ASC
                """,
                terminal,
            ).fetchall()
        return tuple(Investigation.model_validate_json(row[4]) for row in rows)

    def transition_investigation_status(
        self,
        investigation_id: str,
        target: InvestigationStatus,
        *,
        now: datetime,
        stop_reason: StopReason | None = None,
    ) -> Investigation:
        """Atomically move an investigation to ``target``.

        The transition is validated by the investigation state machine and
        applied with a conditional UPDATE on the *current* status, so a
        concurrent writer cannot double-apply a transition.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM investigations WHERE investigation_id = ?",
                (investigation_id,),
            ).fetchone()
            if row is None:
                raise InvestigationNotFound(
                    f"investigation not found: {investigation_id}"
                )
            current = Investigation.model_validate_json(row[0])
            INVESTIGATION_STATE_MACHINE.assert_transition(current.status, target)

            updates: dict[str, object] = {
                "status": target,
                "updated_at": now_utc,
            }
            if target is InvestigationStatus.RUNNING and current.started_at is None:
                updates["started_at"] = now_utc
            if INVESTIGATION_STATE_MACHINE.is_terminal(target):
                updates["completed_at"] = now_utc
            if stop_reason is not None:
                updates["stop_reason"] = stop_reason
            updated = _derive_validated(current, updates)

            cursor = conn.execute(
                """
                UPDATE investigations
                SET record_json = ?, status = ?, updated_at = ?
                WHERE investigation_id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    target.value,
                    _iso(now_utc),
                    investigation_id,
                    current.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ConcurrentModification(
                    f"investigation {investigation_id} status changed concurrently"
                )
        return updated

    def update_investigation(self, investigation: Investigation) -> Investigation:
        """Replace a non-status investigation record (usage, stop_reason...).

        The conditional UPDATE uses the model's own status as the expected
        current status, so a stale model cannot clobber a newer transition.
        Status changes must go through ``transition_investigation_status``.
        """
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE investigations
                SET record_json = ?, status = ?, updated_at = ?
                WHERE investigation_id = ? AND status = ?
                """,
                (
                    investigation.model_dump_json(),
                    investigation.status.value,
                    _iso(investigation.updated_at),
                    investigation.investigation_id,
                    investigation.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                exists = conn.execute(
                    "SELECT 1 FROM investigations WHERE investigation_id = ?",
                    (investigation.investigation_id,),
                ).fetchone()
                if exists is None:
                    raise InvestigationNotFound(
                        f"investigation not found: {investigation.investigation_id}"
                    )
                raise ConcurrentModification(
                    f"investigation {investigation.investigation_id} status changed"
                )
        return investigation

    # -- agent runs -----------------------------------------------------------

    def create_agent_run(self, run: AgentRun) -> AgentRun:
        """Persist a new agent run; raise AlreadyExists on a duplicate id.

        A child run's ``parent_run_id`` must reference a parent run in the same
        investigation, so a child can never be attributed across investigations.
        """
        record_json = run.model_dump_json()
        with self._connection_factory() as conn:
            if run.kind is AgentRunKind.CHILD:
                parent = conn.execute(
                    "SELECT investigation_id FROM agent_runs WHERE agent_run_id = ?",
                    (run.parent_run_id,),
                ).fetchone()
                if parent is None:
                    raise AgentRunNotFound(
                        f"parent run not found: {run.parent_run_id}"
                    )
                if parent[0] != run.investigation_id:
                    raise IllegalTransition(
                        f"child run {run.agent_run_id} parent {run.parent_run_id} "
                        f"belongs to investigation {parent[0]}, not {run.investigation_id}"
                    )
            try:
                conn.execute(
                    f"""
                    INSERT INTO agent_runs ({", ".join(_AGENT_RUN_COLUMNS)})
                    VALUES ({_placeholders(len(_AGENT_RUN_COLUMNS))})
                    """,
                    (
                        run.agent_run_id,
                        run.investigation_id,
                        run.parent_run_id,
                        run.kind.value,
                        run.status.value,
                        record_json,
                        _iso(run.created_at),
                        _iso(run.updated_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"agent run already exists: {run.agent_run_id}"
                ) from exc
        return run

    def get_agent_run(self, agent_run_id: str) -> AgentRun:
        """Return the agent run with the given id, or raise AgentRunNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_AGENT_RUN_COLUMNS)}
                FROM agent_runs WHERE agent_run_id = ?
                """,
                (agent_run_id,),
            ).fetchone()
        if row is None:
            raise AgentRunNotFound(f"agent run not found: {agent_run_id}")
        return AgentRun.model_validate_json(row[5])

    def list_agent_runs(
        self,
        *,
        investigation_id: str | None = None,
        parent_run_id: str | None = None,
        status: AgentRunStatus | None = None,
    ) -> tuple[AgentRun, ...]:
        """Return agent runs filtered by investigation, parent or status."""
        clauses: list[str] = []
        params: list[object] = []
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        if parent_run_id is not None:
            clauses.append("parent_run_id = ?")
            params.append(parent_run_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_AGENT_RUN_COLUMNS)}
                FROM agent_runs {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(AgentRun.model_validate_json(row[5]) for row in rows)

    def list_unfinished_children(
        self, *, investigation_id: str | None = None
    ) -> tuple[AgentRun, ...]:
        """Return child runs not yet in a terminal status (startup query)."""
        terminal = tuple(status.value for status in AGENT_RUN_TERMINAL)
        clauses = ["kind = ?", f"status NOT IN ({_placeholders(len(terminal))})"]
        params: list[object] = [AgentRunKind.CHILD.value, *terminal]
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        where_sql = f"WHERE {' AND '.join(clauses)}"
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_AGENT_RUN_COLUMNS)}
                FROM agent_runs {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(AgentRun.model_validate_json(row[5]) for row in rows)

    def list_waiting_approval_runs(self) -> tuple[AgentRun, ...]:
        """Return agent runs paused on an approval decision (startup query)."""
        return self.list_agent_runs(status=AgentRunStatus.WAITING_APPROVAL)

    def list_waiting_approval_tool_calls(self) -> tuple[ToolCall, ...]:
        """Return tool calls paused on an approval decision (startup query)."""
        return self.list_tool_calls(status=ToolCallStatus.WAITING_APPROVAL)

    def transition_agent_run_status(
        self,
        agent_run_id: str,
        target: AgentRunStatus,
        *,
        now: datetime,
        stop_reason: StopReason | None = None,
    ) -> AgentRun:
        """Atomically move an agent run to ``target`` via a conditional UPDATE."""
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if row is None:
                raise AgentRunNotFound(f"agent run not found: {agent_run_id}")
            current = AgentRun.model_validate_json(row[0])
            AGENT_RUN_STATE_MACHINE.assert_transition(current.status, target)

            updates: dict[str, object] = {
                "status": target,
                "updated_at": now_utc,
            }
            if target is AgentRunStatus.RUNNING and current.started_at is None:
                updates["started_at"] = now_utc
            if AGENT_RUN_STATE_MACHINE.is_terminal(target):
                updates["completed_at"] = now_utc
            if stop_reason is not None:
                updates["stop_reason"] = stop_reason
            updated = _derive_validated(current, updates)

            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET record_json = ?, status = ?, updated_at = ?
                WHERE agent_run_id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    target.value,
                    _iso(now_utc),
                    agent_run_id,
                    current.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ConcurrentModification(
                    f"agent run {agent_run_id} status changed concurrently"
                )
        return updated

    def update_agent_run(self, run: AgentRun) -> AgentRun:
        """Replace a non-status agent-run record (usage, evidence...).

        The conditional UPDATE uses the model's own status as the expected
        current status; status changes must go through
        ``transition_agent_run_status``.
        """
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_runs
                SET record_json = ?, status = ?, updated_at = ?
                WHERE agent_run_id = ? AND status = ?
                """,
                (
                    run.model_dump_json(),
                    run.status.value,
                    _iso(run.updated_at),
                    run.agent_run_id,
                    run.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                exists = conn.execute(
                    "SELECT 1 FROM agent_runs WHERE agent_run_id = ?",
                    (run.agent_run_id,),
                ).fetchone()
                if exists is None:
                    raise AgentRunNotFound(f"agent run not found: {run.agent_run_id}")
                raise ConcurrentModification(
                    f"agent run {run.agent_run_id} status changed"
                )
        return run

    # -- agent rounds (append-only) ------------------------------------------

    def append_round(self, round_summary: AgentRound) -> AgentRound:
        """Append a round summary; raise RoundConflict on a duplicate number."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO agent_rounds ({", ".join(_AGENT_ROUND_COLUMNS)})
                    VALUES ({_placeholders(len(_AGENT_ROUND_COLUMNS))})
                    """,
                    (
                        round_summary.agent_run_id,
                        round_summary.round_number,
                        round_summary.status.value,
                        round_summary.model_dump_json(),
                        _iso(round_summary.created_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise RoundConflict(
                    f"round {round_summary.round_number} already exists for "
                    f"run {round_summary.agent_run_id}"
                ) from exc
        return round_summary

    def list_rounds(self, agent_run_id: str) -> tuple[AgentRound, ...]:
        """Return round summaries for a run, oldest first."""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_AGENT_ROUND_COLUMNS)}
                FROM agent_rounds WHERE agent_run_id = ?
                ORDER BY round_number ASC
                """,
                (agent_run_id,),
            ).fetchall()
        return tuple(AgentRound.model_validate_json(row[3]) for row in rows)

    # -- checkpoints (append-only) -------------------------------------------

    def append_checkpoint(self, checkpoint: Checkpoint) -> Checkpoint:
        """Append a checkpoint; raise CheckpointConflict on a duplicate sequence."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO agent_checkpoints ({", ".join(_CHECKPOINT_COLUMNS)})
                    VALUES ({_placeholders(len(_CHECKPOINT_COLUMNS))})
                    """,
                    (
                        checkpoint.agent_run_id,
                        checkpoint.sequence,
                        checkpoint.model_dump_json(),
                        _iso(checkpoint.created_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise CheckpointConflict(
                    f"checkpoint {checkpoint.sequence} already exists for "
                    f"run {checkpoint.agent_run_id}"
                ) from exc
        return checkpoint

    def list_checkpoints(self, agent_run_id: str) -> tuple[Checkpoint, ...]:
        """Return checkpoints for a run, oldest first."""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_CHECKPOINT_COLUMNS)}
                FROM agent_checkpoints WHERE agent_run_id = ?
                ORDER BY sequence ASC
                """,
                (agent_run_id,),
            ).fetchall()
        return tuple(Checkpoint.model_validate_json(row[2]) for row in rows)

    def get_latest_checkpoint(self, agent_run_id: str) -> Checkpoint | None:
        """Return the highest-sequence checkpoint for a run, or None."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_CHECKPOINT_COLUMNS)}
                FROM agent_checkpoints WHERE agent_run_id = ?
                ORDER BY sequence DESC LIMIT 1
                """,
                (agent_run_id,),
            ).fetchone()
        return Checkpoint.model_validate_json(row[2]) if row is not None else None

    # -- tool calls ----------------------------------------------------------

    def create_tool_call(self, tool_call: ToolCall) -> ToolCall:
        """Persist a new tool call; raise AlreadyExists on a duplicate id."""
        record_json = tool_call.model_dump_json()
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO tool_calls ({", ".join(_TOOL_CALL_COLUMNS)})
                    VALUES ({_placeholders(len(_TOOL_CALL_COLUMNS))})
                    """,
                    (
                        tool_call.tool_call_id,
                        tool_call.agent_run_id,
                        tool_call.status.value,
                        record_json,
                        _iso(tool_call.planned_at),
                        _iso(tool_call.planned_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"tool call already exists: {tool_call.tool_call_id}"
                ) from exc
        return tool_call

    def get_tool_call(self, tool_call_id: str) -> ToolCall:
        """Return the tool call with the given id, or raise ToolCallNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_TOOL_CALL_COLUMNS)}
                FROM tool_calls WHERE tool_call_id = ?
                """,
                (tool_call_id,),
            ).fetchone()
        if row is None:
            raise ToolCallNotFound(f"tool call not found: {tool_call_id}")
        return ToolCall.model_validate_json(row[3])

    def list_tool_calls(
        self,
        *,
        agent_run_id: str | None = None,
        status: ToolCallStatus | None = None,
    ) -> tuple[ToolCall, ...]:
        """Return tool calls filtered by run or status, oldest first."""
        clauses: list[str] = []
        params: list[object] = []
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(agent_run_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_TOOL_CALL_COLUMNS)}
                FROM tool_calls {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(ToolCall.model_validate_json(row[3]) for row in rows)

    def list_in_flight_tool_calls(self) -> tuple[ToolCall, ...]:
        """Return tool calls still planned, awaiting approval or running."""
        values = tuple(status.value for status in _IN_FLIGHT_TOOL_STATUSES)
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_TOOL_CALL_COLUMNS)}
                FROM tool_calls
                WHERE status IN ({_placeholders(len(values))})
                ORDER BY created_at ASC
                """,
                values,
            ).fetchall()
        return tuple(ToolCall.model_validate_json(row[3]) for row in rows)

    def transition_tool_call_status(
        self,
        tool_call_id: str,
        target: ToolCallStatus,
        *,
        now: datetime,
        output_bytes: int | None = None,
        evidence_ids: Sequence[str] | None = None,
        error_redacted: str | None = None,
        approval_id: str | None = None,
    ) -> ToolCall:
        """Atomically move a tool call to ``target`` via a conditional UPDATE.

        ``RUNNING`` stamps ``started_at`` and terminal targets stamp
        ``finished_at``; success may attach bounded ``output_bytes``,
        ``evidence_ids`` and a redacted error summary.  ``approval_id`` records
        the exact approval a WAITING_APPROVAL tool call is paused on so an API
        (Task 8) can link the run back to its pending approval.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM tool_calls WHERE tool_call_id = ?",
                (tool_call_id,),
            ).fetchone()
            if row is None:
                raise ToolCallNotFound(f"tool call not found: {tool_call_id}")
            current = ToolCall.model_validate_json(row[0])
            TOOL_CALL_STATE_MACHINE.assert_transition(current.status, target)

            updates: dict[str, object] = {"status": target}
            if target is ToolCallStatus.RUNNING and current.started_at is None:
                updates["started_at"] = now_utc
            if TOOL_CALL_STATE_MACHINE.is_terminal(target):
                updates["finished_at"] = now_utc
            if output_bytes is not None:
                updates["output_bytes"] = output_bytes
            if evidence_ids is not None:
                updates["evidence_ids"] = tuple(evidence_ids)
            if error_redacted is not None:
                updates["error_redacted"] = error_redacted
            if approval_id is not None:
                updates["approval_id"] = approval_id
            updated = _derive_validated(current, updates)

            cursor = conn.execute(
                """
                UPDATE tool_calls
                SET record_json = ?, status = ?, updated_at = ?
                WHERE tool_call_id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    target.value,
                    _iso(now_utc),
                    tool_call_id,
                    current.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ConcurrentModification(
                    f"tool call {tool_call_id} status changed concurrently"
                )
        return updated

    # -- hypotheses -----------------------------------------------------------

    def create_hypothesis(self, hypothesis: Hypothesis) -> Hypothesis:
        """Persist a new hypothesis; raise AlreadyExists on a duplicate id.

        The ``investigation_id`` index column is derived from the owning agent
        run, so a hypothesis can never be attributed to the wrong investigation.
        """
        record_json = hypothesis.model_dump_json()
        with self._connection_factory() as conn:
            run_row = conn.execute(
                "SELECT investigation_id FROM agent_runs WHERE agent_run_id = ?",
                (hypothesis.agent_run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunNotFound(
                    f"agent run not found: {hypothesis.agent_run_id}"
                )
            investigation_id = run_row[0]
            try:
                conn.execute(
                    f"""
                    INSERT INTO hypotheses ({", ".join(_HYPOTHESIS_COLUMNS)})
                    VALUES ({_placeholders(len(_HYPOTHESIS_COLUMNS))})
                    """,
                    (
                        hypothesis.hypothesis_id,
                        hypothesis.agent_run_id,
                        investigation_id,
                        hypothesis.status.value,
                        record_json,
                        _iso(hypothesis.created_at),
                        _iso(hypothesis.updated_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"hypothesis already exists: {hypothesis.hypothesis_id}"
                ) from exc
        return hypothesis

    def get_hypothesis(self, hypothesis_id: str) -> Hypothesis:
        """Return the hypothesis with the given id, or raise HypothesisNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_HYPOTHESIS_COLUMNS)}
                FROM hypotheses WHERE hypothesis_id = ?
                """,
                (hypothesis_id,),
            ).fetchone()
        if row is None:
            raise HypothesisNotFound(f"hypothesis not found: {hypothesis_id}")
        return Hypothesis.model_validate_json(row[4])

    def list_hypotheses(
        self,
        *,
        agent_run_id: str | None = None,
        investigation_id: str | None = None,
    ) -> tuple[Hypothesis, ...]:
        """Return hypotheses filtered by run or investigation, oldest first."""
        clauses: list[str] = []
        params: list[object] = []
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(agent_run_id)
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_HYPOTHESIS_COLUMNS)}
                FROM hypotheses {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(Hypothesis.model_validate_json(row[4]) for row in rows)

    def transition_hypothesis_status(
        self,
        hypothesis_id: str,
        target: HypothesisStatus,
        *,
        now: datetime,
    ) -> Hypothesis:
        """Atomically move a hypothesis to ``target`` via a conditional UPDATE.

        The transition is validated by the hypothesis state machine and applied
        with a conditional UPDATE on the *current* status, so an illegal rollback
        (for example CONFIRMED -> ACTIVE) is rejected and a concurrent writer
        cannot double-apply a transition.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM hypotheses WHERE hypothesis_id = ?",
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise HypothesisNotFound(f"hypothesis not found: {hypothesis_id}")
            current = Hypothesis.model_validate_json(row[0])
            HYPOTHESIS_STATE_MACHINE.assert_transition(current.status, target)
            updated = _derive_validated(
                current, {"status": target, "updated_at": now_utc}
            )
            cursor = conn.execute(
                """
                UPDATE hypotheses
                SET record_json = ?, status = ?, updated_at = ?
                WHERE hypothesis_id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    target.value,
                    _iso(now_utc),
                    hypothesis_id,
                    current.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ConcurrentModification(
                    f"hypothesis {hypothesis_id} status changed concurrently"
                )
        return updated

    # -- conclusions ----------------------------------------------------------

    def create_conclusion(
        self,
        agent_run_id: str,
        investigation_id: str,
        conclusion: Conclusion,
        *,
        now: datetime,
    ) -> Conclusion:
        """Persist a conclusion, returning the stored contract unchanged.

        The owning agent run must belong to the named investigation, so a
        conclusion can never be attributed across investigations.
        """
        now_utc = now.astimezone(UTC)
        conclusion_id = f"concl-{uuid.uuid4().hex[:16]}"
        with self._connection_factory() as conn:
            run_row = conn.execute(
                "SELECT investigation_id FROM agent_runs WHERE agent_run_id = ?",
                (agent_run_id,),
            ).fetchone()
            if run_row is None:
                raise AgentRunNotFound(f"agent run not found: {agent_run_id}")
            if run_row[0] != investigation_id:
                raise IllegalTransition(
                    f"conclusion agent run {agent_run_id} belongs to investigation "
                    f"{run_row[0]}, not {investigation_id}"
                )
            conn.execute(
                f"""
                INSERT INTO conclusions ({", ".join(_CONCLUSION_COLUMNS)})
                VALUES ({_placeholders(len(_CONCLUSION_COLUMNS))})
                """,
                (
                    conclusion_id,
                    agent_run_id,
                    investigation_id,
                    conclusion.model_dump_json(),
                    _iso(now_utc),
                ),
            )
            conn.commit()
        return conclusion

    def list_conclusions(
        self,
        *,
        agent_run_id: str | None = None,
        investigation_id: str | None = None,
    ) -> tuple[Conclusion, ...]:
        """Return conclusions filtered by run or investigation, oldest first."""
        clauses: list[str] = []
        params: list[object] = []
        if agent_run_id is not None:
            clauses.append("agent_run_id = ?")
            params.append(agent_run_id)
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_CONCLUSION_COLUMNS)}
                FROM conclusions {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(Conclusion.model_validate_json(row[3]) for row in rows)

    # -- delegated tasks ------------------------------------------------------

    def create_delegated_task(
        self, package: DelegatedTaskPackage, *, now: datetime
    ) -> DelegatedTaskPackage:
        """Persist a delegated task package; raise AlreadyExists on a duplicate.

        The delegating parent run must belong to the package's investigation,
        so a child task can never be attributed across investigations.
        """
        with self._connection_factory() as conn:
            parent = conn.execute(
                "SELECT investigation_id FROM agent_runs WHERE agent_run_id = ?",
                (package.parent_run_id,),
            ).fetchone()
            if parent is None:
                raise AgentRunNotFound(
                    f"parent run not found: {package.parent_run_id}"
                )
            if parent[0] != package.investigation_id:
                raise IllegalTransition(
                    f"delegated task parent {package.parent_run_id} belongs to "
                    f"investigation {parent[0]}, not {package.investigation_id}"
                )
            try:
                conn.execute(
                    f"""
                    INSERT INTO delegated_tasks ({", ".join(_DELEGATED_TASK_COLUMNS)})
                    VALUES ({_placeholders(len(_DELEGATED_TASK_COLUMNS))})
                    """,
                    (
                        package.child_run_id,
                        package.parent_run_id,
                        package.investigation_id,
                        package.model_dump_json(),
                        _iso(now.astimezone(UTC)),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"delegated task already exists: {package.child_run_id}"
                ) from exc
        return package

    def get_delegated_task(self, child_run_id: str) -> DelegatedTaskPackage:
        """Return the delegated task for a child run, or raise DelegatedTaskNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_DELEGATED_TASK_COLUMNS)}
                FROM delegated_tasks WHERE child_run_id = ?
                """,
                (child_run_id,),
            ).fetchone()
        if row is None:
            raise DelegatedTaskNotFound(
                f"delegated task not found: {child_run_id}"
            )
        return DelegatedTaskPackage.model_validate_json(row[3])

    def list_delegated_tasks(
        self,
        *,
        parent_run_id: str | None = None,
        investigation_id: str | None = None,
    ) -> tuple[DelegatedTaskPackage, ...]:
        """Return delegated tasks filtered by parent or investigation."""
        clauses: list[str] = []
        params: list[object] = []
        if parent_run_id is not None:
            clauses.append("parent_run_id = ?")
            params.append(parent_run_id)
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_DELEGATED_TASK_COLUMNS)}
                FROM delegated_tasks {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(
            DelegatedTaskPackage.model_validate_json(row[3]) for row in rows
        )

    # -- registry update proposals -------------------------------------------

    def create_proposal(self, proposal: RegistryUpdateProposal) -> RegistryUpdateProposal:
        """Persist a proposal; raise AlreadyExists on a duplicate id."""
        record_json = proposal.model_dump_json()
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO registry_update_proposals ({", ".join(_PROPOSAL_COLUMNS)})
                    VALUES ({_placeholders(len(_PROPOSAL_COLUMNS))})
                    """,
                    (
                        proposal.proposal_id,
                        proposal.investigation_id,
                        proposal.agent_run_id,
                        proposal.status.value,
                        record_json,
                        _iso(proposal.created_at),
                        _iso(proposal.decided_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AlreadyExists(
                    f"proposal already exists: {proposal.proposal_id}"
                ) from exc
        return proposal

    def get_proposal(self, proposal_id: str) -> RegistryUpdateProposal:
        """Return the proposal with the given id, or raise ProposalNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_PROPOSAL_COLUMNS)}
                FROM registry_update_proposals WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            raise ProposalNotFound(f"proposal not found: {proposal_id}")
        return RegistryUpdateProposal.model_validate_json(row[4])

    def list_proposals(
        self,
        *,
        investigation_id: str | None = None,
        status: RegistryProposalStatus | None = None,
    ) -> tuple[RegistryUpdateProposal, ...]:
        """Return proposals filtered by investigation or status."""
        clauses: list[str] = []
        params: list[object] = []
        if investigation_id is not None:
            clauses.append("investigation_id = ?")
            params.append(investigation_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_PROPOSAL_COLUMNS)}
                FROM registry_update_proposals {where_sql}
                ORDER BY created_at ASC
                """,
                tuple(params),
            ).fetchall()
        return tuple(
            RegistryUpdateProposal.model_validate_json(row[4]) for row in rows
        )

    def list_pending_proposals(self) -> tuple[RegistryUpdateProposal, ...]:
        """Return proposals still awaiting an approval decision (startup query)."""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_PROPOSAL_COLUMNS)}
                FROM registry_update_proposals
                WHERE status = ?
                ORDER BY created_at ASC
                """,
                (RegistryProposalStatus.PENDING.value,),
            ).fetchall()
        return tuple(
            RegistryUpdateProposal.model_validate_json(row[4]) for row in rows
        )

    def transition_proposal_status(
        self,
        proposal_id: str,
        target: RegistryProposalStatus,
        *,
        now: datetime,
    ) -> RegistryUpdateProposal:
        """Atomically move a proposal to ``target`` via a conditional UPDATE.

        Only a pending proposal may be decided; the move must target a different
        status (no self-transition) so a decision cannot be re-applied or
        replayed. The transition stamps ``decided_at``.
        """
        now_utc = now.astimezone(UTC)
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT record_json FROM registry_update_proposals WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise ProposalNotFound(f"proposal not found: {proposal_id}")
            current = RegistryUpdateProposal.model_validate_json(row[0])
            legal_targets = _PROPOSAL_DECISION_TRANSITIONS.get(current.status, frozenset())
            if target not in legal_targets:
                raise IllegalTransition(
                    f"illegal proposal transition: {current.status.value!r} -> {target.value!r}"
                )
            updated = _derive_validated(
                current, {"status": target, "decided_at": now_utc}
            )
            cursor = conn.execute(
                """
                UPDATE registry_update_proposals
                SET record_json = ?, status = ?, decided_at = ?
                WHERE proposal_id = ? AND status = ?
                """,
                (
                    updated.model_dump_json(),
                    target.value,
                    _iso(now_utc),
                    proposal_id,
                    current.status.value,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise ConcurrentModification(
                    f"proposal {proposal_id} status changed concurrently"
                )
        return updated
