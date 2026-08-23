"""Storage tests for the SQLite-backed Project Memory store."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.project_memory.store import (
    ProjectMemoryNotFound,
    ProjectMemoryStore,
)
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryEntry,
    ProjectMemoryKind,
    ProjectMemoryStatus,
)


@pytest.fixture()
def store(tmp_path: Path) -> ProjectMemoryStore:
    created = ProjectMemoryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    created.migrate()
    return created


def entry(
    memory_id: str = "mem-1",
    *,
    project_id: str = "p1",
    service_names: tuple[str, ...] = ("payment-api",),
    fact: str = "payment-api recovered by restarting the canary pod",
    kind: ProjectMemoryKind = ProjectMemoryKind.FACT,
    source_investigation_id: str = "inv-1",
    evidence_ids: tuple[str, ...] = ("ev-1",),
    status: ProjectMemoryStatus = ProjectMemoryStatus.ACTIVE,
    created_at: datetime | None = None,
    last_confirmed_at: datetime | None = None,
) -> ProjectMemoryEntry:
    timestamp = created_at or datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    return ProjectMemoryEntry(
        memory_id=memory_id,
        project_id=project_id,
        service_names=service_names,
        fact=fact,
        kind=kind,
        source_investigation_id=source_investigation_id,
        evidence_ids=evidence_ids,
        status=status,
        created_at=timestamp,
        last_confirmed_at=last_confirmed_at or timestamp,
    )


def seed_memories(
    store: ProjectMemoryStore,
    project_ids: tuple[str, ...] = ("p1", "p2"),
) -> tuple[ProjectMemoryEntry, ...]:
    """Seed several ACTIVE memories per project so the select limit bites."""
    seeded: list[ProjectMemoryEntry] = []
    counter = 0
    for project_id in project_ids:
        count = 6 if project_id == "p1" else 3
        for offset in range(count):
            seeded.append(
                entry(
                    memory_id=f"seed-{project_id}-{offset}",
                    project_id=project_id,
                    service_names=(f"service-{project_id}-{offset}",),
                    fact=f"{project_id} verified fact {offset} about recovery",
                    source_investigation_id=f"inv-{counter}",
                    evidence_ids=(f"ev-{counter}",),
                    created_at=datetime(2026, 8, 1, 0, counter, tzinfo=UTC),
                )
            )
            counter += 1
    for seeded_entry in seeded:
        store.upsert(seeded_entry)
    return tuple(seeded)


def test_active_memory_is_project_scoped_and_bounded(store: ProjectMemoryStore) -> None:
    seed_memories(store, project_ids=("p1", "p2"))
    result = store.list_active("p1", limit=5)
    assert len(result) <= 5
    assert {item.project_id for item in result} == {"p1"}


def test_migrate_is_idempotent(store: ProjectMemoryStore) -> None:
    store.migrate()
    store.migrate()
    seed_memories(store, project_ids=("p1",))
    assert len(store.list_active("p1", limit=5)) == 5


def test_upsert_round_trips_full_provenance(store: ProjectMemoryStore) -> None:
    record = entry(
        memory_id="mem-rt",
        evidence_ids=("ev-7", "ev-8"),
        kind=ProjectMemoryKind.REPAIR,
    )
    store.upsert(record)
    loaded = store.list_active("p1", limit=5)[0]
    assert loaded == record
    assert loaded.evidence_ids == ("ev-7", "ev-8")
    assert loaded.source_investigation_id == "inv-1"
    assert loaded.kind is ProjectMemoryKind.REPAIR


def test_upsert_same_memory_id_is_idempotent(store: ProjectMemoryStore) -> None:
    store.upsert(entry(memory_id="mem-x"))
    store.upsert(entry(memory_id="mem-x", fact="same id rewritten content"))
    active = store.list_active("p1", limit=5)
    assert len(active) == 1
    assert active[0].fact == "same id rewritten content"


def test_list_active_orders_by_last_confirmed_desc(store: ProjectMemoryStore) -> None:
    store.upsert(
        entry(
            memory_id="old",
            last_confirmed_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
    )
    store.upsert(
        entry(
            memory_id="new",
            last_confirmed_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        )
    )
    active = store.list_active("p1", limit=5)
    assert [item.memory_id for item in active] == ["new", "old"]


def test_supersede_moves_but_preserves_the_row(store: ProjectMemoryStore) -> None:
    store.upsert(entry(memory_id="mem-s"))
    superseded = store.supersede("mem-s")
    assert superseded.status is ProjectMemoryStatus.SUPERSEDED
    assert store.list_active("p1", limit=5) == ()
    historical = store.get("mem-s")
    assert historical.status is ProjectMemoryStatus.SUPERSEDED
    assert historical.evidence_ids == ("ev-1",)
    assert historical.source_investigation_id == "inv-1"


def test_supersede_is_idempotent(store: ProjectMemoryStore) -> None:
    store.upsert(entry(memory_id="mem-s"))
    store.supersede("mem-s")
    again = store.supersede("mem-s")
    assert again.status is ProjectMemoryStatus.SUPERSEDED


def test_supersede_missing_raises(store: ProjectMemoryStore) -> None:
    with pytest.raises(ProjectMemoryNotFound):
        store.supersede("mem-missing")


def test_list_active_rejects_out_of_range_limit(store: ProjectMemoryStore) -> None:
    with pytest.raises(ValueError):
        store.list_active("p1", limit=0)
