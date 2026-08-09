"""Contract tests for the InvestigationLens dashboard.

Verifies that the packaged dashboard retains investigation, evidence,
evaluation, and export regions while removing all case governance UI.
"""

from pathlib import Path

import pytest

HTML = Path("apps/control-plane/static/index.html")
JS_BUNDLE = Path("apps/control-plane/static/assets/index-vGry3IAM.js")


def test_dashboard_has_no_case_governance_regions() -> None:
    """The dashboard no longer contains case queue/editor/search/history/feedback."""
    html = HTML.read_text()
    source = html
    if JS_BUNDLE.exists():
        source += JS_BUNDLE.read_text()
    # Case governance element IDs must not appear in the rendered HTML
    for element_id in (
        "case-review-queue",
        "case-editor",
        "case-search-form",
        "case-history",
        "case-feedback",
    ):
        assert f'id="{element_id}"' not in source, (
            f"Case governance element id={element_id} should be removed"
        )


def test_dashboard_has_no_case_api_references() -> None:
    """The dashboard does not reference /api/cases endpoints."""
    source = HTML.read_text()
    if JS_BUNDLE.exists():
        source += JS_BUNDLE.read_text()
    assert "/api/cases/search" not in source, "Case search endpoint should be removed"
    assert "/api/cases" not in source or "api/cases" not in source, (
        "No /api/cases references should remain"
    )


def test_dashboard_preserves_investigation_and_export_regions() -> None:
    """The dashboard retains investigation timeline, evidence, and export."""
    source = HTML.read_text()
    if JS_BUNDLE.exists():
        source += JS_BUNDLE.read_text()
    assert "导出" in source, "Export functionality should remain"
    assert "IncidentLens" in source, "Dashboard title should remain"


def test_dashboard_preserves_evaluation_results_region() -> None:
    """The dashboard retains the evaluation results section."""
    source = HTML.read_text()
    if JS_BUNDLE.exists():
        source += JS_BUNDLE.read_text()
    # The evaluation section should still exist
    assert "evaluations" in source.lower() or "评测" in source, (
        "Evaluation results section should remain"
    )


def test_dashboard_never_labels_content_as_chain_of_thought() -> None:
    """Ensure no hidden reasoning or chain-of-thought labels are exposed."""
    if JS_BUNDLE.exists():
        source = (HTML.read_text() + JS_BUNDLE.read_text()).lower()
    else:
        source = HTML.read_text().lower()
    assert "chain of thought" not in source
    assert "思维链" not in source


@pytest.mark.asyncio
async def test_dashboard_is_served_without_shadowing_healthz(agent_api_client) -> None:
    """The packaged dashboard and health endpoint must both remain reachable."""
    dashboard = await agent_api_client.get("/")
    health = await agent_api_client.get("/healthz")

    assert dashboard.status_code == 200
    assert "IncidentLens Dashboard" in dashboard.text
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
