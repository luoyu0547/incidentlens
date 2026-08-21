import json

from eval.cloud_closed_loop import evaluate


def test_evaluator_accepts_complete_closed_loop(tmp_path) -> None:
    kinds = [
        "model_round.started",
        "hypothesis.changed",
        "hypothesis.changed",
        "child_run.started",
        "context.compacted",
        "tool.proposed",
        "tool.proposed",
        "approval.approved",
        "tool.proposed",
        "changeset.rolled_back",
        "changeset.status_changed",
    ]
    trace = tmp_path / "trace.jsonl"
    trace.write_text(
        "\n".join(
            json.dumps(
            {
                "sequence": index,
                "event_type": kind,
                "payload": {"tool_name": "file_edit"}
                if kind == "tool.proposed"
                else {},
            }
        )
            for index, kind in enumerate(kinds, 1)
        )
        + "\n"
    )
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text(
        "\n".join(
            json.dumps({"route": route, "amount": amount, "status": 201})
            for route, amount in (("stable", 10), ("stable", 500), ("canary", 10), ("canary", 500))
        )
        + "\n"
    )
    assert evaluate(trace, matrix).passed


def test_evaluator_fails_closed_on_partial_artifacts(tmp_path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_text(json.dumps({"sequence": 1, "event_type": "model_round.started", "payload": {}}))
    matrix = tmp_path / "matrix.jsonl"
    matrix.write_text("")
    result = evaluate(trace, matrix)
    assert not result.passed
    assert "final_matrix_failed" in result.failures
    assert "two_repairs_missing" in result.failures
