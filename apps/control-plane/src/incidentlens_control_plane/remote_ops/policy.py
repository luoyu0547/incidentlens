"""Policy gate that must run before an adapter can contact a remote target."""

from __future__ import annotations

from collections.abc import Mapping

from incidentlens_control_plane.remote_ops.types import (
    READ_ONLY_OPERATIONS,
    OperationKind,
    PolicyDecision,
    RemoteAction,
    TargetProfile,
)


class RemoteOperationPolicy:
    """Allow investigation by default; require explicit controls for writes.

    This is intentionally independent of LangChain/LangGraph.  The same gate
    protects actions triggered by the dashboard, an API client, or an agent.
    """

    def __init__(self, targets: Mapping[str, TargetProfile]) -> None:
        self._targets = dict(targets)

    def evaluate(self, action: RemoteAction) -> PolicyDecision:
        target = self._targets.get(action.target_id)
        if target is None:
            return PolicyDecision(allowed=False, reason="target is not registered")
        if action.service not in target.allowed_services:
            return PolicyDecision(
                allowed=False,
                reason="service is not allowlisted for this target",
            )

        if action.operation in READ_ONLY_OPERATIONS:
            return PolicyDecision(allowed=True, reason="allowlisted read-only operation")

        if action.operation is OperationKind.PROPOSE_CHANGE:
            return PolicyDecision(
                allowed=True,
                reason="proposal may be generated but cannot modify the target",
                required_gates=("human_review",),
            )

        if action.operation is OperationKind.APPLY_CHANGE:
            if action.change_controls is None:
                return PolicyDecision(
                    allowed=False,
                    reason="a change needs backup, approval, verification, and rollback controls",
                    required_gates=(
                        "backup",
                        "human_approval",
                        "verification_plan",
                        "rollback_plan",
                    ),
                )
            return PolicyDecision(
                allowed=True,
                reason="approved reversible change; adapter must verify after execution",
                required_gates=("post_change_verification",),
            )

        return PolicyDecision(allowed=False, reason="operation is not supported")
