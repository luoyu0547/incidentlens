from __future__ import annotations

import json
import sys
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock

import pytest
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.fake_provider import (
    FakeProviderRegistry,
    RequestToolsStep,
    StopStep,
)
from incidentlens_control_plane.investigation.provider import (
    Conclusion,
    ConversationRequest,
    StopSignal,
    ToolRequest,
)
from incidentlens_control_plane.investigation.types import StopReason, ToolResultBlock
from incidentlens_control_plane.project_registry.types import (
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import record_live_model_demo  # noqa: E402
from record_live_model_demo import run_live_model_workflow  # noqa: E402


def _target() -> TargetRegistration:
    return TargetRegistration(
        target_id="recording-target", host="127.0.0.1", ssh_user="incidentlens"
    )


def _service() -> ServiceRegistration:
    return ServiceRegistration(
        compose_service="test-ssh",
        container_names=("test-ssh",),
        allowed_log_paths=("/workspace/service/live.log",),
        allowed_host_paths=(PurePosixPath("/workspace/service"),),
        allowed_container_paths=(PurePosixPath("/workspace/service"),),
    )


def _settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        data_dir=tmp_path / "runtime",
        report_output_dir=tmp_path / "reports",
        max_active_investigations=2,
    )


class _RecordingRegistry(FakeProviderRegistry):
    """Test-only adapter that grounds this recording script's final stop."""

    def pop_step(self, run_id: str, request: ConversationRequest) -> object:
        step = super().pop_step(run_id, request)
        if not isinstance(step, StopStep) or step.conclusion is None:
            return step
        evidence_ids = tuple(
            evidence_id
            for message in request.messages
            for block in message.blocks
            if isinstance(block, ToolResultBlock)
            for evidence_id in block.evidence_ids
        )
        if not evidence_ids:
            return step
        return step.model_copy(
            update={"conclusion": step.conclusion.model_copy(update={"evidence_ids": evidence_ids})}
        )


def _registry() -> FakeProviderRegistry:
    registry = _RecordingRegistry()
    registry.set_script(
        "pending",
        [
            RequestToolsStep(
                tool_requests=(
                    ToolRequest(
                        tool_call_id="log-1",
                        tool_name="log_query",
                        arguments={
                            "service_name": "test-ssh",
                            "source_kind": "file",
                            "source_ref": "/workspace/service/live.log",
                        },
                    ),
                ),
            ),
            StopStep(
                stop_signal=StopSignal(
                    stop_reason=StopReason.COMPLETED,
                    summary="recorded",
                ),
                conclusion=Conclusion(
                    summary="recorded conclusion",
                    evidence_ids=("__latest__",),
                ),
            ),
        ],
    )
    return registry


@pytest.mark.asyncio
async def test_live_workflow_returns_recording_shape_and_scripted_completion(tmp_path) -> None:
    registry = _registry()
    factory = FakeTransportFactory()
    transport = await factory.connect(_target())
    await transport.write_bytes(
        PurePosixPath("/workspace/service/live.log"),
        b"ERROR checkout timeout\n",
    )
    result = await run_live_model_workflow(
        _settings(tmp_path),
        factory,
        _target(),
        _service(),
        fake_provider_registry=registry,
    )
    assert set(result.to_record()) == {
        "investigation",
        "run",
        "rounds",
        "tool_calls",
        "evidence",
        "conclusions",
        "report",
    }
    assert result.transcript
    assert result.compact_boundaries == ()
    assert result.hooks
    assert result.provider_type
    assert result.run["status"] == "completed"
    run_id = result.run["agent_run_id"]
    assert registry.requests(run_id)
    assert registry.remaining(run_id) == 0
    assert result.conclusions
    assert all("__latest__" not in item["evidence_ids"] for item in result.conclusions)
    owned_evidence_ids = {item["evidence_ref_id"] for item in result.evidence}
    assert set(result.conclusions[0]["evidence_ids"]) <= owned_evidence_ids
    assert result.transcript[0]["blocks"][0]["text"].startswith("Investigation ")
    assert "Symptom:" in result.transcript[0]["blocks"][0]["text"]


@pytest.mark.asyncio
async def test_context_overrides_prefill_complete_groups_and_whitelists_keys(tmp_path) -> None:
    registry = _registry()
    factory = FakeTransportFactory()
    transport = await factory.connect(_target())
    await transport.write_bytes(
        PurePosixPath("/workspace/service/live.log"), b"ERROR checkout timeout\n"
    )
    result = await run_live_model_workflow(
        _settings(tmp_path),
        factory,
        _target(),
        _service(),
        context_overrides={
            "agent_context_max_message_groups": 12,
            "prefill_complete_groups": 2,
        },
        fake_provider_registry=registry,
    )
    assert result.run["status"] == "completed"
    assert len(result.transcript) >= 4
    assert "prefill group 0" in result.transcript[1]["blocks"][0]["text"]


@pytest.mark.asyncio
async def test_context_overrides_reject_unknown_keys(tmp_path) -> None:
    with pytest.raises(ValueError, match="unsupported context override"):
        await run_live_model_workflow(
            _settings(tmp_path),
            FakeTransportFactory(),
            _target(),
            _service(),
            context_overrides={"not_a_context_setting": 1},
            fake_provider_registry=_registry(),
        )


def _result(report_root: Path) -> record_live_model_demo.LiveModelRunResult:
    return record_live_model_demo.LiveModelRunResult(
        investigation={"investigation_id": "inv-1"},
        run={"status": "completed", "usage": {"rounds": 1, "tool_calls": 0}},
        rounds=(),
        tool_calls=(),
        transcript=(),
        compact_boundaries=(),
        evidence=(),
        conclusions=(),
        hooks=(),
        report={"markdown_path": "report.md"},
        markdown_path=report_root / "inv-1.md",
        html_path=report_root / "inv-1.html",
    )


def test_main_serializes_callable_result_and_copies_artifacts(tmp_path, monkeypatch) -> None:
    output = tmp_path / "workflow.json"
    report_dir = tmp_path / "copied"
    generated = tmp_path / "reports"
    expected = _result(generated)
    generated.mkdir()
    md = generated / "inv-1.md"
    html = generated / "inv-1.html"
    md.write_text("markdown")
    html.write_text("html")

    monkeypatch.setattr(
        record_live_model_demo,
        "run_live_model_workflow",
        AsyncMock(return_value=expected),
    )
    monkeypatch.setattr(
        record_live_model_demo,
        "_run",
        lambda command, **kwargs: "127.0.0.1:2222" if "port" in command else "container",
    )
    monkeypatch.setattr(record_live_model_demo, "_host_key", lambda name: "ssh-key")
    monkeypatch.setattr(
        record_live_model_demo.subprocess,
        "run",
        lambda *args, **kwargs: type("R", (), {"stdout": "container", "returncode": 0})(),
    )
    monkeypatch.setattr(record_live_model_demo, "_seed_log", AsyncMock())
    monkeypatch.setattr(record_live_model_demo, "build_runtime", lambda settings, **kwargs: None)
    # Avoid Docker setup details while retaining the original CLI invocation path.
    monkeypatch.setattr(record_live_model_demo.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    runtime_settings = type(
        "S",
        (),
        {
            "from_environment": classmethod(
                lambda cls: RuntimeSettings(
                    data_dir=tmp_path / "runtime",
                    report_output_dir=generated,
                    agent_mode="fake",
                )
            )
        },
    )
    monkeypatch.setattr(record_live_model_demo, "RuntimeSettings", runtime_settings)
    monkeypatch.setattr(
        sys,
        "argv",
        ["record_live_model_demo.py", "--output", str(output), "--report-dir", str(report_dir)],
    )

    record_live_model_demo.main()
    assert json.loads(output.read_text()) == expected.to_record()
    assert (report_dir / "live-model-report.md").read_text() == "markdown"
    assert (report_dir / "live-model-report.html").read_text() == "html"


def test_effective_settings_validates_context_override_values(tmp_path) -> None:
    settings = _settings(tmp_path)
    effective, prefill = record_live_model_demo._effective_settings(
        settings, {"agent_context_max_message_groups": 12}
    )
    assert effective.agent_context_max_message_groups == 12
    assert prefill == 0
    with pytest.raises(ValueError):
        record_live_model_demo._effective_settings(
            settings, {"agent_context_max_message_groups": 1}
        )
    with pytest.raises(ValueError):
        record_live_model_demo._effective_settings(
            settings, {"agent_context_max_message_groups": "12"}
        )
