from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from pydantic import ValidationError


def valid_registration(tmp_path: Path) -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name="Payments",
        local_source_paths=(tmp_path.resolve(),),
        targets=(
            TargetRegistration(
                target_id="dev-a",
                host="dev-a.example.test",
                ssh_user="deploy",
                ssh_config_alias="dev-a",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service="payment-api",
                container_names=("payments-api-1",),
                local_source_path=tmp_path.resolve(),
                container_path_hints=("/app",),
                allowed_log_paths=("/var/log/payment/*.log",),
            ),
        ),
    )


def test_registration_accepts_paths_and_associations(tmp_path: Path) -> None:
    registration = valid_registration(tmp_path)
    record = ProjectRecord.from_registration(
        registration,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert record.project_id == "payments"
    assert record.services[0].local_source_path == tmp_path.resolve()
    assert record.created_at.tzinfo is UTC


def test_registration_rejects_relative_local_source_path() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(Path("relative/source"),),
            targets=(),
            services=(),
        )


def test_registration_rejects_duplicate_service_names(tmp_path: Path) -> None:
    service = ServiceRegistration(compose_service="api")
    with pytest.raises(ValidationError, match="compose_service"):
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(tmp_path.resolve(),),
            targets=(),
            services=(service, service),
        )


def test_service_registration_accepts_absolute_remote_roots() -> None:
    service = ServiceRegistration(
        compose_service="payment-api",
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
        allowed_container_paths=(PurePosixPath("/app"),),
        protected_remote_paths=(PurePosixPath("/opt/payments/.env"),),
    )
    assert service.allowed_container_paths == (PurePosixPath("/app"),)


def test_service_registration_rejects_relative_remote_root() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ServiceRegistration(
            compose_service="payment-api",
            allowed_host_paths=(PurePosixPath("opt/payments"),),
        )
