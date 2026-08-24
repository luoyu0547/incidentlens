"""Deterministic state machine for durable Operations.

The transition table below is the single source of truth for the operation
lifecycle, shared by the store (which validates every move before applying a
conditional UPDATE) and by cancellation logic.  Terminal statuses
(``succeeded`` / ``failed`` / ``cancelled`` / ``uncertain``) are absorbing:
once reached an operation can never transition again.

The generic :class:`~incidentlens_control_plane.investigation.state_machine.StateMachine`
is reused so an illegal move always raises the shared ``IllegalTransition``
error and a terminal state can never be re-executed.
"""

from __future__ import annotations

from incidentlens_control_plane.investigation.state_machine import StateMachine
from incidentlens_control_plane.operations.types import OperationStatus

#: Terminal states are absorbing; no transition is legal once reached.
OPERATION_TERMINAL: frozenset[OperationStatus] = frozenset(
    {
        OperationStatus.SUCCEEDED,
        OperationStatus.FAILED,
        OperationStatus.CANCELLED,
        OperationStatus.UNCERTAIN,
    }
)

#: Verbatim transition table (single source of truth).
OPERATION_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.QUEUED: frozenset(
        {
            OperationStatus.RUNNING,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.CANCELLED,
        }
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.SUCCEEDED,
            OperationStatus.FAILED,
            OperationStatus.CANCEL_REQUESTED,
            OperationStatus.UNCERTAIN,
        }
    ),
    OperationStatus.CANCEL_REQUESTED: frozenset(
        {
            OperationStatus.CANCELLED,
            OperationStatus.FAILED,
            OperationStatus.UNCERTAIN,
        }
    ),
    OperationStatus.SUCCEEDED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.CANCELLED: frozenset(),
    OperationStatus.UNCERTAIN: frozenset(),
}

OPERATION_STATE_MACHINE: StateMachine[OperationStatus] = StateMachine(
    OPERATION_TRANSITIONS, OPERATION_TERMINAL
)


class OperationNotCancellable(Exception):
    """Raised when cancelling an operation in a terminal non-cancelled state."""


__all__ = [
    "OPERATION_STATE_MACHINE",
    "OPERATION_TERMINAL",
    "OPERATION_TRANSITIONS",
    "OperationNotCancellable",
]
