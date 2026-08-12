from datetime import UTC, datetime

from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.types import LogSeverity


def test_parse_json_severity_and_timestamp() -> None:
    parsed = parse_log_line(
        '{"timestamp":"2026-08-12T10:11:12Z","level":"ERROR","message":"failed"}'
    )

    assert parsed.event_time == datetime(2026, 8, 12, 10, 11, 12, tzinfo=UTC)
    assert parsed.severity is LogSeverity.ERROR
    assert parsed.message == "failed"
    assert parsed.fields["level"] == "ERROR"


def test_invalid_json_falls_back_to_text_severity() -> None:
    parsed = parse_log_line('{"level": "ERROR" broken warn fallback')

    assert parsed.event_time is None
    assert parsed.severity is LogSeverity.WARN
    assert parsed.message == '{"level": "ERROR" broken warn fallback'


def test_unknown_severity_when_no_token_matches() -> None:
    parsed = parse_log_line("service emitted an ordinary line")
    assert parsed.severity is LogSeverity.UNKNOWN


def test_json_without_message_field_marks_message_as_raw() -> None:
    parsed = parse_log_line('{"level":"ERROR","password":"hunter2"}')

    assert parsed.message_is_raw is True
    assert parsed.message == '{"level":"ERROR","password":"hunter2"}'


def test_json_with_message_field_is_not_raw() -> None:
    parsed = parse_log_line('{"level":"ERROR","message":"failed"}')

    assert parsed.message_is_raw is False
    assert parsed.message == "failed"


def test_json_array_is_flagged_as_raw() -> None:
    parsed = parse_log_line('["password", "hunter2"]')

    assert parsed.message_is_raw is True


def test_invalid_json_is_not_flagged_as_raw() -> None:
    parsed = parse_log_line("plain text line")

    assert parsed.message_is_raw is False
    assert parsed.message == "plain text line"
