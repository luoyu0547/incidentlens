"""Regression tests for the repeatable cloud acceptance request matrix."""

from __future__ import annotations

import runpy
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "infra" / "acceptance" / "scripts" / "request_matrix.py"


def test_each_matrix_run_has_distinct_request_ids() -> None:
    namespace = runpy.run_path(str(SCRIPT))
    build_request_id = namespace["build_request_id"]

    first = build_request_id("stable", 10, 1, "run-a")
    second = build_request_id("stable", 10, 1, "run-b")

    assert first == "matrix-run-a-stable-10-1"
    assert second == "matrix-run-b-stable-10-1"
    assert first != second
