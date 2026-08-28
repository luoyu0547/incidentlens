"""Unit tests for the Target product facade service.

These drive :class:`TargetService` directly over a temp SQLite runtime.db so the
authoritative ProjectRegistry and the facade bindings share one database, mirroring
``build_runtime``.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.project_registry.store import (
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from incidentlens_control_plane.targets.service import (
    TargetDeleteBlocked,
    TargetService,
)
from incidentlens_control_plane.targets.store import (
    TargetNotFound,
    TargetStore,
    TargetVersionConflict,
)
from incidentlens_control_plane.targets.types import TargetCreate, TargetPatch
from pydantic import ValidationError

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def service(tmp_path: Path) -> TargetService:
    """Build registry + binding + investigation stores over one runtime.db."""
    database = tmp_path / "runtime.db"

    def connection_factory() -> sqlite3.Connection:
        return sqlite3.connect(database)

    projects = ProjectRegistryStore(connection_factory)
    projects.migrate()
    target_store = TargetStore(connection_factory)
    target_store.migrate()
    investigations = InvestigationStore(connection_factory)
    investigations.migrate()
    return TargetService(
        projects=projects,
        target_store=target_store,
        investigations=investigations,
    )


def _projects(service: TargetService) -> ProjectRegistryStore:
    return service._projects  # noqa: SLF001  (test-only introspection)


def test_create_target_never_leaks_authentication_ref(service: TargetService) -> None:
    created = service.create_target(
        TargetCreate(
            name="Payments",
            host="payments.example.test",
            ssh_user="deploy",
            authentication_ref="ssh-agent:deploy@payments.example.test",
        ),
        now=NOW,
    )
    assert created.target_id.startswith("tgt_")
    assert created.ssh_port == 22
    assert created.authentication_configured is True
    assert created.authentication_hint == "ssh-agent"
    serialized = created.model_dump(mode="json")
    assert "authentication_ref" not in serialized
    assert "ssh-agent:deploy" not in created.model_dump_json()
    assert "deploy@payments" not in created.model_dump_json()

    # The authoritative registry record carries host/user/port and no services.
    record = _projects(service).get(_internal_project(service, created))
    assert record.targets[0].host == "payments.example.test"
    assert record.targets[0].ssh_user == "deploy"
    assert record.services == ()


def _internal_project(service: TargetService, view) -> str:
    return service._target_store.get(view.target_id).project_id  # noqa: SLF001


def test_existing_registry_target_gets_stable_binding(service: TargetService) -> None:
    _projects(service).create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(
                TargetRegistration(
                    target_id="dev-a",
                    host="dev-a.example.test",
                    ssh_user="deploy",
                ),
            ),
        ),
        now=NOW,
    )
    first = service.list_targets(now=NOW)
    assert len(first) == 1
    # Globally unique registry target ID is retained as the facade ID.
    assert first[0].target_id == "dev-a"
    assert first[0].host == "dev-a.example.test"
    assert first[0].ssh_user == "deploy"
    assert first[0].authentication_configured is False
    assert first[0].authentication_hint == ""
    assert first[0].name == "Payments"

    # A later access returns the same persisted binding (stable identity).
    later = service.list_targets(now=NOW + timedelta(hours=1))
    assert [view.target_id for view in later] == ["dev-a"]
    assert later[0].version == 1
    assert later[0].created_at == NOW
    assert service.get_target("dev-a", now=NOW).target_id == "dev-a"


def test_duplicate_internal_target_ids_do_not_alias(service: TargetService) -> None:
    projects = _projects(service)
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(TargetRegistration(target_id="db", host="db-a", ssh_user="deploy"),),
        ),
        now=NOW,
    )
    projects.create(
        ProjectRegistration(
            project_id="analytics",
            display_name="Analytics",
            targets=(TargetRegistration(target_id="db", host="db-b", ssh_user="deploy"),),
        ),
        now=NOW,
    )
    views = service.list_targets(now=NOW)
    assert len(views) == 2
    ids = {view.target_id for view in views}
    assert len(ids) == 2
    # Neither aliases to the raw internal ID, and the two derived IDs differ.
    assert "db" not in ids
    assert all(view.target_id.startswith("tgt_") for view in views)
    by_host = {view.host: view.target_id for view in views}
    assert by_host["db-a"] != by_host["db-b"]

    # Direct access resolves each derived ID to its own host.
    assert service.get_target(by_host["db-a"], now=NOW).host == "db-a"
    assert service.get_target(by_host["db-b"], now=NOW).host == "db-b"
    assert len(service._target_store.list()) == 2  # noqa: SLF001


def test_patch_preserves_services_and_scope(service: TargetService) -> None:
    projects = _projects(service)
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(
                TargetRegistration(
                    target_id="dev-a",
                    host="dev-a.example.test",
                    ssh_user="deploy",
                    port=2222,
                ),
            ),
            services=(
                ServiceRegistration(
                    compose_service="payment-api",
                    container_names=("payments-api-1",),
                    allowed_host_paths=(PurePosixPath("/opt/payments"),),
                    protected_remote_paths=(PurePosixPath("/opt/payments/.env"),),
                ),
            ),
        ),
        now=NOW,
    )
    view = service.get_target("dev-a", now=NOW)
    assert view.ssh_port == 2222

    patched = service.patch_target(
        "dev-a",
        TargetPatch(
            name="Payments API",
            host="dev-b.example.test",
            expected_version=view.version,
        ),
        now=NOW + timedelta(minutes=1),
    )
    assert patched.version == view.version + 1
    assert patched.host == "dev-b.example.test"
    assert patched.ssh_user == "deploy"  # untouched
    assert patched.ssh_port == 2222  # untouched
    assert patched.name == "Payments API"

    record = projects.get("payments")
    svc = record.services[0]
    assert svc.compose_service == "payment-api"
    assert svc.container_names == ("payments-api-1",)
    assert svc.allowed_host_paths == (PurePosixPath("/opt/payments"),)
    assert svc.protected_remote_paths == (PurePosixPath("/opt/payments/.env"),)

    services = service.services_for_target("dev-a", now=NOW)
    assert len(services) == 1
    assert services[0].service == "payment-api"
    assert services[0].container_names == ("payments-api-1",)
    assert services[0].allowed_host_paths == (PurePosixPath("/opt/payments"),)


def test_patch_stale_version_conflicts(service: TargetService) -> None:
    created = service.create_target(
        TargetCreate(name="A", host="a.test", ssh_user="u", authentication_ref="profile:a"),
        now=NOW,
    )
    first = service.patch_target(
        created.target_id,
        TargetPatch(name="B", expected_version=created.version),
        now=NOW + timedelta(minutes=1),
    )
    assert first.version == 2
    with pytest.raises(TargetVersionConflict):
        service.patch_target(
            created.target_id,
            TargetPatch(name="C", expected_version=created.version),
            now=NOW + timedelta(minutes=2),
        )


def test_optional_source_path_maps_to_registry_and_clears_on_patch(
    service: TargetService,
) -> None:
    created = service.create_target(
        TargetCreate(
            name="A",
            host="a.test",
            ssh_user="u",
            authentication_ref="profile:a",
            optional_source_path=Path("/srv/a"),
        ),
        now=NOW,
    )
    assert created.optional_source_path == Path("/srv/a")
    record = _projects(service).get(_internal_project(service, created))
    assert record.local_source_paths == (Path("/srv/a"),)

    cleared = service.patch_target(
        created.target_id,
        TargetPatch(optional_source_path=None, expected_version=created.version),
        now=NOW + timedelta(minutes=1),
    )
    assert cleared.optional_source_path is None
    record = _projects(service).get(_internal_project(service, created))
    assert record.local_source_paths == ()


def test_pinned_policy_requires_pin() -> None:
    with pytest.raises(ValidationError):
        TargetCreate(
            name="A",
            host="a.test",
            ssh_user="u",
            authentication_ref="pinned:abc",
            host_key_policy="pinned",
        )


def test_pinned_policy_accepts_pin(service: TargetService) -> None:
    created = service.create_target(
        TargetCreate(
            name="A",
            host="a.test",
            ssh_user="u",
            authentication_ref="pinned:pin",
            host_key_policy="pinned",
            pinned_host_key_sha256="a" * 64,
        ),
        now=NOW,
    )
    assert created.host_key_policy == "pinned"
    assert created.pinned_host_key_sha256 == "a" * 64


def test_delete_blocked_while_investigation_active(service: TargetService) -> None:
    created = service.create_target(
        TargetCreate(name="X", host="x.test", ssh_user="u", authentication_ref="profile:x"),
        now=NOW,
    )
    store = service._target_store  # noqa: SLF001
    binding = store.get(created.target_id)
    service._investigations.create_investigation(  # noqa: SLF001
        Investigation(
            investigation_id="inv-1",
            incident_id="inc-1",
            project_id=binding.project_id,
            target_id=binding.registry_target_id,
            service="default",
            symptom="down",
            status=InvestigationStatus.RUNNING,
            budget=InvestigationBudget(),
            usage=UsageCounters(),
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(TargetDeleteBlocked):
        service.delete_target(created.target_id, now=NOW)
    # Target still present.
    assert service.get_target(created.target_id, now=NOW).target_id == created.target_id


def test_delete_facade_owned_target_removes_internal_project(
    service: TargetService,
) -> None:
    created = service.create_target(
        TargetCreate(name="X", host="x.test", ssh_user="u", authentication_ref="profile:x"),
        now=NOW,
    )
    project_id = _internal_project(service, created)
    service.delete_target(created.target_id, now=NOW)
    with pytest.raises(TargetNotFound):
        service.get_target(created.target_id, now=NOW)
    with pytest.raises(ProjectNotFound):
        _projects(service).get(project_id)


def test_delete_existing_registry_target_removes_only_that_target(
    service: TargetService,
) -> None:
    projects = _projects(service)
    projects.create(
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            targets=(
                TargetRegistration(target_id="dev-a", host="a.test", ssh_user="u"),
                TargetRegistration(target_id="dev-b", host="b.test", ssh_user="u"),
            ),
        ),
        now=NOW,
    )
    views = service.list_targets(now=NOW)
    assert {view.target_id for view in views} == {"dev-a", "dev-b"}

    service.delete_target("dev-a", now=NOW)
    with pytest.raises(TargetNotFound):
        service.get_target("dev-a", now=NOW)
    # The sibling target survives and the project record stays authoritative.
    assert service.get_target("dev-b", now=NOW).host == "b.test"
    record = projects.get("payments")
    assert [target.target_id for target in record.targets] == ["dev-b"]


def test_auth_hint_truncates_long_underscored_ref(service: TargetService) -> None:
    created = service.create_target(
        TargetCreate(
            name="X",
            host="x.test",
            ssh_user="u",
            authentication_ref="my-named-profile-without-a-colon",
        ),
        now=NOW,
    )
    assert created.authentication_hint == "my-named-profile"
    assert "my-named-profile-without-a-colon" not in created.model_dump_json()
