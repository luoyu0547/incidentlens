"""Deterministic, table-driven state machines for Phase 4 entities.

Every transition is validated through a single ``StateMachine`` instance so a
terminal state can never be re-executed and an unknown transition can never be
applied silently. The tables below are the single source of truth for the
investigation, agent-run and tool-call lifecycles.
"""

from __future__ import annotations

from enum import StrEnum
from typing import AbstractSet, Generic, Mapping, TypeVar

StatusT = TypeVar("StatusT", bound=StrEnum)


class IllegalTransition(Exception):
    """Raised when a requested state transition is not permitted."""


class InvestigationStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_REGISTRY_UPDATE = "waiting_registry_update"
    PAUSED_BUDGET = "paused_budget"
    PAUSED_MISSING_EVIDENCE = "paused_missing_evidence"
    PAUSED_UNCERTAIN_STATE = "paused_uncertain_state"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class AgentRunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_TOOL = "waiting_tool"
    WAITING_CHILDREN = "waiting_children"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED_BUDGET = "paused_budget"
    PAUSED_MISSING_EVIDENCE = "paused_missing_evidence"
    PAUSED_UNCERTAIN_STATE = "paused_uncertain_state"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"


class ToolCallStatus(StrEnum):
    PLANNED = "planned"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNCERTAIN = "uncertain"
    CANCELLED = "cancelled"


INVESTIGATION_TERMINAL = frozenset(
    {
        InvestigationStatus.CANCELLED,
        InvestigationStatus.FAILED,
        InvestigationStatus.COMPLETED,
    }
)

AGENT_RUN_TERMINAL = frozenset(
    {
        AgentRunStatus.CANCELLED,
        AgentRunStatus.FAILED,
        AgentRunStatus.COMPLETED,
    }
)

TOOL_CALL_TERMINAL = frozenset(
    {
        ToolCallStatus.SUCCEEDED,
        ToolCallStatus.FAILED,
        ToolCallStatus.UNCERTAIN,
        ToolCallStatus.CANCELLED,
    }
)

INVESTIGATION_TRANSITIONS: dict[InvestigationStatus, frozenset[InvestigationStatus]] = {
    InvestigationStatus.CREATED: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.FAILED,
            InvestigationStatus.CANCELLED,
        }
    ),
    InvestigationStatus.RUNNING: frozenset(
        {
            InvestigationStatus.WAITING_APPROVAL,
            InvestigationStatus.WAITING_REGISTRY_UPDATE,
            InvestigationStatus.PAUSED_BUDGET,
            InvestigationStatus.PAUSED_MISSING_EVIDENCE,
            InvestigationStatus.PAUSED_UNCERTAIN_STATE,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
            InvestigationStatus.COMPLETED,
        }
    ),
    InvestigationStatus.WAITING_APPROVAL: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.WAITING_REGISTRY_UPDATE: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.PAUSED_BUDGET: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.PAUSED_MISSING_EVIDENCE: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.PAUSED_UNCERTAIN_STATE: frozenset(
        {
            InvestigationStatus.RUNNING,
            InvestigationStatus.CANCEL_REQUESTED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.CANCEL_REQUESTED: frozenset(
        {
            InvestigationStatus.CANCELLED,
            InvestigationStatus.FAILED,
        }
    ),
    InvestigationStatus.CANCELLED: frozenset(),
    InvestigationStatus.FAILED: frozenset(),
    InvestigationStatus.COMPLETED: frozenset(),
}

AGENT_RUN_TRANSITIONS: dict[AgentRunStatus, frozenset[AgentRunStatus]] = {
    AgentRunStatus.CREATED: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.FAILED,
            AgentRunStatus.CANCELLED,
        }
    ),
    AgentRunStatus.RUNNING: frozenset(
        {
            AgentRunStatus.WAITING_TOOL,
            AgentRunStatus.WAITING_CHILDREN,
            AgentRunStatus.WAITING_APPROVAL,
            AgentRunStatus.PAUSED_BUDGET,
            AgentRunStatus.PAUSED_MISSING_EVIDENCE,
            AgentRunStatus.PAUSED_UNCERTAIN_STATE,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
            AgentRunStatus.COMPLETED,
        }
    ),
    AgentRunStatus.WAITING_TOOL: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.WAITING_CHILDREN: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.WAITING_APPROVAL: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.PAUSED_BUDGET: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.PAUSED_MISSING_EVIDENCE: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.PAUSED_UNCERTAIN_STATE: frozenset(
        {
            AgentRunStatus.RUNNING,
            AgentRunStatus.CANCEL_REQUESTED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.CANCEL_REQUESTED: frozenset(
        {
            AgentRunStatus.CANCELLED,
            AgentRunStatus.FAILED,
        }
    ),
    AgentRunStatus.CANCELLED: frozenset(),
    AgentRunStatus.FAILED: frozenset(),
    AgentRunStatus.COMPLETED: frozenset(),
}

TOOL_CALL_TRANSITIONS: dict[ToolCallStatus, frozenset[ToolCallStatus]] = {
    ToolCallStatus.PLANNED: frozenset(
        {
            ToolCallStatus.WAITING_APPROVAL,
            ToolCallStatus.RUNNING,
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.UNCERTAIN,
            ToolCallStatus.CANCELLED,
        }
    ),
    ToolCallStatus.WAITING_APPROVAL: frozenset(
        {
            ToolCallStatus.RUNNING,
            ToolCallStatus.FAILED,
            ToolCallStatus.CANCELLED,
        }
    ),
    ToolCallStatus.RUNNING: frozenset(
        {
            ToolCallStatus.SUCCEEDED,
            ToolCallStatus.FAILED,
            ToolCallStatus.UNCERTAIN,
            ToolCallStatus.CANCELLED,
        }
    ),
    ToolCallStatus.SUCCEEDED: frozenset(),
    ToolCallStatus.FAILED: frozenset(),
    ToolCallStatus.UNCERTAIN: frozenset(),
    ToolCallStatus.CANCELLED: frozenset(),
}


class StateMachine(Generic[StatusT]):
    """A deterministic, table-driven state machine over an enum status.

    The transition table is supplied at construction time; ``can_transition``
    answers membership, ``assert_transition`` raises on illegal moves and
    ``assert_not_terminal`` refuses to execute further operations on a terminal
    state. Instances are immutable after construction.
    """

    def __init__(
        self,
        transitions: Mapping[StatusT, AbstractSet[StatusT]],
        terminal: AbstractSet[StatusT],
    ) -> None:
        self._transitions: dict[StatusT, frozenset[StatusT]] = {
            status: frozenset(targets) for status, targets in transitions.items()
        }
        self._terminal: frozenset[StatusT] = frozenset(terminal)

    def can_transition(self, current: StatusT, target: StatusT) -> bool:
        """Return True only when ``current -> target`` is in the table.

        Statuses must come from the same enum: distinct ``StrEnum`` members that
        share a value (for example ``COMPLETED`` on several enums) compare
        equal, so membership alone would leak cross-enum moves.
        """
        if type(current) is not type(target):
            return False
        return target in self._transitions.get(current, frozenset())

    def assert_transition(self, current: StatusT, target: StatusT) -> StatusT:
        """Return ``target`` when the move is legal, otherwise raise."""
        if not self.can_transition(current, target):
            raise IllegalTransition(
                f"illegal transition: {current.value!r} -> {target.value!r}"
            )
        return target

    def is_terminal(self, status: StatusT) -> bool:
        """Return True for absorbing terminal states."""
        return status in self._terminal

    def assert_not_terminal(self, status: StatusT) -> None:
        """Raise when ``status`` is terminal and must not execute operations."""
        if status in self._terminal:
            raise IllegalTransition(
                f"terminal state cannot execute operations: {status.value!r}"
            )

    def transitions(self, current: StatusT) -> frozenset[StatusT]:
        """Return the set of legal targets reachable from ``current``."""
        return self._transitions.get(current, frozenset())


INVESTIGATION_STATE_MACHINE = StateMachine(
    INVESTIGATION_TRANSITIONS, INVESTIGATION_TERMINAL
)
AGENT_RUN_STATE_MACHINE = StateMachine(AGENT_RUN_TRANSITIONS, AGENT_RUN_TERMINAL)
TOOL_CALL_STATE_MACHINE = StateMachine(TOOL_CALL_TRANSITIONS, TOOL_CALL_TERMINAL)
