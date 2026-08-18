"""Shared-boundary tests for the centralized ``DelegationValidator``.

Both input forms — the structured ``ChildDelegationRequest`` path and the
``delegate_child`` tool path — convert to the same ``DelegationSpec`` and run
through ``DelegationValidator.prepare``.  The ``source`` parameter proves both
forms take the identical validation boundary: every rejection and every
successful package is produced by the same code regardless of which input form
triggered it.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import PurePosixPath

import pytest
from incidentlens_control_plane.investigation.delegation import (
    DelegationRejected,
    DelegationSpec,
    DelegationValidator,
)
from incidentlens_control_plane.investigation.guard import InvestigationGuard
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
)
from incidentlens_control_plane.investigation.types import (
    AgentBudget,
    AgentRun,
    AgentRunKind,
    AgentScope,
    EvidenceReference,
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.logs.types import LogScope
from incidentlens_control_plane.project_registry.store import ProjectRegistryStore
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)

NOW = datetime(2026, 8, 12, 10, 0, 0, tzinfo=UTC)
PROJECT_ID = "payments"
TARGET_ID = "dev-a"
SERVICE = "payment-api"
CONTAINER = "payments-api-1"
CONTAINER_ROOT = PurePosixPath("/app")
OTHER_PROJECT = "warehouse"

SOURCES = ["structured", "tool"]


@pytest.fixture
def registry(tmp_path) -> ProjectRegistryStore:
    db_path = tmp_path / "registry.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    store = ProjectRegistryStore(connect)
    store.migrate()
    store.create(
        ProjectRegistration(
            project_id=PROJECT_ID,
            display_name="Payments",
            targets=(
                TargetRegistration(
                    target_id=TARGET_ID,
                    host="dev-a.example.test",
                    ssh_user="deploy",
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service=SERVICE,
                    container_names=(CONTAINER, "payments-api-2"),
                    allowed_host_paths=(PurePosixPath("/opt/payments"),),
                    allowed_container_paths=(CONTAINER_ROOT,),
                ),
            ),
        ),
        now=NOW,
    )
    return store


@pytest.fixture
def validator(registry) -> DelegationValidator:
    return DelegationValidator(projects=registry, guard=InvestigationGuard())


def _container_scope(
    *,
    container_name: str = CONTAINER,
    allowed_container_paths: tuple[PurePosixPath, ...] = (CONTAINER_ROOT,),
) -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.CONTAINER,
        service_name=SERVICE,
        container_name=container_name,
        allowed_container_paths=allowed_container_paths,
    )


def delegation_spec(
    source: str,
    *,
    child_run_id: str = "child-1",
    task_prompt: str = "inspect the payments container",
    scope: AgentScope | None = None,
    evidence_ids: tuple[str, ...] = (),
    budget: AgentBudget | None = None,
) -> DelegationSpec:
    """Build a ``DelegationSpec`` the way each input form would.

    The ``source`` distinguishes the structured ``ChildDelegationRequest`` path
    from the ``delegate_child`` tool path, but both convert to the same frozen
    spec so the shared validator sees an identical boundary.  ``scope`` defaults
    to a registered container child within the parent's envelope.
    """
    child_scope = scope or _container_scope()
    if source == "structured":
        return DelegationSpec(
            child_run_id=child_run_id,
            task_prompt=task_prompt,
            scope=child_scope,
            evidence_ids=evidence_ids,
            budget=budget,
        )
    if source == "tool":
        # The tool path rebuilds the scope from JSON via AgentScope(**args["scope"]).
        return DelegationSpec(
            child_run_id=child_run_id,
            task_prompt=task_prompt,
            scope=AgentScope(**child_scope.model_dump()),
            evidence_ids=evidence_ids,
            budget=budget,
        )
    raise AssertionError(f"unknown delegation source: {source!r}")


def _host_scope() -> AgentScope:
    return AgentScope(
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        scope=LogScope.HOST,
        allowed_host_paths=(PurePosixPath("/opt/payments"),),
    )


@pytest.fixture
def parent() -> AgentRun:
    return AgentRun(
        agent_run_id="run-1",
        investigation_id="inv-1",
        parent_run_id=None,
        kind=AgentRunKind.PARENT,
        scope=_host_scope(),
        status=AgentRunStatus.RUNNING,
        budget=AgentBudget(),
        usage=UsageCounters(),
        evidence=(),
        created_at=NOW,
        updated_at=NOW,
    )


@pytest.fixture
def investigation() -> Investigation:
    return Investigation(
        investigation_id="inv-1",
        incident_id="inc-1",
        project_id=PROJECT_ID,
        target_id=TARGET_ID,
        service=SERVICE,
        symptom="checkout requests are failing",
        status=InvestigationStatus.RUNNING,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=NOW,
        updated_at=NOW,
    )


# -- success path ------------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_registered_container_delegation_builds_package(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    spec = delegation_spec(source)
    package = validator.prepare(parent, investigation, spec)
    assert package.child_run_id == "child-1"
    assert package.parent_run_id == "run-1"
    assert package.investigation_id == "inv-1"
    assert package.task_prompt == spec.task_prompt
    assert package.scope == spec.scope
    assert package.evidence_ids == ()
    # A budget is always materialized even when the spec carries none.
    assert package.budget == spec.budget or package.budget is not None


@pytest.mark.parametrize("source", SOURCES)
def test_none_budget_materializes_bounded_default(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    """With no explicit budget a bounded default is computed that never exceeds
    the parent's envelope on any axis."""
    parent = parent.model_copy(
        update={"budget": AgentBudget(max_rounds=2, max_evidence=3)}
    )
    package = validator.prepare(parent, investigation, delegation_spec(source, budget=None))
    assert package.budget.max_rounds == 2
    assert package.budget.max_evidence == 3
    for name in AgentBudget.model_fields:
        assert getattr(package.budget, name) <= getattr(parent.budget, name)


# -- registry-aware container validation ------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_unregistered_container_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    spec = delegation_spec(source, scope=_container_scope(container_name="not-registered"))
    with pytest.raises(DelegationRejected, match="registered container"):
        validator.prepare(parent, investigation, spec)


@pytest.mark.parametrize("source", SOURCES)
def test_unregistered_service_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    scope = _container_scope(container_name=CONTAINER).model_copy(
        update={"service_name": "unknown-service"}
    )
    spec = delegation_spec(source, scope=scope)
    with pytest.raises(DelegationRejected, match="registered service"):
        validator.prepare(parent, investigation, spec)


@pytest.mark.parametrize("source", SOURCES)
def test_container_path_outside_registry_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    spec = delegation_spec(
        source, scope=_container_scope(allowed_container_paths=(PurePosixPath("/etc"),))
    )
    with pytest.raises(DelegationRejected, match="container paths"):
        validator.prepare(parent, investigation, spec)


# -- scope narrowing ---------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_project_target_mismatch_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    scope = _container_scope().model_copy(update={"project_id": OTHER_PROJECT})
    spec = delegation_spec(source, scope=scope)
    with pytest.raises(DelegationRejected, match="child scope"):
        validator.prepare(parent, investigation, spec)


# -- guard boundary ----------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_child_parent_cannot_delegate_grandchild(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    parent = parent.model_copy(
        update={"kind": AgentRunKind.CHILD, "parent_run_id": "run-0"}
    )
    with pytest.raises(DelegationRejected, match="child run must not delegate grandchildren"):
        validator.prepare(parent, investigation, delegation_spec(source))


@pytest.mark.parametrize("source", SOURCES)
def test_exhausted_child_budget_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    investigation = investigation.model_copy(
        update={"usage": UsageCounters(children=investigation.budget.max_children)}
    )
    with pytest.raises(DelegationRejected, match="child budget exhausted"):
        validator.prepare(parent, investigation, delegation_spec(source))


# -- evidence ownership ------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
def test_foreign_evidence_is_rejected(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    spec = delegation_spec(source, evidence_ids=("ev-fabricated",))
    with pytest.raises(DelegationRejected, match="not owned by this run"):
        validator.prepare(parent, investigation, spec)


@pytest.mark.parametrize("source", SOURCES)
def test_owned_evidence_is_accepted(
    source: str, validator: DelegationValidator, parent: AgentRun, investigation: Investigation
) -> None:
    parent = parent.model_copy(
        update={
            "evidence": (
                EvidenceReference(
                    evidence_id="ev-seed",
                    operation_id="seed",
                    summary="seeded evidence",
                ),
            )
        }
    )
    package = validator.prepare(
        parent, investigation, delegation_spec(source, evidence_ids=("ev-seed",))
    )
    assert package.evidence_ids == ("ev-seed",)


# -- budget envelope ---------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("axis", list(AgentBudget.model_fields))
def test_explicit_budget_axis_never_exceeds_parent_envelope(
    source: str,
    axis: str,
    validator: DelegationValidator,
    parent: AgentRun,
    investigation: Investigation,
) -> None:
    """Every explicit child budget axis larger than the parent's is rejected."""
    base = getattr(parent.budget, axis)
    parent = parent.model_copy(
        update={"budget": AgentBudget(**{axis: base})}
    )
    over = base + 1
    budget = AgentBudget(**{axis: over})
    with pytest.raises(DelegationRejected, match=f"child {axis} must not exceed the run budget"):
        validator.prepare(parent, investigation, delegation_spec(source, budget=budget))
