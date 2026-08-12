from incidentlens_control_plane.logs.correlation import extract_correlation_key
from incidentlens_control_plane.logs.parser import parse_log_line


def test_extracts_trace_then_request_then_span_then_correlation_id() -> None:
    assert (
        extract_correlation_key(
            parse_log_line('{"trace_id":"tr-1","request_id":"req-1"}')
        )
        == "trace:tr-1"
    )
    assert (
        extract_correlation_key(parse_log_line("request_id=req-2 span_id=sp-2"))
        == "request:req-2"
    )
    assert extract_correlation_key(parse_log_line("span=sp-3")) == "span:sp-3"
    assert (
        extract_correlation_key(parse_log_line("correlation_id=corr-4"))
        == "correlation:corr-4"
    )


def test_does_not_generate_fake_service_only_correlation() -> None:
    assert extract_correlation_key(parse_log_line("payment-api container started")) is None
