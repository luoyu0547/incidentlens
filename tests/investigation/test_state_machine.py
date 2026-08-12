"""Table-driven tests locking the Phase 4 state machines."""

from enum import StrEnum

import pytest
from incidentlens_control_plane.investigation.state_machine import (
    AGENT_RUN_STATE_MACHINE,
    AGENT_RUN_TERMINAL,
    AGENT_RUN_TRANSITIONS,
    INVESTIGATION_STATE_MACHINE,
    INVESTIGATION_TERMINAL,
    INVESTIGATION_TRANSITIONS,
    TOOL_CALL_STATE_MACHINE,
    TOOL_CALL_TERMINAL,
    TOOL_CALL_TRANSITIONS,
    AgentRunStatus,
    IllegalTransition,
    InvestigationStatus,
    StateMachine,
    ToolCallStatus,
)

INVESTIGATION_CASES = [
    # created
    (InvestigationStatus.CREATED, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.CREATED, InvestigationStatus.FAILED, True),
    (InvestigationStatus.CREATED, InvestigationStatus.CANCELLED, True),
    (InvestigationStatus.CREATED, InvestigationStatus.COMPLETED, False),
    (InvestigationStatus.CREATED, InvestigationStatus.CANCEL_REQUESTED, False),
    # running
    (InvestigationStatus.RUNNING, InvestigationStatus.WAITING_APPROVAL, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.WAITING_REGISTRY_UPDATE, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.PAUSED_BUDGET, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.PAUSED_MISSING_EVIDENCE, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.PAUSED_UNCERTAIN_STATE, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.CANCEL_REQUESTED, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.COMPLETED, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.FAILED, True),
    (InvestigationStatus.RUNNING, InvestigationStatus.CANCELLED, False),
    (InvestigationStatus.RUNNING, InvestigationStatus.CREATED, False),
    # waiting / paused must resume before completing
    (InvestigationStatus.WAITING_APPROVAL, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.WAITING_APPROVAL, InvestigationStatus.CANCEL_REQUESTED, True),
    (InvestigationStatus.WAITING_APPROVAL, InvestigationStatus.FAILED, True),
    (InvestigationStatus.WAITING_APPROVAL, InvestigationStatus.COMPLETED, False),
    (InvestigationStatus.WAITING_REGISTRY_UPDATE, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.WAITING_REGISTRY_UPDATE, InvestigationStatus.COMPLETED, False),
    (InvestigationStatus.PAUSED_BUDGET, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.PAUSED_BUDGET, InvestigationStatus.CANCELLED, False),
    (InvestigationStatus.PAUSED_MISSING_EVIDENCE, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.PAUSED_UNCERTAIN_STATE, InvestigationStatus.RUNNING, True),
    (InvestigationStatus.PAUSED_BUDGET, InvestigationStatus.PAUSED_MISSING_EVIDENCE, False),
    # cancel is a two-step audit trail: requested, then cancelled
    (InvestigationStatus.CANCEL_REQUESTED, InvestigationStatus.CANCELLED, True),
    (InvestigationStatus.CANCEL_REQUESTED, InvestigationStatus.FAILED, True),
    (InvestigationStatus.CANCEL_REQUESTED, InvestigationStatus.RUNNING, False),
    # terminal states are absorbing
    (InvestigationStatus.CANCELLED, InvestigationStatus.RUNNING, False),
    (InvestigationStatus.CANCELLED, InvestigationStatus.CANCELLED, False),
    (InvestigationStatus.FAILED, InvestigationStatus.RUNNING, False),
    (InvestigationStatus.FAILED, InvestigationStatus.COMPLETED, False),
    (InvestigationStatus.COMPLETED, InvestigationStatus.RUNNING, False),
    (InvestigationStatus.COMPLETED, InvestigationStatus.CANCELLED, False),
]

AGENT_RUN_CASES = [
    # created
    (AgentRunStatus.CREATED, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.CREATED, AgentRunStatus.FAILED, True),
    (AgentRunStatus.CREATED, AgentRunStatus.CANCELLED, True),
    (AgentRunStatus.CREATED, AgentRunStatus.COMPLETED, False),
    (AgentRunStatus.CREATED, AgentRunStatus.CANCEL_REQUESTED, False),
    # running
    (AgentRunStatus.RUNNING, AgentRunStatus.WAITING_TOOL, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.WAITING_CHILDREN, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.WAITING_APPROVAL, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.PAUSED_BUDGET, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.PAUSED_MISSING_EVIDENCE, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.PAUSED_UNCERTAIN_STATE, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.CANCEL_REQUESTED, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.COMPLETED, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.FAILED, True),
    (AgentRunStatus.RUNNING, AgentRunStatus.CANCELLED, False),
    (AgentRunStatus.RUNNING, AgentRunStatus.CREATED, False),
    # waiting / paused
    (AgentRunStatus.WAITING_TOOL, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.WAITING_TOOL, AgentRunStatus.CANCEL_REQUESTED, True),
    (AgentRunStatus.WAITING_TOOL, AgentRunStatus.FAILED, True),
    (AgentRunStatus.WAITING_TOOL, AgentRunStatus.COMPLETED, False),
    (AgentRunStatus.WAITING_CHILDREN, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.WAITING_CHILDREN, AgentRunStatus.CANCELLED, False),
    (AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.WAITING_APPROVAL, AgentRunStatus.CANCEL_REQUESTED, True),
    (AgentRunStatus.PAUSED_BUDGET, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.PAUSED_BUDGET, AgentRunStatus.WAITING_CHILDREN, False),
    (AgentRunStatus.PAUSED_MISSING_EVIDENCE, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.PAUSED_UNCERTAIN_STATE, AgentRunStatus.RUNNING, True),
    (AgentRunStatus.PAUSED_UNCERTAIN_STATE, AgentRunStatus.FAILED, True),
    # cancel path
    (AgentRunStatus.CANCEL_REQUESTED, AgentRunStatus.CANCELLED, True),
    (AgentRunStatus.CANCEL_REQUESTED, AgentRunStatus.FAILED, True),
    (AgentRunStatus.CANCEL_REQUESTED, AgentRunStatus.RUNNING, False),
    # terminal absorbing
    (AgentRunStatus.CANCELLED, AgentRunStatus.RUNNING, False),
    (AgentRunStatus.FAILED, AgentRunStatus.RUNNING, False),
    (AgentRunStatus.COMPLETED, AgentRunStatus.RUNNING, False),
]

TOOL_CALL_CASES = [
    (ToolCallStatus.PLANNED, ToolCallStatus.WAITING_APPROVAL, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.RUNNING, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.SUCCEEDED, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.FAILED, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.UNCERTAIN, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.CANCELLED, True),
    (ToolCallStatus.PLANNED, ToolCallStatus.PLANNED, False),
    (ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.RUNNING, True),
    (ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.CANCELLED, True),
    (ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.FAILED, True),
    (ToolCallStatus.WAITING_APPROVAL, ToolCallStatus.UNCERTAIN, False),
    (ToolCallStatus.RUNNING, ToolCallStatus.SUCCEEDED, True),
    (ToolCallStatus.RUNNING, ToolCallStatus.FAILED, True),
    (ToolCallStatus.RUNNING, ToolCallStatus.UNCERTAIN, True),
    (ToolCallStatus.RUNNING, ToolCallStatus.CANCELLED, True),
    (ToolCallStatus.RUNNING, ToolCallStatus.WAITING_APPROVAL, False),
    (ToolCallStatus.SUCCEEDED, ToolCallStatus.RUNNING, False),
    (ToolCallStatus.SUCCEEDED, ToolCallStatus.CANCELLED, False),
    (ToolCallStatus.FAILED, ToolCallStatus.RUNNING, False),
    (ToolCallStatus.UNCERTAIN, ToolCallStatus.RUNNING, False),
    (ToolCallStatus.UNCERTAIN, ToolCallStatus.SUCCEEDED, False),
    (ToolCallStatus.CANCELLED, ToolCallStatus.RUNNING, False),
]

ALL_CASES = [
    *[(INVESTIGATION_STATE_MACHINE, *case) for case in INVESTIGATION_CASES],
    *[(AGENT_RUN_STATE_MACHINE, *case) for case in AGENT_RUN_CASES],
    *[(TOOL_CALL_STATE_MACHINE, *case) for case in TOOL_CALL_CASES],
]

ALL_CASE_IDS = [
    *[f"inv:{current.value}->{target.value}" for current, target, _ in INVESTIGATION_CASES],
    *[f"run:{current.value}->{target.value}" for current, target, _ in AGENT_RUN_CASES],
    *[f"tool:{current.value}->{target.value}" for current, target, _ in TOOL_CALL_CASES],
]

MACHINES_AND_TERMINAL = [
    (INVESTIGATION_STATE_MACHINE, INVESTIGATION_TERMINAL),
    (AGENT_RUN_STATE_MACHINE, AGENT_RUN_TERMINAL),
    (TOOL_CALL_STATE_MACHINE, TOOL_CALL_TERMINAL),
]

MACHINES_AND_TABLES = [
    (INVESTIGATION_STATE_MACHINE, INVESTIGATION_TRANSITIONS, INVESTIGATION_TERMINAL),
    (AGENT_RUN_STATE_MACHINE, AGENT_RUN_TRANSITIONS, AGENT_RUN_TERMINAL),
    (TOOL_CALL_STATE_MACHINE, TOOL_CALL_TRANSITIONS, TOOL_CALL_TERMINAL),
]


@pytest.mark.parametrize(
    ("machine", "current", "target", "expected"),
    ALL_CASES,
    ids=ALL_CASE_IDS,
)
def test_transition_is_legal_or_illegal(machine, current, target, expected):
    assert machine.can_transition(current, target) is expected
    if expected:
        assert machine.assert_transition(current, target) is target
    else:
        with pytest.raises(IllegalTransition):
            machine.assert_transition(current, target)


@pytest.mark.parametrize(
    ("machine", "terminal"),
    MACHINES_AND_TERMINAL,
    ids=["investigation", "agent_run", "tool_call"],
)
def test_terminal_states_have_no_outgoing_transitions(machine, terminal):
    for status in terminal:
        assert machine.transitions(status) == frozenset()
        for other in type(status):
            assert machine.can_transition(status, other) is False


@pytest.mark.parametrize(
    ("machine", "terminal"),
    MACHINES_AND_TERMINAL,
    ids=["investigation", "agent_run", "tool_call"],
)
def test_terminal_invariants(machine, terminal):
    sample = next(iter(terminal))
    for status in type(sample):
        assert machine.is_terminal(status) is (status in terminal)
    for status in terminal:
        with pytest.raises(IllegalTransition):
            machine.assert_not_terminal(status)
    active = set(type(sample)) - set(terminal)
    for status in active:
        machine.assert_not_terminal(status)


@pytest.mark.parametrize(
    ("machine", "transitions", "terminal"),
    MACHINES_AND_TABLES,
    ids=["investigation", "agent_run", "tool_call"],
)
def test_transition_table_covers_every_status_and_terminal_is_empty(machine, transitions, terminal):
    status_type = type(next(iter(transitions)))
    assert set(transitions) == set(status_type)
    for status in terminal:
        assert transitions[status] == frozenset()
    for status, targets in transitions.items():
        assert status in status_type
        assert set(targets) <= set(status_type)


def test_unrelated_enum_value_is_always_illegal():
    assert AGENT_RUN_STATE_MACHINE.can_transition(
        AgentRunStatus.RUNNING, InvestigationStatus.COMPLETED
    ) is False
    with pytest.raises(IllegalTransition):
        AGENT_RUN_STATE_MACHINE.assert_transition(
            AgentRunStatus.RUNNING, InvestigationStatus.COMPLETED
        )


def test_custom_state_machine_obeys_its_table():
    class Color(StrEnum):
        RED = "red"
        GREEN = "green"
        BLUE = "blue"

    machine = StateMachine(
        {
            Color.RED: frozenset({Color.GREEN}),
            Color.GREEN: frozenset(),
            Color.BLUE: frozenset(),
        },
        terminal=frozenset({Color.GREEN, Color.BLUE}),
    )

    assert machine.can_transition(Color.RED, Color.GREEN) is True
    assert machine.can_transition(Color.RED, Color.BLUE) is False
    assert machine.assert_transition(Color.RED, Color.GREEN) is Color.GREEN
    assert machine.is_terminal(Color.GREEN) is True
    assert machine.is_terminal(Color.RED) is False
    with pytest.raises(IllegalTransition):
        machine.assert_transition(Color.GREEN, Color.RED)
    machine.assert_not_terminal(Color.RED)
    with pytest.raises(IllegalTransition):
        machine.assert_not_terminal(Color.BLUE)
