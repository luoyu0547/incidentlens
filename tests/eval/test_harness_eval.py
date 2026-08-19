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
