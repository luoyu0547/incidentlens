"""Opt-in real MaaS checks for persisted harness invariants.

Collection is harmless unless INCIDENTLENS_RUN_LIVE_MODEL_TESTS=1 is set: the
module gate runs before the disposable-target fixture can perform any setup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from incidentlens_control_plane.config import RuntimeSettings

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from eval.metrics import evaluate_trace  # noqa: E402
from eval.types import HarnessTrace  # noqa: E402
from record_live_model_demo import run_live_model_workflow  # noqa: E402
from test_live_agent_runtime import live_target as _live_target  # noqa: E402

pytestmark = pytest.mark.skipif(
    os.environ.get("INCIDENTLENS_RUN_LIVE_MODEL_TESTS") != "1",
    reason="INCIDENTLENS_RUN_LIVE_MODEL_TESTS=1 is not set",
)


@pytest.fixture(autouse=True)
def _enable_disposable_target():
    previous = os.environ.get("INCIDENTLENS_RUN_LIVE_AGENT_TESTS")
    os.environ["INCIDENTLENS_RUN_LIVE_AGENT_TESTS"] = "1"
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("INCIDENTLENS_RUN_LIVE_AGENT_TESTS", None)
        else:
            os.environ["INCIDENTLENS_RUN_LIVE_AGENT_TESTS"] = previous


live_target = _live_target


def live_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings.from_environment().model_copy(
        update={"data_dir": tmp_path / "runtime", "agent_mode": "llm_agent"}
    )


def count_overflow_retries(hooks: tuple[dict[str, object], ...]) -> int:
    return sum(
        event.get("payload", {}).get("metadata", {}).get("mode") == "reactive"
        and event.get("payload", {}).get("hook_type") == "PreCompact"
        for event in hooks
        if isinstance(event.get("payload"), dict)
        and isinstance(event["payload"].get("metadata"), dict)
    )


@pytest.mark.asyncio
async def test_real_maas_run_satisfies_harness_invariants(live_target, tmp_path) -> None:
    settings = RuntimeSettings.from_environment().model_copy(
        update={"data_dir": tmp_path / "runtime", "agent_mode": "llm_agent"}
    )
    result = await run_live_model_workflow(
        settings, live_target.factory, live_target.target, live_target.service
    )
    metrics = evaluate_trace(HarnessTrace.from_live_result(result))
    assert result.report["provider_type"] == "XfyunMaaSProvider"
    assert result.report["provider_model"] == settings.llm_active_model.removeprefix("xfyun-")
    assert metrics.foreign_evidence_count == 0
    assert metrics.scope_policy_bypass_count == 0
    assert metrics.unapproved_mutation_count == 0
    assert metrics.tool_pairing_rate == 1.0
    assert metrics.child_exactly_once_rate == 1.0
    if result.run["status"] == "completed":
        assert metrics.grounded_completion is True


@pytest.mark.asyncio
async def test_real_maas_small_window_compacts_or_pauses_safely(live_target, tmp_path) -> None:
    settings = live_settings(tmp_path).model_copy(
        update={
            "agent_context_window_tokens": 8_000,
            "agent_context_max_output_tokens": 1_000,
            "agent_context_reserve_tokens": 1_000,
        }
    )
    result = await run_live_model_workflow(
        settings,
        live_target.factory,
        live_target.target,
        live_target.service,
        context_overrides={"prefill_complete_groups": 12},
    )
    assert result.compact_boundaries or (
        result.run["status"] == "paused_budget" and count_overflow_retries(result.hooks) >= 1
    )
    assert count_overflow_retries(result.hooks) <= 1
    assert evaluate_trace(HarnessTrace.from_live_result(result)).scope_policy_bypass_count == 0
