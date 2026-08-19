from __future__ import annotations

import sys
from pathlib import Path

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProviderRegistry,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import StopSignal
from incidentlens_control_plane.investigation.types import StopReason
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from record_live_model_demo import run_live_model_workflow  # noqa: E402


@pytest.mark.asyncio
async def test_live_workflow_returns_the_recording_shape(tmp_path) -> None:
    target = TargetRegistration(
        target_id="recording-target", host="127.0.0.1", ssh_user="incidentlens"
    )
    service = ServiceRegistration(
        compose_service="test-ssh",
        container_names=("test-ssh",),
        allowed_log_paths=("/workspace/service/live.log",),
        allowed_host_paths=(),
        allowed_container_paths=(),
    )
    settings = RuntimeSettings(
        data_dir=tmp_path / "runtime",
        report_output_dir=tmp_path / "reports",
        max_active_investigations=2,
    )
    registry = FakeProviderRegistry()
    # The workflow assigns a stable script id through its test-only override.
    registry.set_script(
        "run-recording",
        [
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED,
                    summary="recorded",
                )
            )
        ],
    )
    result = await run_live_model_workflow(
        settings,
        FakeTransportFactory(),
        target,
        service,
        fake_provider_registry=registry,
    )
    assert set(result.to_record()) == {
        "investigation", "run", "rounds", "tool_calls", "transcript",
        "compact_boundaries", "evidence", "conclusions", "hooks", "report",
    }
    assert result.run["status"] in {"completed", "failed"}


def test_project_registration_shape_is_constructible() -> None:
    registration = ProjectRegistration(
        project_id="recording-project",
        display_name="Live MaaS recording",
        targets=(),
        services=(),
    )
    assert registration.project_id == "recording-project"
