"""Contract export invariants that run without network or application lifespan."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.export_product_contracts import exports

ROOT = Path(__file__).resolve().parents[2]


def test_checked_in_contracts_are_deterministic() -> None:
    for path, value in exports().items():
        expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        assert path.exists(), path
        assert path.read_text(encoding="utf-8") == expected


def test_openapi_is_v1_scoped_and_has_no_network_export_route() -> None:
    document = exports()[ROOT / "packages/protocol/openapi/v1.json"]
    assert document["info"]["version"] == "v1"
    assert all(path.startswith("/api/v1/") or path == "/api/v1" for path in document["paths"])
    assert "/openapi.json" not in document["paths"]


def test_openapi_operation_ids_are_unique_and_success_responses_exist() -> None:
    document = exports()[ROOT / "packages/protocol/openapi/v1.json"]
    operations = [
        operation
        for methods in document["paths"].values()
        for method, operation in methods.items()
        if method in {"get", "post", "put", "patch", "delete", "head", "options"}
    ]
    ids = [operation["operationId"] for operation in operations if "operationId" in operation]
    assert len(ids) == len(set(ids))
    assert all(operation.get("responses") for operation in operations)


def test_stream_schemas_are_versioned_and_discriminated() -> None:
    for path, schema in exports().items():
        if path.name.endswith("stream-v1.schema.json"):
            assert schema["properties"]["schema_version"]["const"] == 1
            assert "event_type" in schema["properties"]
            assert schema["additionalProperties"] is False


def test_log_stream_schema_matches_cursor_stream_frames() -> None:
    schema = exports()[ROOT / "packages/protocol/schema/log-stream-v1.schema.json"]
    assert schema["title"] == "LogStreamEnvelope"
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["event_type"]["type"] == "string"
    assert schema["properties"]["occurred_at"]["format"] == "date-time"
    assert any(item.get("type") == "string" for item in schema["properties"]["cursor"]["anyOf"])
    assert any(item.get("type") == "object" for item in schema["properties"]["payload"]["anyOf"])


def test_request_authentication_ref_is_allowed_but_response_ref_is_not() -> None:
    document = exports()[ROOT / "packages/protocol/openapi/v1.json"]
    target_create = document["components"]["schemas"]["TargetCreate"]
    assert "authentication_ref" in target_create["properties"]
    assert "authentication_ref" not in document["components"]["schemas"]["TargetView"]["properties"]
