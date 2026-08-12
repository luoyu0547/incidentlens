"""Shared fixtures for web API tests.

The ``client`` fixture builds the real FastAPI application but injects a fake
remote transport factory, so connection lifecycle tests never touch the
network.  The remaining fixtures seed the runtime with a registered project,
an open host session, a pending approval, and an applied ChangeSet.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from incidentlens_control_plane.changes.types import ChangeSetStatus, FileChange
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
)
from incidentlens_control_plane.main import create_app
from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory


def make_web_log_record(
    message_redacted: str,
    *,
    now: datetime,
    log_id: str = "log-web-1",
) -> LogRecord:
    """Build a redacted LogRecord for the web-api fixtures' payments project."""
    severity = (
        LogSeverity.ERROR
        if message_redacted.startswith("ERROR")
        else LogSeverity.WARN
        if message_redacted.startswith("WARN")
        else LogSeverity.INFO
    )
    return LogRecord(
        log_id=log_id,
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor="offset:1",
        dedupe_key=hashlib.sha256(message_redacted.encode("utf-8")).hexdigest(),
        observed_at=now,
        event_time=None,
        severity=severity,
        message_redacted=message_redacted,
        redaction_summary={},
        normal_signal=None,
        correlation_key=None,
        evidence_ref_id=None,
        created_at=now,
    )


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a test client whose session manager uses a fake transport."""
    app = create_app(
        RuntimeSettings(data_dir=tmp_path / "data"),
        transport_factory=FakeTransportFactory(),
    )
    with TestClient(app) as client:
        yield client


@pytest.fixture
def runtime(client: TestClient):
    """Return the runtime services attached to the test application."""
    return client.app.state.runtime


@pytest.fixture
def project_payload(tmp_path: Path) -> dict[str, object]:
    """Sample project registration payload with a registered target/service."""
    return {
        "project_id": "payments",
        "display_name": "Payments",
        "local_source_paths": [str((tmp_path / "src").resolve())],
        "targets": [
            {
                "target_id": "dev-a",
                "host": "dev-a.example.test",
                "ssh_user": "deploy",
                "ssh_config_alias": "dev-a",
            }
        ],
        "services": [
            {
                "compose_service": "payment-api",
                "container_names": ["payments-api-1"],
                "local_source_path": str((tmp_path / "src").resolve()),
                "container_path_hints": ["/app"],
                "allowed_log_paths": ["/var/log/payment/*.log"],
                "protected_remote_paths": ["/opt/payments/.env"],
            }
        ],
    }


@pytest.fixture
def registered_project(
    client: TestClient, project_payload: dict[str, object]
) -> str:
    """Register the payments project and return its project id."""
    response = client.post("/api/projects", json=project_payload)
    assert response.status_code == 201
    return "payments"


@pytest.fixture
def connected_host(client: TestClient, registered_project: str) -> str:
    """Open a host session against the registered project and return its id."""
    response = client.post(
        "/api/remote-sessions",
        json={"project_id": "payments", "target_id": "dev-a"},
    )
    assert response.status_code == 201
    return response.json()["session_id"]


@pytest.fixture
def pending_approval(client: TestClient, runtime) -> str:
    """Create a pending approval and return its id."""
    intent = {
        "kind": "docker.restart",
        "target_id": "dev-a",
        "container": "payments-api-1",
        "argv": ["docker", "restart", "payments-api-1"],
    }
    record = asyncio.run(runtime.approvals.request(intent))
    return record.approval_id


@pytest.fixture
def applied_changeset(
    client: TestClient, runtime, registered_project: str
) -> str:
    """Create a changeset in APPLIED status over a protected path."""
    changeset_id = "chs-web-1"
    store = runtime.change_store
    store.create_changeset(
        changeset_id=changeset_id,
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        files=(
            FileChange(
                file_change_id="op-1",
                scope="host",
                remote_path="/opt/payments/.env",
                expected_sha256=None,
                replacement_sha256="b" * 64,
                diff_text="",
                original_metadata={},
                local_backup_ref="backup.enc",
                remote_backup_path=(
                    "/opt/payments/.env.incidentlens-backup.20260810T120000.000000Z"
                ),
                temp_path="/opt/payments/.env.incidentlens-tmp-chs-web-1",
                applied=True,
                validation_result="validated",
                rollback_result=None,
            ),
        ),
        verification_plan="run syntax checks and compare service behavior",
        rollback_plan="restore the verified timestamped backup",
    )
    for status in (
        ChangeSetStatus.PREFLIGHTED,
        ChangeSetStatus.LOCALLY_BACKED_UP,
        ChangeSetStatus.REMOTELY_BACKED_UP,
        ChangeSetStatus.APPLIED,
    ):
        store.transition(changeset_id, status)
    return changeset_id
