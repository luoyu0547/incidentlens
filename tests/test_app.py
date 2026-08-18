from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import app


def test_healthz_does_not_claim_a_remote_connection() -> None:
    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "remote_execution": "not_configured"}


def test_app_lifespan_creates_local_database(tmp_path: Path) -> None:
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
        xfyun_maas_api_key="test-key",
        llm_active_model="spark-x",
    )


def _fake_settings(tmp_path: Path) -> RuntimeSettings:
    return RuntimeSettings(data_dir=tmp_path / "incidentlens")


def test_llm_runtime_injects_maas_compactor(tmp_path: Path) -> None:
    from incidentlens_control_plane.investigation.xfyun_compactor import (
        XfyunMaaSCompactor,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.runtime import build_runtime

    settings = _llm_settings(tmp_path)
    runtime = build_runtime(settings, transport_factory=FakeTransportFactory())
    assert isinstance(runtime.context_manager._compactor, XfyunMaaSCompactor)


def test_fake_runtime_does_not_inject_network_compactor(tmp_path: Path) -> None:
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.runtime import build_runtime

    settings = _fake_settings(tmp_path)
    runtime = build_runtime(settings, transport_factory=FakeTransportFactory())
    assert runtime.context_manager._compactor is None
