"""ReportService tests: aggregate investigation + evidence into both formats."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.state_machine import InvestigationStatus
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.investigation.types import (
    Investigation,
    InvestigationBudget,
    UsageCounters,
)
from incidentlens_control_plane.reports.service import ReportService
from incidentlens_control_plane.reports.types import ReportBundle


@pytest.fixture()
def stores(tmp_path: Path):
    def connect():
        return sqlite3.connect(tmp_path / "runtime.db")

    inv_store = InvestigationStore(connect)
    ev_store = EvidenceStore(connect)
    inv_store.migrate()
    ev_store.migrate()
    return inv_store, ev_store


def _create_investigation(store: InvestigationStore) -> str:
    inv = Investigation(
        investigation_id="inv-test001",
        incident_id="inc-test001",
        project_id="proj-1",
        target_id="target-1",
        service="order-service",
        symptom="timeout errors under load",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=UsageCounters(),
        created_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 13, 10, 5, 0, tzinfo=UTC),
    )
    store.create_investigation(inv)
    return inv.investigation_id


def test_generate_creates_both_files(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.investigation_id == inv_id
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.markdown_path.read_text().startswith("# Investigation Report")
    assert "<!DOCTYPE html>" in bundle.html_path.read_text()


def test_generate_investigation_not_found(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    with pytest.raises(KeyError):
        svc.generate("nonexistent")


def test_generate_metadata_matches_investigation(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert bundle.metadata.symptom == "timeout errors under load"
    assert "order-service" in bundle.metadata.services_affected
