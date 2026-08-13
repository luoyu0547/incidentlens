# tests/reports/test_markdown.py
from datetime import UTC, datetime

from incidentlens_control_plane.reports.markdown import MarkdownRenderer
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


def test_render_produces_markdown_with_title() -> None:
    renderer = MarkdownRenderer()
    sections = [
        ReportSection(
            kind=ReportKind.SUMMARY,
            title="Summary",
            content="Order service experienced timeouts.",
        )
    ]
    result = renderer.render(sections, _meta())
    assert "# Investigation Report" in result
    assert "## Summary" in result
    assert "Order service experienced timeouts." in result


def test_render_includes_metadata_header() -> None:
    renderer = MarkdownRenderer()
    result = renderer.render([], _meta())
    assert "order-service timeouts" in result
    assert "92%" in result
    assert "PostgreSQL connection pool exhaustion" in result


def test_render_includes_all_sections() -> None:
    renderer = MarkdownRenderer()
    sections = [
        ReportSection(kind=ReportKind.SUMMARY, title="Summary", content="s"),
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="e"),
        ReportSection(kind=ReportKind.APPENDIX, title="Appendix", content="a"),
    ]
    result = renderer.render(sections, _meta())
    assert "## Summary" in result
    assert "## Evidence" in result
    assert "## Appendix" in result


def test_render_empty_sections() -> None:
    renderer = MarkdownRenderer()
    result = renderer.render([], _meta())
    assert "# Investigation Report" in result
    assert "order-service timeouts" in result


def test_render_preserves_diff_code_blocks() -> None:
    renderer = MarkdownRenderer()
    diff_text = "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+new"
    sections = [
        ReportSection(kind=ReportKind.MODIFICATIONS, title="Changes", content=diff_text)
    ]
    result = renderer.render(sections, _meta())
    assert "```diff" in result
    assert diff_text in result
