"""Tests for test topology — verifying integration markers are declared.

Ensures that integration test modules declare pytestmark = pytest.mark.integration
so that `pytest -m "not integration"` correctly excludes them.
"""

from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "path",
    [
        Path("tests/integration/test_scenario_acceptance.py"),
        Path("tests/integration/test_compose_flow.py"),
        Path("tests/integration/test_memory_compaction_flow.py"),
    ],
)
def test_integration_modules_declare_marker(path: Path) -> None:
    source = path.read_text()
    assert "pytestmark = pytest.mark.integration" in source


def test_governance_flow_module_removed() -> None:
    """Legacy memory governance flow test must not exist."""
    legacy = Path("tests/integration/test_memory_governance_flow.py")
    assert not legacy.exists(), (
        f"{legacy} must be deleted as part of legacy RAG retirement"
    )
