"""Guard tests covering budgets, pre-execution checks and evidence ownership."""

from datetime import UTC, datetime, timedelta

import pytest
from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    ChildReport,
    ChildReportStatus,
    Conclusion,
    EvidenceReference,
    Hypothesis,
    Investigation,
    InvestigationBudget,
    StopReason,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from pydantic import ValidationError

GUARD = InvestigationGuard()
NOW = datetime(2026, 8, 12, 12, 0, 0, tzinfo=UTC)


def make_run(**kwargs: object) -> AgentRun:
    fields: dict[str, object] = {
        "agent_run_id": "run-1",
        "investigation_id": "inv-1",
        "kind": AgentRunKind.PARENT,
        "scope": AgentScope(
            project_id="proj-1",
            target_id="prod-a",
            scope=LogScope.HOST,
        ),
        "status": AgentRunStatus.RUNNING,
        "budget": AgentBudget(),
        "usage": UsageCounters(),
        "created_at": datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
    }
    fields.update(kwargs)
    return AgentRun(**fields)


def make_investigation(**kwargs: object) -> Investigation:
    fields: dict[str, object] = {
        "investigation_id": "inv-1",
        "incident_id": "inc-123",
        "project_id": "proj-1",
        "target_id": "prod-a",
        "service": "orders",
        "symptom": "checkout requests are failing",
        "status": InvestigationStatus.RUNNING,
        "budget": InvestigationBudget(),
        "usage": UsageCounters(),
        "created_at": datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
        "updated_at": datetime(2026, 8, 12, 0, 0, 0, tzinfo=UTC),
    }
    fields.update(kwargs)
    return Investigation(**fields)


def make_evidence(
    evidence_id: str, summary: str = "orders container reports timeout"
) -> EvidenceReference:
    return EvidenceReference(
        evidence_id=evidence_id,
        operation_id="op-1",
        summary=summary,
    )


# -- pre-execution: status ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (AgentRunStatus.COMPLETED, "run is terminal"),
        (AgentRunStatus.CANCELLED, "run is terminal"),
        (AgentRunStatus.FAILED, "run is terminal"),
        (AgentRunStatus.CREATED, "run is not executable in status 'created'"),
        (AgentRunStatus.WAITING_TOOL, "run is not executable in status 'waiting_tool'"),
        (AgentRunStatus.PAUSED_BUDGET, "run is not executable in status 'paused_budget'"),
    ],
    ids=["completed", "cancelled", "failed", "created", "waiting_tool", "paused_budget"],
)
def test_model_turn_is_refused_for_non_running_status(status, reason):
    allowed, got = GUARD.check_before_model_turn(make_run(status=status), now=NOW)

    assert allowed is False
    assert got == reason


def test_tool_execution_is_refused_for_terminal_run():
    allowed, reason = GUARD.check_before_tool_execution(
        make_run(status=AgentRunStatus.COMPLETED), now=NOW
    )

    assert allowed is False
    assert reason == "run is terminal"


# -- budget boundaries (table-driven) -----------------------------------------


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (UsageCounters(rounds=7), True),
        (UsageCounters(rounds=8), False),
    ],
    ids=["round_below_limit", "round_at_limit"],
)
def test_model_turn_round_budget_boundary(usage, expected):
    allowed, _ = GUARD.check_before_model_turn(make_run(usage=usage), now=NOW)

    assert allowed is expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (UsageCounters(tool_calls=15), True),
        (UsageCounters(tool_calls=16), False),
    ],
    ids=["tool_calls_below_limit", "tool_calls_at_limit"],
)
def test_tool_execution_tool_budget_boundary(usage, expected):
    allowed, _ = GUARD.check_before_tool_execution(make_run(usage=usage), now=NOW)

    assert allowed is expected


@pytest.mark.parametrize(
    ("started_offset", "expected"),
    [
        (None, True),
        (timedelta(seconds=1_799), True),
        (timedelta(seconds=1_800), False),
        (timedelta(seconds=7_200), False),
    ],
    ids=["never_started", "under_time_budget", "at_time_budget", "past_time_budget"],
)
def test_wall_clock_budget_boundary(started_offset, expected):
    started_at = None if started_offset is None else NOW - started_offset
    allowed, _ = GUARD.check_before_model_turn(make_run(started_at=started_at), now=NOW)

    assert allowed is expected


@pytest.mark.parametrize(
    ("budget", "output_bytes", "expected"),
    [
        (AgentBudget(max_output_bytes_per_tool=100), 100, True),
        (AgentBudget(max_output_bytes_per_tool=100), 101, False),
        (AgentBudget(max_output_bytes_per_tool=100), -1, False),
    ],
    ids=["output_at_per_tool_limit", "output_over_per_tool_limit", "negative_output"],
)
def test_per_tool_output_budget_boundary(budget, output_bytes, expected):
    allowed, _ = GUARD.can_accept_output(make_run(budget=budget), output_bytes)

    assert allowed is expected


@pytest.mark.parametrize(
    ("budget", "usage", "output_bytes", "expected"),
    [
        (AgentBudget(max_total_output_bytes=100), UsageCounters(total_output_bytes=99), 1, True),
        (AgentBudget(max_total_output_bytes=100), UsageCounters(total_output_bytes=100), 0, True),
        (AgentBudget(max_total_output_bytes=100), UsageCounters(total_output_bytes=100), 1, False),
        (AgentBudget(max_total_output_bytes=100), UsageCounters(total_output_bytes=50), 60, False),
    ],
    ids=[
        "cumulative_under_limit",
        "cumulative_at_limit_zero_incoming",
        "cumulative_at_limit_one_incoming",
        "cumulative_over_limit",
    ],
)
def test_cumulative_output_budget_boundary(budget, usage, output_bytes, expected):
    allowed, _ = GUARD.can_accept_output(make_run(budget=budget, usage=usage), output_bytes)

    assert allowed is expected


@pytest.mark.parametrize(
    ("budget", "usage", "expected"),
    [
        (InvestigationBudget(max_children=2), UsageCounters(children=1), True),
        (InvestigationBudget(max_children=2), UsageCounters(children=2), False),
        (InvestigationBudget(max_children=0), UsageCounters(children=0), False),
    ],
    ids=["child_below_limit", "child_at_limit", "children_disabled"],
)
def test_child_budget_boundary(budget, usage, expected):
    allowed, _ = GUARD.can_spawn_child(make_run(), make_investigation(budget=budget, usage=usage))

    assert allowed is expected


def test_child_run_cannot_delegate_grandchildren():
    child = make_run(kind=AgentRunKind.CHILD, parent_run_id="run-parent")
    allowed, reason = GUARD.can_spawn_child(child, make_investigation())

    assert allowed is False
    assert reason == "child run must not delegate grandchildren"


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (UsageCounters(evidence_count=99), True),
        (UsageCounters(evidence_count=100), False),
    ],
    ids=["evidence_below_limit", "evidence_at_limit"],
)
def test_evidence_budget_boundary(usage, expected):
    allowed, _ = GUARD.can_accept_new_evidence(make_run(usage=usage))

    assert allowed is expected


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (UsageCounters(consecutive_no_new_evidence_rounds=2), True),
        (UsageCounters(consecutive_no_new_evidence_rounds=3), False),
    ],
    ids=["no_new_evidence_below_limit", "no_new_evidence_at_limit"],
)
def test_no_new_evidence_budget_boundary(usage, expected):
    allowed, _ = GUARD.is_stalled_no_new_evidence(make_run(usage=usage))

    assert allowed is expected


def test_model_turn_is_refused_when_stalled_no_new_evidence():
    run = make_run(usage=UsageCounters(consecutive_no_new_evidence_rounds=3))
    allowed, reason = GUARD.check_before_model_turn(run, now=NOW)

    assert allowed is False
    assert reason == "no-new-evidence budget exhausted"


# -- evidence ownership -------------------------------------------------------


def test_guard_rejects_conclusion_with_fabricated_evidence():
    allowed, reason = GUARD.validate_conclusion(
        make_run(),
        Conclusion(summary="database is saturated", evidence_ids=("ev-fabricated",)),
    )

    assert allowed is False
    assert reason == "conclusion cites evidence not collected in this investigation"


def test_guard_rejects_conclusion_without_any_evidence():
    allowed, reason = GUARD.validate_conclusion(
        make_run(),
        Conclusion(summary="no root cause found", evidence_ids=()),
    )

    assert allowed is False
    assert reason == "conclusion cites no evidence"


def test_guard_rejects_conclusion_citing_foreign_evidence():
    run = make_run(evidence=(make_evidence("ev-1"),))
    allowed, reason = GUARD.validate_conclusion(
        run,
        Conclusion(summary="database is saturated", evidence_ids=("ev-1", "ev-foreign")),
    )

    assert allowed is False
    assert "not collected" in reason


def test_guard_accepts_conclusion_grounded_in_current_evidence():
    run = make_run(evidence=(make_evidence("ev-1"), make_evidence("ev-2")))
    allowed, reason = GUARD.validate_conclusion(
        run,
        Conclusion(summary="database pool is exhausted", evidence_ids=("ev-1", "ev-2")),
    )

    assert allowed is True
    assert reason == "conclusion is grounded in current evidence"


def test_guard_rejects_hypothesis_with_fabricated_evidence():
    hypothesis = Hypothesis(
        hypothesis_id="hyp-1",
        agent_run_id="run-1",
        summary="database pool is exhausted",
        facts=("orders container reports pool timeout",),
        inferences=("traffic is over connection pool limit",),
        unknowns=("pool sizing is unknown",),
        evidence_ids=("ev-fabricated",),
        created_at=NOW,
        updated_at=NOW,
    )
    allowed, reason = GUARD.validate_hypothesis(make_run(), hypothesis)

    assert allowed is False
    assert reason == "hypothesis cites evidence not collected in this investigation"


def test_guard_accepts_hypothesis_grounded_in_current_evidence():
    hypothesis = Hypothesis(
        hypothesis_id="hyp-1",
        agent_run_id="run-1",
        summary="database pool is exhausted",
        facts=("orders container reports pool timeout",),
        evidence_ids=("ev-1",),
        created_at=NOW,
        updated_at=NOW,
    )
    allowed, reason = GUARD.validate_hypothesis(
        make_run(evidence=(make_evidence("ev-1"),)), hypothesis
    )

    assert allowed is True
    assert reason == "hypothesis is grounded in current evidence"


def test_guard_rejects_child_report_with_fabricated_evidence():
    report = ChildReport(
        agent_run_id="run-child",
        parent_run_id="run-1",
        status=ChildReportStatus.PARTIAL,
        summary="container health degraded",
        findings=("container restarted",),
        evidence_ids=("ev-fabricated",),
        stop_reason=StopReason.BUDGET_TIME,
        created_at=NOW,
    )
    allowed, reason = GUARD.validate_child_report(make_run(), report)

    assert allowed is False
    assert reason == "child report cites evidence not collected in this investigation"


def test_guard_accepts_child_report_grounded_in_current_evidence():
    report = ChildReport(
        agent_run_id="run-child",
        parent_run_id="run-1",
        status=ChildReportStatus.COMPLETE,
        summary="container health degraded by OOM restarts",
        findings=("container restarted twice",),
        evidence_ids=("ev-child-1",),
        stop_reason=StopReason.COMPLETED,
        created_at=NOW,
    )
    allowed, reason = GUARD.validate_child_report(
        make_run(evidence=(make_evidence("ev-child-1", "child container restarted"),)), report
    )

    assert allowed is True
    assert reason == "child report is grounded in current evidence"


# -- contract validation ------------------------------------------------------


def test_container_scope_requires_container_identity():
    with pytest.raises(ValidationError):
        AgentScope(project_id="proj-1", target_id="prod-a", scope=LogScope.CONTAINER)


def test_host_scope_must_not_set_container_identity():
    with pytest.raises(ValidationError):
        AgentScope(
            project_id="proj-1",
            target_id="prod-a",
            scope=LogScope.HOST,
            container_name="orders-1",
        )


def test_evidence_citations_must_be_unique():
    with pytest.raises(ValidationError):
        Conclusion(summary="database saturated", evidence_ids=("ev-1", "ev-1"))


def test_evidence_citations_must_not_be_empty_strings():
    with pytest.raises(ValidationError):
        Conclusion(summary="database saturated", evidence_ids=("ev-1", " "))


def test_parent_run_must_not_link_a_parent():
    with pytest.raises(ValidationError):
        make_run(parent_run_id="run-0")


def test_child_run_requires_parent_link():
    with pytest.raises(ValidationError):
        make_run(kind=AgentRunKind.CHILD)
