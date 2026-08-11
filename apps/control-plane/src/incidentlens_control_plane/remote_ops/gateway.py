"""Gateway for shell operations with approval integration."""

from __future__ import annotations

from dataclasses import dataclass

from incidentlens_control_plane.approvals.service import (
    ApprovalService,
    ApprovalMismatch,
    ApprovalUnavailable,
)
from incidentlens_control_plane.approvals.store import ApprovalNotFound
from incidentlens_control_plane.remote_ops.types import ShellRequest


@dataclass(frozen=True, slots=True)
class ShellResult:
    """Result of a shell operation."""
    command: str
    approved: bool
    approval_id: str | None = None
    reason: str = ""


class Gateway:
    """Gateway for shell operations with approval integration."""

    def __init__(self, approvals: ApprovalService) -> None:
        self._approvals = approvals

    async def shell(
        self,
        request: ShellRequest,
        approval_id: str | None = None,
    ) -> ShellResult:
        """Execute a shell command with optional approval.

        Automatic commands execute immediately.
        Approval-required commands call approvals.request when no ID is provided
        and return that pending record without transport execution.
        With an ID, call approvals.consume immediately before writing to the PTY.
        Forbidden commands never create approvals.
        """
        from datetime import UTC, datetime

        intent = {
            "kind": "shell",
            "target_id": request.target_id,
            "command": request.command,
            "service": request.service,
        }

        if approval_id is not None:
            # Consume the approval before executing
            try:
                await self._approvals.consume(approval_id, intent)
            except (ApprovalUnavailable, ApprovalNotFound) as exc:
                return ShellResult(
                    command=request.command,
                    approved=False,
                    approval_id=approval_id,
                    reason=f"Approval unavailable: {exc}",
                )
            return ShellResult(
                command=request.command,
                approved=True,
                approval_id=approval_id,
                reason="Approved and consumed",
            )

        # No approval ID provided - request one
        now = datetime.now(UTC)
        record = await self._approvals.request(intent, now=now)
        return ShellResult(
            command=request.command,
            approved=False,
            approval_id=record.approval_id,
            reason="Approval required",
        )
