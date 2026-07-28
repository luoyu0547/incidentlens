"""Recovery tests: interrupt/resume, model timeout, and checkpoint corruption.

Uses real SQLite (not mocked) to verify:
  - Resume does not repeat a successful tool or change the evidence ID
  - Model timeout keeps completed evidence and is resumable
  - Corrupt checkpoints never restart from empty state
"""

from __future__ import annotations

import pytest

from incidentlens_control_plane.agent.checkpoint import CheckpointCorruptError


async def test_resume_does_not_repeat_successful_tool_or_change_evidence_id(
    recovery_harness,
) -> None:
    """After an interrupted run, resume must NOT re-execute the same tool
    and must preserve the original evidence ID."""
    first = await recovery_harness.run_until_after_tool(
        incident_id="inc-recover",
        tool_name="search_logs",
    )
    assert first.tool_executions == 1
    evidence_id = first.state.evidence[0].id

    resumed = await recovery_harness.resume("inc-recover")

    assert resumed.tool_executions == 1
    assert resumed.state.evidence[0].id == evidence_id
    assert resumed.state.last_checkpoint_id != first.state.last_checkpoint_id


async def test_model_timeout_keeps_completed_evidence_and_is_resumable(
    recovery_harness,
) -> None:
    """When the model times out, completed evidence must be preserved
    and the investigation must be resumable with a healthy model."""
    result = await recovery_harness.run_with_model_timeout("inc-timeout")
    assert result.state.last_error_code == "model_timeout"
    assert result.state.evidence

    resumed = await recovery_harness.resume_with_healthy_model("inc-timeout")
    assert resumed.state.evidence[0].id == result.state.evidence[0].id


async def test_corrupt_checkpoint_never_restarts_from_empty_state(
    recovery_harness,
) -> None:
    """When a checkpoint is corrupt, the runtime must raise
    CheckpointCorruptError and never invoke the model."""
    # First start an investigation so the checkpoint table exists
    recovery_harness._model.interrupt_before_second_model_call = True
    await recovery_harness.engine.start(
        {"incident_id": "inc-corrupt", "service": "order-service"}
    )
    # Now corrupt the checkpoint
    await recovery_harness.insert_corrupt_checkpoint("inc-corrupt")
    # Resume should raise CheckpointCorruptError, not restart from empty
    with pytest.raises(CheckpointCorruptError):
        await recovery_harness.engine.resume("inc-corrupt")
    # start() already invoked the model twice (tool call + interrupt);
    # resume() must not invoke it again.
    invocations_after_resume = recovery_harness.model_invocations
    assert invocations_after_resume == 2  # only the start() invocations
