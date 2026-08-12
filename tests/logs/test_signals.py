from incidentlens_control_plane.logs.parser import parse_log_line
from incidentlens_control_plane.logs.signals import detect_normal_signal


def test_detects_deterministic_normal_signals() -> None:
    assert detect_normal_signal(parse_log_line("heartbeat ok")) == "heartbeat"
    assert detect_normal_signal(parse_log_line("GET /health 200 OK")) == "healthcheck_ok"
    assert detect_normal_signal(parse_log_line("GET /api/payments 200 31ms")) == "request_ok"
    assert detect_normal_signal(parse_log_line("service startup complete")) == "startup"
    assert detect_normal_signal(parse_log_line("shutdown requested")) == "shutdown"
    assert detect_normal_signal(parse_log_line("retrying connection attempt 2")) == "retry"
    assert detect_normal_signal(parse_log_line("unexpected payment failure")) is None
