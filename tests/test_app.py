from pathlib import Path

from fastapi.testclient import TestClient
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
