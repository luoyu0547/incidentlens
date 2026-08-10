from __future__ import annotations

import pytest
from incidentlens_control_plane.remote_ops.policy import RemoteOperationPolicy
from incidentlens_control_plane.remote_ops.types import (
    ChangeControls,
    OperationKind,
    RemoteAction,
    RuntimeKind,
    TargetProfile,
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
