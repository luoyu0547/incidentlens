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
            assert "schema_version" in schema["required"]
            assert "event_type" in schema["properties"]
            assert schema["additionalProperties"] is False


def test_cli_stream_schema_covers_control_and_business_frames() -> None:
    schema = exports()[ROOT / "packages/protocol/schema/cli-stream-v1.schema.json"]
    event_type = schema["properties"]["event_type"]
    event_types = set()
    for variant in event_type["anyOf"]:
        if "enum" in variant:
            event_types.update(variant["enum"])
        elif "$ref" in variant:
            event_types.update(schema["$defs"]["RuntimeEventType"]["enum"])
    assert {
        "stream.hello",
        "stream.heartbeat",
        "stream.gap",
        "stream.slow_consumer",
        "project.created",
        "project.updated",
        "project.deleted",
    } <= event_types
    assert "sequence" in schema["properties"]
    assert "payload" in schema["properties"]


def test_log_stream_schema_requires_protocol_version() -> None:
    schema = exports()[ROOT / "packages/protocol/schema/log-stream-v1.schema.json"]
    assert "schema_version" in schema["required"]


def test_log_stream_schema_matches_cursor_stream_frames() -> None:
    schema = exports()[ROOT / "packages/protocol/schema/log-stream-v1.schema.json"]
    assert schema["title"] == "LogStreamEnvelope"
    assert schema["properties"]["schema_version"]["const"] == 1
    assert schema["properties"]["event_type"]["type"] == "string"
    assert schema["properties"]["occurred_at"]["format"] == "date-time"
    assert any(item.get("type") == "string" for item in schema["properties"]["cursor"]["anyOf"])
    assert any(item.get("type") == "object" for item in schema["properties"]["payload"]["anyOf"])


def test_response_ref_siblings_are_checked_for_secrets() -> None:
    from scripts.check_product_contracts import _response_schemas, _walk

    document = {
        "components": {"schemas": {"Safe": {"type": "object"}}},
        "paths": {
            "/api/v1/example": {
                "get": {
                    "responses": {
                        "200": {
                            "$ref": "#/components/schemas/Safe",
                            "content": {
                                "application/json": {
                                    "schema": {"properties": {"secret": {"type": "string"}}}
                                }
                            },
                        }
                    }
                }
            }
        },
    }
    assert "secret" in _walk({"responses": _response_schemas(document)})


def test_log_stream_record_uses_public_allowlist() -> None:
    from types import SimpleNamespace

    from incidentlens_control_plane.streams.logs import _record_payload

    record = SimpleNamespace(
        log_id="log-1",
        cursor="cursor-1",
        observed_at="2026-01-01T00:00:00Z",
        severity="info",
        message_redacted="ok",
        stream_sequence=7,
        authentication_ref="must-not-leak",
    )
    payload = _record_payload(record)
    assert payload == {
        "log_id": "log-1",
        "cursor": "cursor-1",
        "occurred_at": "2026-01-01T00:00:00Z",
        "severity": "info",
        "message": "ok",
        "fields": {},
    }
