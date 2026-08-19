from pathlib import Path

from fastapi.testclient import TestClient
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
