import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

import pytest
from incidentlens_control_plane.project_registry.store import (
    ProjectAlreadyExists,
    ProjectNotFound,
    ProjectRegistryStore,
    ProjectServiceNotFound,
    RegistryUpdateConflict,
)
from incidentlens_control_plane.project_registry.types import (
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)


def connection_factory(path: Path):
    return lambda: sqlite3.connect(path)


def registration(path: Path, name: str = "Payments") -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name=name,
        local_source_paths=(path.resolve(),),
    )


def test_store_round_trips_project_across_connections(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)

    created = store.create(registration(tmp_path), now=now)
    reopened = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))

    assert reopened.get("payments") == created
    assert reopened.list() == (created,)


def test_store_rejects_duplicate_and_missing_projects(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=now)

    with pytest.raises(ProjectAlreadyExists):
        store.create(registration(tmp_path), now=now)
    with pytest.raises(ProjectNotFound):
        store.get("unknown")


def test_replace_preserves_created_at_and_updates_updated_at(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created_at = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=created_at)

    replaced = store.replace(
        registration(tmp_path, name="Payments API"),
        now=created_at + timedelta(minutes=5),
    )

    assert replaced.created_at == created_at
    assert replaced.updated_at == created_at + timedelta(minutes=5)


def _full_registration(tmp_path: Path) -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name="Payments",
        local_source_paths=(tmp_path,),
        targets=(
            TargetRegistration(
                target_id="dev-a",
                host="dev-a.example.test",
                ssh_user="deploy",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service="payment-api",
                container_names=("payments-api-1",),
                allowed_host_paths=(PurePosixPath("/opt/payments"),),
            ),
        ),
    )


def test_derive_registration_adds_container_and_preserves_identity(
    tmp_path: Path,
) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created = store.create(_full_registration(tmp_path), now=datetime(2026, 8, 10, tzinfo=UTC))

    derived = store.derive_registration_with_updates(
        created, service_name="payment-api", container="payments-api-2"
    )

    svc = derived.services[0]
    assert svc.container_names == ("payments-api-1", "payments-api-2")
    assert svc.allowed_host_paths == (PurePosixPath("/opt/payments"),)
    assert derived.project_id == "payments"
    assert derived.targets == created.targets


def test_derive_registration_adds_host_paths_dedup(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created = store.create(_full_registration(tmp_path), now=datetime(2026, 8, 10, tzinfo=UTC))

    derived = store.derive_registration_with_updates(
        created,
        service_name="payment-api",
        host_paths=(
            PurePosixPath("/var/log/payment"),
            PurePosixPath("/opt/payments"),  # already allowed -> dedup
        ),
    )

    svc = derived.services[0]
    assert PurePosixPath("/var/log/payment") in svc.allowed_host_paths
    assert svc.allowed_host_paths.count(PurePosixPath("/opt/payments")) == 1


def test_derive_registration_raises_conflict_when_already_applied(
    tmp_path: Path,
) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created = store.create(_full_registration(tmp_path), now=datetime(2026, 8, 10, tzinfo=UTC))

    with pytest.raises(RegistryUpdateConflict):
        store.derive_registration_with_updates(
            created, service_name="payment-api", container="payments-api-1"
        )
    with pytest.raises(RegistryUpdateConflict):
        store.derive_registration_with_updates(
            created, service_name="payment-api", host_paths=(PurePosixPath("/opt/payments"),)
        )


def test_derive_registration_rejects_unknown_service_and_bad_values(
    tmp_path: Path,
) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created = store.create(_full_registration(tmp_path), now=datetime(2026, 8, 10, tzinfo=UTC))

    with pytest.raises(ProjectServiceNotFound):
        store.derive_registration_with_updates(
            created, service_name="ghost", container="x-1"
        )
    with pytest.raises(ValueError):
        store.derive_registration_with_updates(
            created, service_name="payment-api", container="bad/name"
        )
    with pytest.raises(ValueError):
        store.derive_registration_with_updates(
            created,
            service_name="payment-api",
            host_paths=(PurePosixPath("relative"),),
        )


def test_derive_registration_feeds_atomic_replace(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created_at = datetime(2026, 8, 10, tzinfo=UTC)
    created = store.create(_full_registration(tmp_path), now=created_at)

    derived = store.derive_registration_with_updates(
        created, service_name="payment-api", container="payments-api-2"
    )
    replaced = store.replace(derived, now=created_at + timedelta(minutes=5))

    assert replaced.created_at == created_at
    assert replaced.updated_at == created_at + timedelta(minutes=5)
    svc = replaced.services[0]
    assert "payments-api-2" in svc.container_names
