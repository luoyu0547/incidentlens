"""Contract tests for the case governance dashboard.

Verifies that index.html and app.js contain all required governance
regions, endpoints, and accessibility features.
"""

from pathlib import Path

import pytest

HTML = Path("apps/control-plane/static/index.html")
JS = Path("apps/control-plane/static/app.js")


def test_dashboard_contains_governance_regions() -> None:
    """Check that all required governance UI regions are present in HTML."""
    html = HTML.read_text()
    for element_id in (
        "case-review-queue",
        "case-editor",
        "case-search-form",
        "case-history",
        "case-feedback",
        "export-investigation-btn",
        "evaluation-comparison",
    ):
        assert f'id="{element_id}"' in html, f"Missing element with id={element_id}"


def test_dashboard_uses_revision_and_real_endpoints() -> None:
    """Check that JS references correct API endpoints and revision handling."""
    source = JS.read_text()
    assert "expected_version" in source, "Missing expected_version for optimistic locking"
    assert "/api/cases/search" in source, "Missing search endpoint"
    assert "/feedback" in source, "Missing feedback endpoint"
    assert "/export" in source, "Missing export endpoint"
    assert "/api/evaluations/comparison" in source, "Missing evaluation comparison endpoint"
    assert "尚无实际运行结果" in source, "Missing empty state text for evaluations"


def test_dashboard_never_labels_content_as_chain_of_thought() -> None:
    """Ensure no hidden reasoning or chain-of-thought labels are exposed."""
    source = (HTML.read_text() + JS.read_text()).lower()
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
