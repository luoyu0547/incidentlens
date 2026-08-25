from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.evidence.types import EvidenceKind, EvidenceRef
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogRecord, LogScope, LogSeverity, LogSourceKind
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory

NOW = datetime(2026, 8, 25, 17, 0, tzinfo=UTC)

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


def _issue_cursor(created_at: str, issue_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at, "issue_id": issue_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "is1_" + base64.urlsafe_b64encode(payload).decode("ascii")


def _investigation_cursor(created_at: str, investigation_id: str) -> str:
    payload = json.dumps(
        {"created_at": created_at, "investigation_id": investigation_id},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "iv1_" + base64.urlsafe_b64encode(payload).decode("ascii")


def _seed(client: TestClient) -> tuple[str, str, str]:
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
    assert client.post("/api/projects", json=payments_payload).status_code == 201
    assert client.post("/api/projects", json=worker_payload).status_code == 201

    runtime = client.app.state.runtime
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            symptom="checkout errors",
            status=InvestigationStatus.WAITING_APPROVAL,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=4),
            updated_at=NOW,
        )
    )
    runtime.investigation_store.create_investigation(
        Investigation(
            investigation_id="inv-2",
            incident_id="inc-2",
            project_id="ops",
            target_id="dev-b",
            service="worker-api",
            symptom="queue stalls",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW - timedelta(minutes=3),
            updated_at=NOW,
        )
    )
    runtime.change_store.create_changeset(
        changeset_id="chs-1",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        files=(
            FileChange(
                file_change_id="fc-1",
                scope="host",
                remote_path="/srv/payments/app.py",
                expected_sha256="a" * 64,
                replacement_sha256="b" * 64,
            ),
        ),
    )
    for status in (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
    ):
        runtime.change_store.transition("chs-1", status)
    record = LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payments/api.log",
        cursor="offset:1",
        dedupe_key=hashlib.sha256(b"log-1").hexdigest(),
        observed_at=NOW - timedelta(minutes=2),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted="ERROR token=[REDACTED_TOKEN] checkout failed",
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key=None,
        evidence_ref_id=None,
        created_at=NOW - timedelta(minutes=2),
    )
    runtime.log_store.append_batch((record,))
    evidence = runtime.evidence.create_from_log_record(
        record,
        incident_id="inc-1",
        created_by="tester",
        now=NOW - timedelta(minutes=2),
    )
    runtime.evidence.create(
        EvidenceRef(
            evidence_ref_id="ev-worker",
            incident_id="inc-2",
            evidence_kind=EvidenceKind.COMMAND_OUTPUT,
            agent_run_id=None,
            project_id="ops",
            target_id="dev-b",
            service_name="worker-api",
            source_ref="host:dev-b",
            content_redacted="safe output",
            content_sha256=hashlib.sha256(b"safe output").hexdigest(),
            redaction_summary={},
            truncation=None,
            metadata={"command": "status"},
            created_at=NOW,
            created_by="tester",
        )
    )
    approval = asyncio.run(
        runtime.approvals.request(
            {"kind": "docker.restart", "target_id": "dev-a", "service": "payment-api"},
            now=NOW - timedelta(minutes=1),
            target_id="dev-a",
            service="payment-api",
            investigation_id="inv-1",
        )
    )
    return "iss_inv-1", approval.approval_id, evidence.evidence_ref_id


def test_web_read_routes_require_read_scope(client: TestClient) -> None:
    issue_id, _approval_id, evidence_id = _seed(client)

    for path in (
        "/api/v1/issues",
        f"/api/v1/issues/{issue_id}",
        "/api/v1/investigations",
        "/api/v1/investigations/inv-1/summary",
        f"/api/v1/evidence/{evidence_id}",
    ):
        response = client.get(path, headers=_auth(READLESS_TOKEN))
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "permission_denied"


def test_web_read_routes_filter_by_authorized_target_and_paginate(client: TestClient) -> None:
    issue_id, approval_id, evidence_id = _seed(client)

    issues = client.get("/api/v1/issues?limit=1", headers=_auth(SCOPED_TOKEN))
    assert issues.status_code == 200, issues.text
    body = issues.json()
    assert [item["issue_id"] for item in body["items"]] == [issue_id]
    assert body["has_more"] is False
    assert body["items"][0]["pending_approval_ids"] == [approval_id]
    assert "/srv/payments/.env" not in issues.text
    assert "content_sha256" not in issues.text

    issue = client.get(f"/api/v1/issues/{issue_id}", headers=_auth(SCOPED_TOKEN))
    assert issue.status_code == 200
    assert issue.json()["issue_id"] == issue_id

    investigations = client.get(
        "/api/v1/investigations?limit=1", headers=_auth(SCOPED_TOKEN)
    )
    assert investigations.status_code == 200, investigations.text
    inv_body = investigations.json()
    assert [item["investigation_id"] for item in inv_body["items"]] == ["inv-1"]
    assert inv_body["has_more"] is False

    summary = client.get(
        "/api/v1/investigations/inv-1/summary", headers=_auth(SCOPED_TOKEN)
    )
    assert summary.status_code == 200, summary.text
    assert summary.json()["pending_approval_ids"] == [approval_id]

    detail = client.get(f"/api/v1/evidence/{evidence_id}", headers=_auth(SCOPED_TOKEN))
    assert detail.status_code == 200, detail.text
    assert detail.json()["content_redacted"].startswith("ERROR")
    assert detail.json()["provenance"]["log_cursor"].startswith("lc1_")
    assert "content_sha256" not in detail.text
    assert "/var/log/payments/api.log" not in detail.text
    assert "metadata" not in detail.text


def test_web_read_routes_hide_unauthorized_resources(client: TestClient) -> None:
    _issue_id, _approval_id, _evidence_id = _seed(client)

    unauthorized_issue = client.get(
        "/api/v1/issues/iss_inv-2", headers=_auth(SCOPED_TOKEN)
    )
    assert unauthorized_issue.status_code == 404
    assert unauthorized_issue.json()["error"]["code"] == "resource_not_found"

    unauthorized_summary = client.get(
        "/api/v1/investigations/inv-2/summary", headers=_auth(SCOPED_TOKEN)
    )
    assert unauthorized_summary.status_code == 404
    assert unauthorized_summary.json()["error"]["code"] == "resource_not_found"

    unauthorized_evidence = client.get(
        "/api/v1/evidence/ev-worker", headers=_auth(SCOPED_TOKEN)
    )
    assert unauthorized_evidence.status_code == 404
    assert unauthorized_evidence.json()["error"]["code"] == "resource_not_found"


def test_openapi_exports_web_read_operations(client: TestClient) -> None:
    schema = client.app.openapi()

    assert schema["paths"]["/api/v1/issues"]["get"]["operationId"] == "listIssues"
    assert schema["paths"]["/api/v1/issues/{issue_id}"]["get"]["operationId"] == "getIssue"
    assert schema["paths"]["/api/v1/investigations"]["get"]["operationId"] == (
        "listInvestigationSummaries"
    )
    assert schema["paths"]["/api/v1/investigations/{investigation_id}/summary"]["get"][
        "operationId"
    ] == "getInvestigationSummary"
    assert schema["paths"]["/api/v1/evidence/{evidence_ref_id}"]["get"][
        "operationId"
    ] == "getEvidence"


def test_web_read_list_routes_reject_timezone_naive_cursors(client: TestClient) -> None:
    _seed(client)

    issues = client.get(
        "/api/v1/issues",
        params={"after": _issue_cursor("2026-08-25T17:00:00", "iss_inv-1")},
        headers=_auth(SCOPED_TOKEN),
    )
    assert issues.status_code == 422
    assert issues.json()["error"]["code"] == "cursor_invalid"

    investigations = client.get(
        "/api/v1/investigations",
        params={"after": _investigation_cursor("2026-08-25T17:00:00", "inv-1")},
        headers=_auth(SCOPED_TOKEN),
    )
    assert investigations.status_code == 422
    assert investigations.json()["error"]["code"] == "cursor_invalid"
