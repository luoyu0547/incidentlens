from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.reports.types import (
    ReportBundle,
    ReportKind,
    ReportMetadata,
    ReportSection,
)
from pydantic import ValidationError


def test_report_kind_values() -> None:
    assert {item.value for item in ReportKind} == {
        "summary",
        "root_cause",
        "timeline",
        "evidence",
        "hypotheses",
        "modifications",
        "verification",
        "backups",
        "recommendations",
        "appendix",
    }


def test_report_section_requires_title_and_content() -> None:
    with pytest.raises(ValidationError):
        ReportSection(title="", content="body")


def test_report_section_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ReportSection(
            title="Test",
            content="body",
            extra_field="nope",  # type: ignore[arg-type]
        )


def test_report_metadata_freeze() -> None:
    meta = ReportMetadata(
        symptom="timeout",
        root_cause="db pool exhausted",
        confidence=0.85,
        services_affected=["order-service"],
        evidence_count=5,
        tool_calls_count=12,
        duration_seconds=300.0,
        generated_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        meta.symptom = "changed"  # type: ignore[misc]


def test_report_bundle_paths() -> None:
    meta = ReportMetadata(
        symptom="s",
        root_cause=None,
        confidence=None,
        services_affected=[],
        evidence_count=0,
        tool_calls_count=0,
        duration_seconds=0.0,
        generated_at=datetime.now(UTC),
    )
    bundle = ReportBundle(
        investigation_id="inv-123",
        markdown_path=Path("/tmp/report.md"),
        html_path=Path("/tmp/report.html"),
        metadata=meta,
    )
    assert bundle.investigation_id == "inv-123"
    assert bundle.markdown_path.suffix == ".md"
