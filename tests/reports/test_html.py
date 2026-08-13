from datetime import UTC, datetime

from incidentlens_control_plane.reports.html import HtmlRenderer
from incidentlens_control_plane.reports.types import (
    ReportKind,
    ReportMetadata,
    ReportSection,
)


def _meta() -> ReportMetadata:
    return ReportMetadata(
        symptom="order-service timeouts",
        root_cause="PostgreSQL connection pool exhaustion",
        confidence=0.92,
        services_affected=["order-service", "payment-service"],
        evidence_count=8,
        tool_calls_count=15,
        duration_seconds=420.0,
        generated_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
    )


def test_render_produces_valid_html_shell() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert result.startswith("<!DOCTYPE html>")
    assert "<html" in result
    assert "</html>" in result
    assert "Investigation Report" in result


def test_render_includes_css_variables() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    # Self-contained theme uses custom properties (--il-*)
    assert "--il-primary" in result


def test_render_includes_metadata() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert "order-service timeouts" in result
    assert "PostgreSQL connection pool exhaustion" in result


def test_render_includes_sections() -> None:
    renderer = HtmlRenderer()
    sections = [
        ReportSection(kind=ReportKind.SUMMARY, title="Summary", content="<p>Test</p>"),
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="Evidence data"),
    ]
    result = renderer.render(sections, _meta())
    assert "Summary" in result
    assert "Evidence" in result


def test_render_diff_with_highlighting() -> None:
    renderer = HtmlRenderer()
    diff = "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+new"
    sections = [
        ReportSection(kind=ReportKind.MODIFICATIONS, title="Changes", content=diff)
    ]
    result = renderer.render(sections, _meta())
    assert "diff" in result.lower()
    assert "+new" in result or "&#x2B;new" in result


def test_render_evidence_collapsible() -> None:
    renderer = HtmlRenderer()
    sections = [
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="details")
    ]
    result = renderer.render(sections, _meta())
    # Evidence sections use <details>/<summary> for collapsibility
    assert "<details" in result or "collapsible" in result.lower()


def test_render_print_styles() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert "@media print" in result
