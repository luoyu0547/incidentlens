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
        allowed_validation_scripts=(
            PurePosixPath("/opt/payments/scripts/request_matrix.py"),
        ),
    )
    assert service.allowed_container_paths == (PurePosixPath("/app"),)
    assert service.allowed_validation_scripts == (
        PurePosixPath("/opt/payments/scripts/request_matrix.py"),
    )


def test_service_registration_rejects_relative_remote_root() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ServiceRegistration(
            compose_service="payment-api",
            allowed_host_paths=(PurePosixPath("opt/payments"),),
        )


def test_target_compose_files_must_be_absolute_and_inside_project_directory() -> None:
    with pytest.raises(ValidationError, match="compose_files"):
        TargetRegistration(
            target_id="target",
            host="example.test",
            ssh_user="deploy",
            compose_working_directory=PurePosixPath("/srv/app"),
            compose_files=(PurePosixPath("compose.cloud.yaml"),),
        )

    with pytest.raises(ValidationError, match="compose_files"):
        TargetRegistration(
            target_id="target",
            host="example.test",
            ssh_user="deploy",
            compose_working_directory=PurePosixPath("/srv/app"),
            compose_files=(PurePosixPath("/srv/other/compose.yaml"),),
        )


def test_target_validation_base_url_must_be_loopback_http() -> None:
    target = TargetRegistration(
        target_id="target",
        host="example.test",
        ssh_user="deploy",
        validation_base_url="http://localhost:18080",
    )
    assert target.validation_base_url == "http://localhost:18080"

    with pytest.raises(ValidationError, match="validation_base_url"):
        TargetRegistration(
            target_id="target",
            host="example.test",
            ssh_user="deploy",
            validation_base_url="https://example.com",
        )
