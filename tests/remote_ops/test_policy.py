from __future__ import annotations

from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.remote_ops.policy import RemoteOperationPolicy
from incidentlens_control_plane.remote_ops.types import (
    ChangeControls,
    ContainerScope,
    FileEditRequest,
    FileOperationKind,
    FileOperationRequest,
    FileWriteRequest,
    HostScope,
    OperationKind,
    OperationRisk,
    RemoteAction,
    RuntimeKind,
    ShellRequest,
    TargetProfile,
    TextReplacement,
)
from pydantic import ValidationError


@pytest.fixture
def policy() -> RemoteOperationPolicy:
    target = TargetProfile(
        target_id="prod-a",
        host="10.0.0.8",
        ssh_user="incidentlens",
        runtime=RuntimeKind.DOCKER_COMPOSE,
        allowed_services=frozenset({"orders"}),
        credential_ref="vault://incidentlens/prod-a",
    )
    return RemoteOperationPolicy({target.target_id: target})


def action(operation: OperationKind, **kwargs: object) -> RemoteAction:
    return RemoteAction(
        target_id="prod-a",
        operation=operation,
        service="orders",
        incident_id="inc-123",
        **kwargs,
    )


def test_read_operation_is_allowed_for_allowlisted_service(policy: RemoteOperationPolicy) -> None:
    decision = policy.evaluate(action(OperationKind.COLLECT_LOGS, log_query="level:error"))

    assert decision.allowed is True
    assert decision.reason == "allowlisted read-only operation"


def test_unknown_target_is_rejected(policy: RemoteOperationPolicy) -> None:
    decision = policy.evaluate(
        RemoteAction(
            target_id="unknown",
            operation=OperationKind.INSPECT_CONFIG,
            service="orders",
            incident_id="inc-123",
        )
    )

    assert decision.allowed is False
    assert decision.reason == "target is not registered"


def test_apply_change_requires_all_safety_controls(policy: RemoteOperationPolicy) -> None:
    decision = policy.evaluate(action(OperationKind.APPLY_CHANGE))

    assert decision.allowed is False
    assert set(decision.required_gates) == {
        "backup",
        "human_approval",
        "verification_plan",
        "rollback_plan",
    }


def test_apply_change_is_allowed_only_after_reversible_approval(
    policy: RemoteOperationPolicy,
) -> None:
    controls = ChangeControls(
        change_ticket="CHG-12",
        backup_ref="backup://inc-123/orders-compose.yaml",
        approval_id="approval-44",
        verification_plan="check health endpoint and error rate",
        rollback_plan="restore backup and restart only orders",
    )

    decision = policy.evaluate(action(OperationKind.APPLY_CHANGE, change_controls=controls))

    assert decision.allowed is True
    assert decision.required_gates == ("post_change_verification",)


def test_raw_shell_input_is_not_part_of_the_contract() -> None:
    with pytest.raises(ValidationError):
        RemoteAction(
            target_id="prod-a",
            operation=OperationKind.COLLECT_LOGS,
            service="orders",
            incident_id="inc-123",
            command="docker rm -f orders",
        )


def test_typed_requests_never_contain_credentials() -> None:
    request = FileOperationRequest(
        operation_id="op-1",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=ContainerScope(container="payments-api-1"),
        kind=FileOperationKind.READ,
        path=PurePosixPath("/app/service.py"),
    )
    assert "credential" not in request.model_dump()
    assert request.scope.kind == "container"


def test_shell_request_requires_nonempty_reason() -> None:
    with pytest.raises(ValidationError):
        ShellRequest(
            operation_id="op-2",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            scope=HostScope(),
            command="pwd",
            reason="",
        )


def test_operation_risks_are_ordered_by_explicit_policy_not_enum_value() -> None:
    assert set(OperationRisk) == {
        OperationRisk.AUTO_READ,
        OperationRisk.BACKUP_REQUIRED,
        OperationRisk.APPROVAL_REQUIRED,
        OperationRisk.FORBIDDEN,
    }


def test_edit_request_carries_exact_version_and_multiple_replacements() -> None:
    request = FileEditRequest(
        operation_id="op-3",
        incident_id="inc-1",
        project_id="payments",
        target_id="dev-a",
        service="payment-api",
        scope=HostScope(),
        path=PurePosixPath("/opt/payments/app.py"),
        expected_sha256="a" * 64,
        replacements=(
            TextReplacement(old_text="old_a", new_text="new_a"),
            TextReplacement(old_text="old_b", new_text="new_b"),
        ),
    )
    assert len(request.replacements) == 2


def test_write_request_limits_payload_to_ten_mib() -> None:
    with pytest.raises(ValidationError):
        FileWriteRequest(
            operation_id="op-4",
            incident_id="inc-1",
            project_id="payments",
            target_id="dev-a",
            service="payment-api",
            scope=HostScope(),
            path=PurePosixPath("/opt/payments/generated.py"),
            content=b"x" * (10_485_760 + 1),
        )
