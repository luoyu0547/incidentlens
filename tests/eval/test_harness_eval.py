from .runner import EXPECTED_SCENARIOS, run_all


async def test_all_required_harness_scenarios_pass() -> None:
    results = {result.scenario: result for result in await run_all()}
    assert set(results) == EXPECTED_SCENARIOS
    assert all(result.foreign_evidence_count == 0 for result in results.values())
    assert all(result.scope_policy_bypass_count == 0 for result in results.values())
    assert all(result.unapproved_mutation_count == 0 for result in results.values())
    assert all(result.tool_pairing_rate == 1.0 for result in results.values())
    assert results["context_overflow_recovery"].compaction_recovered is True
    assert results["child_restart_delivery"].child_exactly_once_rate == 1.0
    assert results["grounded_diagnosis"].grounded_completion is True
    assert results["scope_violation"].tool_calls >= 1
    assert results["approval_pause_resume"].tool_calls >= 1
    assert results["delegation_equivalence"].child_exactly_once_rate == 1.0
    assert results["delegation_equivalence"].scenario == "delegation_equivalence"


async def test_delegation_equivalence_preserves_distinct_persisted_sources() -> None:
    from .scenarios import run_delegation_equivalence

    trace = await run_delegation_equivalence()
    assert trace.aggregate_sources == ("run-typed", "run-tool")
    assert tuple(run.agent_run_id for run in trace.source_runs) == (
        "run-typed", "child-typed", "run-tool", "child-tool"
    )
    assert tuple(investigation.investigation_id for investigation in trace.source_investigations) == (
        "inv-typed", "inv-tool"
    )
    assert {package.child_run_id for package in trace.delegated_tasks} == {"child-typed", "child-tool"}
    assert {call.tool_call_id for call in trace.tool_calls} >= {
        "tool-delegate",
    }
    assert len({call.tool_call_id for call in trace.tool_calls}) == len(trace.tool_calls)
    assert len({round_.agent_run_id for round_ in trace.rounds}) == 4
    assert len(trace.source_runs) == 4
    assert len(trace.source_investigations) == 2
    from incidentlens_control_plane.investigation.state_machine import AGENT_RUN_TERMINAL

    assert all(run.status in AGENT_RUN_TERMINAL for run in trace.source_runs)
    assert all(run.stop_reason is not None for run in trace.source_runs)
    assert trace.source_runs[0].stop_reason == trace.source_runs[2].stop_reason
    assert trace.source_runs[1].stop_reason == trace.source_runs[3].stop_reason
    packages = {package.child_run_id: package for package in trace.delegated_tasks}
    for child in (trace.source_runs[1], trace.source_runs[3]):
        assert child.scope == packages[child.agent_run_id].scope
        assert packages[child.agent_run_id].parent_run_id == child.parent_run_id
    assert {receipt.child_run_id for receipt in trace.child_receipts} == {"child-typed", "child-tool"}
    assert all(receipt.delivered_at is not None for receipt in trace.child_receipts)
    assert len(trace.owned_evidence_by_run) == 4
    assert all(run.agent_run_id in trace.owned_evidence_by_run for run in trace.source_runs)
