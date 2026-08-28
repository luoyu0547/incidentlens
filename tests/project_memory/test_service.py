"""Validation, deduplication and advisory-selection tests for Project Memory."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.project_memory.service import ProjectMemoryService
from incidentlens_control_plane.project_memory.store import ProjectMemoryStore
from incidentlens_control_plane.project_memory.types import (
    ProjectMemoryEntry,
    ProjectMemoryKind,
    ProjectMemoryRejected,
    ProjectMemoryStatus,
)


@pytest.fixture()
def store(tmp_path: Path) -> ProjectMemoryStore:
    created = ProjectMemoryStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    created.migrate()
    return created


@pytest.fixture()
def service(store: ProjectMemoryStore) -> ProjectMemoryService:
    return ProjectMemoryService(store)


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


def investigation(project_id: str = "p1") -> Investigation:
    moment = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)
    return Investigation(
        investigation_id="inv-1",
        incident_id="inc-1",
        project_id=project_id,
        target_id="dev-a",
        service="payment-api",
        symptom="payment-api canary errors",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=moment,
        updated_at=moment,
    )


def _seed_project_memories(store: ProjectMemoryStore) -> None:
    store.upsert(
        entry(
            memory_id="m-pay",
            service_names=("payment-api",),
            fact="payment-api crashed when the canary pod OOM-killed",
            source_investigation_id="inv-1",
            evidence_ids=("ev-1",),
            last_confirmed_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
    )
    store.upsert(
        entry(
            memory_id="m-auth",
            service_names=("auth-api",),
            fact="auth-api recovered after restarting the auth daemon",
            source_investigation_id="inv-2",
            evidence_ids=("ev-2",),
            last_confirmed_at=datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
        )
    )
    store.upsert(
        entry(
            memory_id="m-canary",
            service_names=("payment-api",),
            fact="payments canary error rate spiked before the disk filled",
            source_investigation_id="inv-3",
            evidence_ids=("ev-3",),
            last_confirmed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        )
    )


# -- admission: each safety rule has a passing and a rejecting case ----------


def test_memory_requires_owned_evidence(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="foreign evidence"):
        service.accept_extracted(
            [entry(evidence_ids=("ev-foreign",))],
            investigation(),
            owned_evidence_ids={"ev-owned"},
        )


def test_valid_verified_fact_is_accepted(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    accepted = service.accept_extracted(
        [entry(evidence_ids=("ev-owned",))],
        investigation(),
        owned_evidence_ids={"ev-owned"},
    )
    active = store.list_active("p1", limit=5)
    assert len(accepted) == 1
    assert [item.memory_id for item in active] == [accepted[0].memory_id]


def test_accept_extracted_is_atomic_on_rejection(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    with pytest.raises(ProjectMemoryRejected, match="foreign evidence"):
        service.accept_extracted(
            [
                entry(memory_id="mem-good", evidence_ids=("ev-owned",)),
                entry(memory_id="mem-bad", evidence_ids=("ev-foreign",)),
            ],
            investigation(),
            owned_evidence_ids={"ev-owned"},
        )
    assert store.list_active("p1", limit=5) == ()


def test_empty_source_provenance_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="empty provenance"):
        service.accept_extracted(
            [entry(source_investigation_id="")],
            investigation(),
            owned_evidence_ids={"ev-1"},
        )


def test_missing_evidence_backing_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="empty provenance"):
        service.accept_extracted(
            [entry(evidence_ids=())],
            investigation(),
            owned_evidence_ids=set(),
        )


def test_unverified_hypothesis_kind_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="unverified hypothesis kind"):
        service.accept_extracted(
            [entry(kind=ProjectMemoryKind.UNVERIFIED_HYPOTHESIS)],
            investigation(),
            owned_evidence_ids={"ev-1"},
        )


def test_repair_kind_is_accepted(service: ProjectMemoryService, store: ProjectMemoryStore) -> None:
    service.accept_extracted(
        [entry(kind=ProjectMemoryKind.REPAIR, evidence_ids=("ev-1",))],
        investigation(),
        owned_evidence_ids={"ev-1"},
    )
    assert len(store.list_active("p1", limit=5)) == 1


def test_secret_like_fact_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="secret-like value"):
        service.accept_extracted(
            [entry(fact="the api key = sk-abcd1234efgh5678 for the service")],
            investigation(),
            owned_evidence_ids={"ev-1"},
        )


def test_plain_fact_is_accepted(service: ProjectMemoryService, store: ProjectMemoryStore) -> None:
    service.accept_extracted([entry()], investigation(), owned_evidence_ids={"ev-1"})
    assert len(store.list_active("p1", limit=5)) == 1


def test_oversized_fact_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="oversized fact"):
        service.accept_extracted(
            [entry(fact="x" * 2200)],
            investigation(),
            owned_evidence_ids={"ev-1"},
        )


def test_bounded_fact_is_accepted(service: ProjectMemoryService, store: ProjectMemoryStore) -> None:
    service.accept_extracted(
        [entry(fact="useful fact " + "y" * 500)],
        investigation(),
        owned_evidence_ids={"ev-1"},
    )
    assert len(store.list_active("p1", limit=5)) == 1


def test_memory_from_unrelated_project_is_rejected(service: ProjectMemoryService) -> None:
    with pytest.raises(ProjectMemoryRejected, match="unrelated project identity"):
        service.accept_extracted(
            [entry(project_id="p-other")],
            investigation(project_id="p1"),
            owned_evidence_ids={"ev-1"},
        )


def test_matching_project_is_accepted(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    service.accept_extracted(
        [entry(project_id="p1")],
        investigation(project_id="p1"),
        owned_evidence_ids={"ev-1"},
    )
    assert len(store.list_active("p1", limit=5)) == 1


# -- deduplication / supression ----------------------------------------------


def test_active_duplicate_is_superseded_not_duplicated(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    first = entry(memory_id="mem-first", evidence_ids=("ev-1",))
    service.accept_extracted([first], investigation(), owned_evidence_ids={"ev-1"})
    second = entry(memory_id="mem-second", evidence_ids=("ev-2",))
    service.accept_extracted([second], investigation(), owned_evidence_ids={"ev-2"})

    active = store.list_active("p1", limit=5)
    assert [item.memory_id for item in active] == ["mem-second"]
    historical = store.get("mem-first")
    assert historical.status is ProjectMemoryStatus.SUPERSEDED


def test_genuinely_new_fact_is_added_not_superseded(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    service.accept_extracted(
        [entry(memory_id="mem-1", fact="failure mode alpha", evidence_ids=("ev-1",))],
        investigation(),
        owned_evidence_ids={"ev-1"},
    )
    service.accept_extracted(
        [entry(memory_id="mem-2", fact="repair for alpha", evidence_ids=("ev-1",))],
        investigation(),
        owned_evidence_ids={"ev-1"},
    )
    active = store.list_active("p1", limit=5)
    assert {item.memory_id for item in active} == {"mem-1", "mem-2"}


# -- normalization ------------------------------------------------------------


def test_service_names_are_normalized(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    service.accept_extracted(
        [entry(memory_id="mem-n", service_names=(" Auth-Payment ", "auth-payment", "  "))],
        investigation(),
        owned_evidence_ids={"ev-1"},
    )
    active = store.list_active("p1", limit=5)[0]
    assert active.service_names == ("auth-payment",)


# -- selection and rendering --------------------------------------------------


def test_select_prefers_exact_service_overlap(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    _seed_project_memories(store)
    selected = service.select_relevant("p1", symptom="pod crash", services=("payment-api",))
    assert selected[0].memory_id == "m-pay"
    assert selected[1].memory_id == "m-canary"
    assert selected[-1].memory_id == "m-auth"


def test_select_prefers_symptom_term_overlap_within_same_service(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    _seed_project_memories(store)
    selected = service.select_relevant(
        "p1", symptom="canary error rate", services=("payment-api",)
    )
    assert selected[0].memory_id == "m-canary"


def test_select_uses_recency_to_break_ties(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    store.upsert(
        entry(
            memory_id="m-old",
            service_names=("s1",),
            fact="s1 requires one restart step",
            last_confirmed_at=datetime(2026, 8, 1, 10, 0, tzinfo=UTC),
        )
    )
    store.upsert(
        entry(
            memory_id="m-new",
            service_names=("s1",),
            fact="s1 requires one restart step",
            last_confirmed_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
        )
    )
    selected = service.select_relevant("p1", symptom="restart", services=("s1",))
    assert selected[0].memory_id == "m-new"


def test_render_is_bounded_at_five(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    for index in range(7):
        store.upsert(
            entry(
                memory_id=f"m{index}",
                service_names=(f"s{index}",),
                fact=f"recovery fact number {index}",
                last_confirmed_at=datetime(2026, 8, 1, 0, index, tzinfo=UTC),
            )
        )
    selected = service.select_relevant("p1", symptom="recovery", services=("s0",), limit=5)
    assert len(selected) == 5
    rendered = service.render_relevant("p1", symptom="recovery", services=("s0",), limit=5)
    assert rendered.startswith("Project memory (advisory; revalidate current environment.)")


def test_render_includes_advisory_and_provenance(
    service: ProjectMemoryService, store: ProjectMemoryStore
) -> None:
    store.upsert(
        entry(
            memory_id="m-pay",
            evidence_ids=("ev-9",),
            source_investigation_id="inv-9",
        )
    )
    rendered = service.render_relevant("p1", symptom="recovery", services=("payment-api",))
    assert "Project memory (advisory; revalidate current environment.)" in rendered
    assert "source investigation" in rendered
    assert "inv-9" in rendered
    assert "ev-9" in rendered


def test_render_empty_when_no_active_memory(service: ProjectMemoryService) -> None:
    assert (
        service.render_relevant("p1", symptom="anything", services=("s1",)) == ""
    )
