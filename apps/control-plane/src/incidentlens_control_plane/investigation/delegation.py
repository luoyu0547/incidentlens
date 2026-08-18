"""Centralized child-delegation validation shared by both input forms.

Both the structured Provider path (``ChildDelegationRequest``) and the
``delegate_child`` tool path convert their inputs into a frozen
``DelegationSpec`` and run it through ``DelegationValidator.prepare``.  Every
rejection raises ``DelegationRejected`` with stable prose; the tool caller
surfaces it as a ``ToolExecutionError`` and the structured caller drives its
fail/pause handling from the message.

The validator is a pure gate: it never persists, never accounts usage, and
never spawns a child.  Only validation and package construction are centralized
here; persistence, usage accounting and child spawn stay in each caller.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.provider import _scope_within
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentScope,
    DelegatedTaskPackage,
    Investigation,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
)


class DelegationSpec(BaseModel):
    """Normalized child-delegation request accepted by the shared validator.

    ``budget`` is ``None`` when the caller has no explicit budget (the provider-
    structured path always does); the validator then computes a bounded default
    that caps every axis by the parent's budget so a child never widens its
    envelope.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    child_run_id: str
    task_prompt: str
    scope: AgentScope
    evidence_ids: tuple[str, ...] = ()
    budget: AgentBudget | None = None


class DelegationRejected(Exception):
    """A child delegation failed validation.

    ``str(exc)`` is stable consumer-facing prose matching the guard's reasons.
    """


class DelegationValidator:
    """Validate and build a child-delegation package for a parent run."""

    def __init__(
        self,
        projects: ProjectRegistryStore,
        guard: InvestigationGuard | None = None,
    ) -> None:
        self._projects = projects
        self._guard = guard or InvestigationGuard()

    def prepare(
        self,
        parent: AgentRun,
        investigation: Investigation,
        spec: DelegationSpec,
        *,
        now: datetime | None = None,
    ) -> DelegatedTaskPackage:
        """Validate *spec* against *parent*/*investigation* and build the package.

        Raises ``DelegationRejected`` when the delegation is not allowed.  This
        never grants or widens anything: every axis of the child is bounded by
        the parent run's envelope and the project registry.
        """
        allowed, reason = self._guard.can_spawn_child(parent, investigation)
        if not allowed:
            raise DelegationRejected(reason)
        self._validate_registered_scope(parent, spec.scope)
        self._validate_evidence(parent, spec.evidence_ids)
        budget = self._bounded_budget(parent, spec.budget, now=now)
        return DelegatedTaskPackage(
            child_run_id=spec.child_run_id,
            parent_run_id=parent.agent_run_id,
            investigation_id=parent.investigation_id,
            task_prompt=spec.task_prompt,
            scope=spec.scope,
            budget=budget,
            evidence_ids=spec.evidence_ids,
        )

    def _validate_registered_scope(self, parent: AgentRun, child: AgentScope) -> None:
        allowed, reason = _scope_within(child, parent.scope)
        if not allowed:
            raise DelegationRejected(reason)
        if child.scope is not LogScope.CONTAINER:
            # Host-scoped children are bounded by the parent run's host paths via
            # _scope_within; only container children must resolve to a registered
            # service container so they cannot escape into an unregistered one.
            return
        try:
            project = self._projects.get(child.project_id)
        except ProjectNotFound as exc:
            raise DelegationRejected(
                f"child project {child.project_id!r} is not a registered project"
            ) from exc
        service = next(
            (
                svc
                for svc in project.services
                if svc.compose_service == child.service_name
            ),
            None,
        )
        if service is None:
            raise DelegationRejected(
                f"child service {child.service_name!r} is not a registered "
                f"service for project {child.project_id!r}"
            )
        if child.container_name not in service.container_names:
            raise DelegationRejected(
                f"child container {child.container_name!r} is not a registered "
                f"container for service {service.compose_service!r}"
            )
        if not service.allowed_container_paths or any(
            not any(
                child_path.is_relative_to(registered_root)
                for registered_root in service.allowed_container_paths
            )
            for child_path in child.allowed_container_paths
        ):
            raise DelegationRejected(
                "child container paths must be within the registered service's "
                "allowed container paths"
            )

    def _validate_evidence(
        self, parent: AgentRun, evidence_ids: tuple[str, ...]
    ) -> None:
        owned = {ref.evidence_id for ref in parent.evidence}
        missing = set(evidence_ids) - owned
        if missing:
            raise DelegationRejected(
                f"child delegation cites evidence not owned by this run: "
                f"{sorted(missing)}"
            )

    def _bounded_budget(
        self,
        parent: AgentRun,
        budget: AgentBudget | None,
        *,
        now: datetime | None,
    ) -> AgentBudget:
        base = parent.budget
        usage = parent.usage
        elapsed = 0.0
        if parent.started_at is not None:
            current = (now or datetime.now(UTC)).astimezone(UTC)
            elapsed = max(
                0.0,
                (current - parent.started_at.astimezone(UTC)).total_seconds(),
            )
        remaining: dict[str, int] = {
            "max_rounds": base.max_rounds - usage.rounds,
            "max_tool_calls": base.max_tool_calls - usage.tool_calls,
            "max_wall_clock_seconds": int(base.max_wall_clock_seconds - elapsed),
            "max_output_bytes_per_tool": base.max_output_bytes_per_tool,
            "max_total_output_bytes": base.max_total_output_bytes
            - usage.total_output_bytes,
            "max_evidence": base.max_evidence - usage.evidence_count,
            "max_no_new_evidence_rounds": base.max_no_new_evidence_rounds
            - usage.consecutive_no_new_evidence_rounds,
        }
        for field_name, capacity in remaining.items():
            if capacity <= 0:
                raise DelegationRejected(
                    f"child budget {field_name} exhausted"
                )
        if budget is None:
            default = AgentBudget()
            values = {
                name: min(getattr(default, name), remaining[name])
                for name in AgentBudget.model_fields
            }
            return AgentBudget(**values)
        for field_name in AgentBudget.model_fields:
            if getattr(budget, field_name) > remaining[field_name]:
                raise DelegationRejected(
                    f"child {field_name} must not exceed the run budget; "
                    f"remaining run budget ({remaining[field_name]})"
                )
        return budget
