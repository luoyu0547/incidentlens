import json
from datetime import UTC, datetime

from incidentlens_control_plane.cli.recording import SessionRecorder
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def test_recording_writes_parseable_synchronized_artifacts(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path / "session.cast")
    recorder.record_input(":approve apr-1")
    recorder.record_event(
        RuntimeEvent(
            event_id="evt-1",
            sequence=1,
            event_type=RuntimeEventType.MODEL_ROUND_STARTED,
            occurred_at=datetime(2026, 8, 21, tzinfo=UTC),
            payload={"run_id": "run-1"},
        )
    )
    recorder.close()
    cast = [json.loads(line) for line in (tmp_path / "session.cast").read_text().splitlines()]
    trace = [json.loads(line) for line in (tmp_path / "session.trace.jsonl").read_text().splitlines()]
    assert cast[0]["version"] == 2
    assert [item["event_type"] for item in trace] == [
        "session.input",
        "model_round.started",
    ]
    assert "MODEL" not in (tmp_path / "session.txt").read_text()  # semantic type is preserved


def test_recording_redacts_secrets_ip_paths_and_ansi(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path / "redacted.cast")
    recorder.record_input(
        "\x1b[31m api_key=sk-secret host=10.1.2.3 /Users/alice/.ssh/id_rsa"
    )
    recorder.close()
    combined = (tmp_path / "redacted.trace.jsonl").read_text() + (tmp_path / "redacted.txt").read_text()
    assert "sk-secret" not in combined
    assert "10.1.2.3" not in combined
    assert "/Users/alice" not in combined
    assert "\x1b[" not in combined
    assert "[REDACTED" in combined


def test_interruption_is_structured(tmp_path) -> None:
    recorder = SessionRecorder(tmp_path / "interrupted.cast")
    recorder.interrupted()
    recorder.close()
    assert "session.interrupted" in (tmp_path / "interrupted.trace.jsonl").read_text()
