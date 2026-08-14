"""讯飞 MaaS Provider 的纯本地适配测试。"""

from incidentlens_control_plane.investigation.xfyun_provider import (
    _normalise_optional_fields,
)


def test_normalise_optional_fields_only_converts_known_empty_shapes():
    payload = {
        "child_delegation": [],
        "stop_signal": {"stop_reason": "missing_evidence"},
        "tool_requests": [],
    }

    _normalise_optional_fields(payload)

    assert payload["child_delegation"] is None
    assert payload["stop_signal"] == {
        "stop_reason": "missing_evidence",
        "summary": "模型请求停止：missing_evidence",
    }
    assert payload["tool_requests"] == []


def test_normalise_optional_fields_rejects_non_object_result():
    try:
        _normalise_optional_fields([])
    except ValueError as exc:
        assert "must be an object" in str(exc)
    else:
        raise AssertionError("non-object provider result must be rejected")
