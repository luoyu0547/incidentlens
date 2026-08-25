from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope, LogSourceKind
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.project_registry.types import ProjectRegistration
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

NOW = datetime(2026, 8, 25, 14, 0, tzinfo=UTC)

OPERATOR_TOKEN = "operator-a-bearer-token"
SCOPED_TOKEN = "scoped-b-bearer-token"
READLESS_TOKEN = "operate-only-bearer-token"

PROFILES = json.dumps(
    [
        {
            "principal_id": "operator-a",
            "display_name": "Operator A",
            "scopes": ["read", "operate", "approve", "admin"],
            "token_digest": hashlib.sha256(OPERATOR_TOKEN.encode()).hexdigest(),
        },
        {
            "principal_id": "scoped-b",
            "display_name": "Scoped B",
            "scopes": ["read"],
            "allowed_target_ids": ["dev-a"],
            "token_digest": hashlib.sha256(SCOPED_TOKEN.encode()).hexdigest(),
        },
        {
            "principal_id": "operate-only",
            "display_name": "Operate Only",
            "scopes": ["operate"],
            "token_digest": hashlib.sha256(READLESS_TOKEN.encode()).hexdigest(),
        },
    ]
)


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    app = create_app(
        RuntimeSettings(
            data_dir=tmp_path / "data",
            auth_profiles_json=PROFILES,
            secure_cookies=False,
        ),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as test_client:
        yield test_client


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(client: TestClient) -> None:
    payments_payload = {
        "project_id": "payments",
        "display_name": "Payments",
        "targets": [
            {"target_id": "dev-a", "host": "dev-a.example.test", "ssh_user": "deploy"},
        ],
        "services": [
            {
                "compose_service": "payment-api",
                "container_names": ["payments-api-1"],
                "allowed_host_paths": ["/srv/payments"],
                "protected_remote_paths": ["/srv/payments/.env"],
            },
        ],
    }
    worker_payload = {
        "project_id": "ops",
        "display_name": "Ops",
        "targets": [
            {"target_id": "dev-b", "host": "dev-b.example.test", "ssh_user": "deploy"},
        ],
        "services": [
            {
                "compose_service": "worker-api",
                "container_names": ["worker-api-1"],
                "allowed_host_paths": ["/srv/ops"],
                "protected_remote_paths": ["/srv/ops/worker.env"],
            },
        ],
    }
    response = client.post("/api/projects", json=payments_payload)
    assert response.status_code == 201, response.text
    response = client.post("/api/projects", json=worker_payload)
    assert response.status_code == 201, response.text
    runtime = client.app.state.runtime
    runtime.log_store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payments/api.log",
        opt_in_streaming=True,
        created_by="tester",
        now=NOW - timedelta(minutes=3),
    )
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="checkout errors",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=4),
            updated_at=NOW - timedelta(minutes=1),
        )
    )


def test_overview_route_requires_read_scope(client: TestClient) -> None:
    response = client.get("/api/v1/overview", headers=_auth(READLESS_TOKEN))

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "permission_denied"


def test_overview_and_service_routes_filter_targets_and_stay_safe(client: TestClient) -> None:
    _seed(client)

    overview = client.get("/api/v1/overview", headers=_auth(SCOPED_TOKEN))
    assert overview.status_code == 200, overview.text
    body = overview.json()
    assert [target["target_id"] for target in body["targets"]] == ["dev-a"]
    assert "/srv/payments/.env" not in overview.text

    service = client.get("/api/v1/services/payment-api", headers=_auth(SCOPED_TOKEN))
    assert service.status_code == 200, service.text
    detail = service.json()
    assert detail["target_ids"] == ["dev-a"]
    assert detail["issue_ids"] == ["iss_inv-1"]
    assert "approve" not in service.text
    assert "reject" not in service.text
    assert "/var/log/payments/api.log" not in service.text


def test_service_route_hides_unauthorized_existence(client: TestClient) -> None:
    _seed(client)

    response = client.get("/api/v1/services/worker-api", headers=_auth(SCOPED_TOKEN))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "resource_not_found"


def test_openapi_exports_new_read_operations(client: TestClient) -> None:
    schema = client.app.openapi()

    assert schema["paths"]["/api/v1/overview"]["get"]["operationId"] == "getOverview"
    assert schema["paths"]["/api/v1/services/{service_id}"]["get"]["operationId"] == (
        "getService"
    )
