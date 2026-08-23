import json

from eval.cloud_closed_loop import evaluate


def _trace_path(tmp_path, events):
    path = tmp_path / "trace.jsonl"
    lines = [
        json.dumps(
            {
                "sequence": index,
                "occurred_at": "2026-08-23T00:00:00Z",
                "event_type": kind,
                "payload": payload,
            }
        )
        for index, (kind, payload) in enumerate(events, 1)
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def valid_cloud_trace(tmp_path, *, compaction: bool = False):
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("evidence.appended", {"added": 1, "total": 2}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.created", {"changeset_id": "cs-1", "status": "draft"}),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        (
            "tool.proposed",
            {"tool_name": "docker_action", "tool_call_id": "t-2", "status": "proposed"},
        ),
        ("approval.requested", {"approval_id": "apr-2"}),
        ("approval.approved", {"approval_id": "apr-2"}),
        (
            "tool_call.completed",
            {
                "tool_name": "docker_action",
                "tool_call_id": "t-2",
                "status": "succeeded",
                "approval_id": "apr-2",
            },
        ),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.created", {"changeset_id": "cs-2", "status": "draft"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "verified"}),
        (
            "report.generated",
            {
                "evidence": ["ev-1", "ev-2"],
                "conclusions": [
                    {"summary": "order canary config drift", "evidence_ids": ["ev-1"]},
                    {"summary": "payment high amount throttled", "evidence_ids": ["ev-2"]},
                ],
            },
        ),
        ("investigation.completed", {"status": "completed", "stop_reason": "completed"}),
    ]
    if compaction:
        events.insert(6, ("context.compacted", {"mode": "semantic"}))
    return _trace_path(tmp_path, events)


def valid_matrix(tmp_path):
    path = tmp_path / "matrix.jsonl"
    lines = [
        json.dumps({"route": route, "amount": amount, "status": 201})
        for route, amount in (("stable", 10), ("stable", 500), ("canary", 10), ("canary", 500))
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_cloud_run_may_pass_without_compaction(tmp_path) -> None:
    result = evaluate(valid_cloud_trace(tmp_path, compaction=False), valid_matrix(tmp_path))
    assert "compaction_missing" not in result.failures
    assert result.passed


def test_cloud_run_may_pass_with_compaction(tmp_path) -> None:
    result = evaluate(valid_cloud_trace(tmp_path, compaction=True), valid_matrix(tmp_path))
    assert result.passed


def test_cloud_fails_closed_on_unreadable_trace(tmp_path) -> None:
    result = evaluate(tmp_path / "missing.jsonl", valid_matrix(tmp_path))
    assert not result.passed
    assert "trace_unreadable" in result.failures


def test_cloud_fails_closed_on_empty_trace(tmp_path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text("", encoding="utf-8")
    result = evaluate(trace, valid_matrix(tmp_path))
    assert not result.passed
    assert "trace_empty" in result.failures


def test_cloud_fails_closed_on_partial_matrix(tmp_path) -> None:
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text("", encoding="utf-8")
    result = evaluate(valid_cloud_trace(tmp_path), matrix)
    assert not result.passed
    assert "final_matrix_failed" in result.failures


def test_cloud_fails_on_cell_not_all_status_201(tmp_path) -> None:
    matrix = tmp_path / "matrix.jsonl"
    lines = [
        json.dumps({"route": route, "amount": amount, "status": status})
        for route, amount, status in (
            ("stable", 10, 201),
            ("stable", 500, 429),
            ("canary", 10, 201),
            ("canary", 500, 201),
        )
    ]
    matrix.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = evaluate(valid_cloud_trace(tmp_path), matrix)
    assert not result.passed
    assert "final_matrix_failed" in result.failures


def test_cloud_fails_when_no_evidence_appended(tmp_path) -> None:
    events = [
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "verified"}),
        (
            "report.generated",
            {
                "evidence": ["ev-1", "ev-2"],
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-2"]},
                ],
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "owned_evidence_missing" in result.failures


def test_cloud_fails_on_single_conclusion(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "verified"}),
        (
            "report.generated",
            {"conclusions": [{"summary": "only one root cause", "evidence_ids": ["ev-1"]}]},
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "conclusions_unsupported" in result.failures


def test_cloud_fails_on_foreign_evidence_citation(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "unsupported cause", "evidence_ids": ["ev-foreign"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "conclusions_unsupported" in result.failures


def test_cloud_fails_when_no_conclusions(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "conclusions_unsupported" in result.failures


def test_cloud_fails_on_unapproved_mutation(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": None,
            },
        ),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "verified"}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-1"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "unapproved_mutation" in result.failures


def test_cloud_fails_when_approval_comes_after_mutation(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("approval.approved", {"approval_id": "apr-1"}),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "verified"}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-1"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "approval_before_mutation_missing" in result.failures


def test_cloud_fails_when_verification_missing(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.status_changed", {"changeset_id": "cs-2", "status": "applied"}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-1"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert "verification_missing" not in result.failures


def test_cloud_accepts_live_conclusion_events(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 2, "evidence_ids": ["ev-1", "ev-2"]}),
        ("changeset.created", {"changeset_id": "cs-1", "status": "draft"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        ("changeset.created", {"changeset_id": "cs-2", "status": "draft"}),
        (
            "conclusion.created",
            {
                "conclusion": {
                    "summary": "root cause a",
                    "evidence_ids": ["ev-1"],
                }
            },
        ),
        (
            "conclusion.created",
            {
                "conclusion": {
                    "summary": "root cause b",
                    "evidence_ids": ["ev-2"],
                }
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert result.passed


def test_cloud_fails_when_rollback_missing(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-1"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "rollback_missing" in result.failures


def test_cloud_fails_when_reapply_missing(tmp_path) -> None:
    events = [
        ("evidence.appended", {"added": 1, "total": 1}),
        ("tool.proposed", {"tool_name": "file_edit", "tool_call_id": "t-1", "status": "proposed"}),
        ("approval.requested", {"approval_id": "apr-1"}),
        ("approval.approved", {"approval_id": "apr-1"}),
        (
            "tool_call.completed",
            {
                "tool_name": "file_edit",
                "tool_call_id": "t-1",
                "status": "succeeded",
                "approval_id": "apr-1",
            },
        ),
        ("changeset.status_changed", {"changeset_id": "cs-1", "status": "verified"}),
        ("changeset.rolled_back", {"changeset_id": "cs-1", "status": "rolled_back"}),
        (
            "report.generated",
            {
                "conclusions": [
                    {"summary": "root cause a", "evidence_ids": ["ev-1"]},
                    {"summary": "root cause b", "evidence_ids": ["ev-1"]},
                ]
            },
        ),
    ]
    result = evaluate(_trace_path(tmp_path, events), valid_matrix(tmp_path))
    assert not result.passed
    assert "reapply_missing" in result.failures
