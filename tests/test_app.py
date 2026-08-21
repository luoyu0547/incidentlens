from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import app


def test_healthz_does_not_claim_a_remote_connection() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "remote_execution": "not_configured"}


def test_runtime_shares_hook_runner_and_delegation_validator(tmp_path: Path) -> None:
    from incidentlens_control_plane.config import RuntimeSettings
    from incidentlens_control_plane.runtime import build_runtime

    runtime = build_runtime(RuntimeSettings(data_dir=tmp_path / "incidentlens"))
    orchestrator = runtime.investigations._orchestrator
    executor = runtime.investigations._executor
    assert orchestrator._context is runtime.context_manager
    assert orchestrator._hooks is executor._hooks
    assert orchestrator._delegation is executor._delegation


    from incidentlens_control_plane.config import RuntimeSettings
    from incidentlens_control_plane.main import create_app

    settings = RuntimeSettings(data_dir=tmp_path / "incidentlens")
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {
            "status": "ok",
            "remote_execution": "not_configured",
        }

    assert (settings.data_dir / "runtime.db").is_file()


# ---------------------------------------------------------------------------
# Runtime composition: compactor injection
# ---------------------------------------------------------------------------


def _llm_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(
        data_dir=tmp_path / "incidentlens",
        agent_mode="llm_agent",
        llm_api_key="test-key",
        llm_active_model="spark-x",
        llm_base_url="https://llm.example/v1",
    )


def _fake_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(data_dir=tmp_path / "incidentlens")


def test_llm_runtime_injects_openai_compatible_compactor(tmp_path: Path) -> None:
    from incidentlens_control_plane.investigation.openai_compactor import (
        OpenAICompatibleCompactor,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.runtime import build_runtime

    settings = _llm_settings(tmp_path)
    runtime = build_runtime(settings, transport_factory=FakeTransportFactory())
    assert isinstance(runtime.context_manager._compactor, OpenAICompatibleCompactor)


def test_fake_runtime_does_not_inject_network_compactor(tmp_path: Path) -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.runtime import build_runtime

    settings = _fake_settings(tmp_path)
    runtime = build_runtime(settings, transport_factory=FakeTransportFactory())
    assert runtime.context_manager._compactor is None
