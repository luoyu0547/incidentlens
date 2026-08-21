"""Fail-closed evaluator for a recorded hard cloud incident."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CloudClosedLoopResult:
    passed: bool
    failures: tuple[str, ...]


def evaluate(trace_path: Path, matrix_path: Path) -> CloudClosedLoopResult:
    failures: list[str] = []
    try:
        events = [json.loads(line) for line in trace_path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        return CloudClosedLoopResult(False, ("trace_unreadable",))
    if not events:
        return CloudClosedLoopResult(False, ("trace_empty",))
    sequences = [event.get("sequence") for event in events if isinstance(event.get("sequence"), int)]
    if sequences != sorted(sequences) or len(sequences) != len(set(sequences)):
        failures.append("trace_sequence_invalid")
    types = [event.get("event_type") for event in events]
    payloads = [event.get("payload", {}) for event in events]
    if types.count("hypothesis.changed") < 2:
        failures.append("two_hypotheses_missing")
    if "child_run.started" not in types:
        failures.append("subagent_missing")
    compact_index = _first(types, "context.compacted")
    if compact_index is None:
        failures.append("compaction_missing")
    elif not any(item in {"tool_call.started", "tool.proposed"} for item in types[compact_index + 1 :]):
        failures.append("fresh_remote_observation_after_compaction_missing")
    proposals = [payload for kind, payload in zip(types, payloads) if kind == "tool.proposed"]
    mutations = [item for item in proposals if item.get("tool_name") in {"file_edit", "file_write", "docker_action"}]
    if len(mutations) < 2:
        failures.append("two_repairs_missing")
    approvals = [payload.get("approval_id") for kind, payload in zip(types, payloads) if kind == "approval.approved"]
    if not approvals:
        failures.append("approval_missing")
    if "changeset.rolled_back" not in types:
        failures.append("rollback_missing")
    rolled = _first(types, "changeset.rolled_back")
    if rolled is not None and not any(kind == "changeset.status_changed" for kind in types[rolled + 1 :]):
        failures.append("reapply_missing")
    try:
        cells = [json.loads(line) for line in matrix_path.read_text().splitlines() if line.strip()]
    except (OSError, json.JSONDecodeError):
        failures.append("matrix_unreadable")
        cells = []
    expected = {("stable", 10), ("stable", 500), ("canary", 10), ("canary", 500)}
    observed = {(cell.get("route"), cell.get("amount")) for cell in cells}
    if observed != expected or any(cell.get("status") != 201 for cell in cells):
        failures.append("final_matrix_failed")
    return CloudClosedLoopResult(not failures, tuple(failures))


def _first(values: list[str], target: str) -> int | None:
    try:
        return values.index(target)
    except ValueError:
        return None
