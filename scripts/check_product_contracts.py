#!/usr/bin/env python3
"""Validate operation IDs, public schemas, and checked-in protocol artifacts."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.export_product_contracts import exports  # noqa: E402

FORBIDDEN = {
    "authentication_ref", "canonical_intent", "canonical_intent_hash", "request_payload",
    "client_actor", "actor_id", "backup_plaintext", "provider_payload", "api_key",
    "secret", "password", "private_key",
}


def _walk(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"properties", "required", "$defs", "components", "schemas"}:
                if isinstance(child, dict):
                    found.update(str(k) for k in child if str(k).lower() in FORBIDDEN)
                elif isinstance(child, list):
                    found.update(str(k) for k in child if str(k).lower() in FORBIDDEN)
            found.update(_walk(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_walk(child))
    return found


def _response_schemas(document: dict[str, Any]) -> list[Any]:
    """Return response schemas, resolving local refs and preserving siblings."""
    components = document.get("components", {})
    schemas = components.get("schemas", {})
    responses = components.get("responses", {})
    values: list[Any] = []
    seen: set[str] = set()

    def resolve(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        ref = value.get("$ref")
        if not isinstance(ref, str):
            return {key: resolve(child) for key, child in value.items()}
        if ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            target = schemas.get(name)
        elif ref.startswith("#/components/responses/"):
            name = ref.rsplit("/", 1)[-1]
            target = responses.get(name)
        else:
            raise ValueError(f"unsupported response reference: {ref}")
        if not isinstance(target, dict):
            raise ValueError(f"unresolved response reference: {ref}")
        if ref in seen:
            raise ValueError(f"cyclic response reference: {ref}")
        seen.add(ref)
        resolved = resolve(target)
        seen.remove(ref)
        siblings = {key: resolve(child) for key, child in value.items() if key != "$ref"}
        if isinstance(resolved, dict):
            return {**resolved, **siblings}
        return siblings

    for methods in document.get("paths", {}).values():
        for operation in methods.values():
            if not isinstance(operation, dict):
                continue
            for response in operation.get("responses", {}).values():
                if not isinstance(response, dict):
                    raise ValueError("invalid response object")
                resolved_response = resolve(response)
                for media in resolved_response.get("content", {}).values():
                    if isinstance(media, dict) and "schema" in media:
                        values.append(resolve(media["schema"]))
    return values


def main() -> int:
    failures: list[str] = []
    generated = exports()
    for path, value in generated.items():
        expected = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if not path.exists():
            failures.append(f"missing {path.relative_to(ROOT)}")
        elif path.read_text(encoding="utf-8") != expected:
            failures.append(f"drift {path.relative_to(ROOT)}")
        forbidden = (
            _walk({"responses": _response_schemas(value)})
            if path.name == "v1.json"
            else _walk(value)
        )
        if forbidden:
            failures.append(f"schema secrecy violation in {path.name}: {sorted(forbidden)}")

    document = generated[next(path for path in generated if path.name == "v1.json")]
    ids: dict[str, str] = {}
    for route, methods in document.get("paths", {}).items():
        for method, operation in methods.items():
            if method.lower() not in {"get", "post", "put", "patch", "delete", "options", "head"}:
                continue
            operation_id = operation.get("operationId")
            if not operation_id:
                failures.append(f"missing operationId: {method.upper()} {route}")
            elif operation_id in ids:
                failures.append(
                    f"duplicate operationId {operation_id}: "
                    f"{ids[operation_id]} and {method.upper()} {route}"
                )
            else:
                ids[operation_id] = f"{method.upper()} {route}"
            responses = operation.get("responses", {})
            if not responses:
                failures.append(f"missing responses: {method.upper()} {route}")
            if "2XX" not in responses and not any(str(code).startswith("2") for code in responses):
                failures.append(f"missing structured success response: {method.upper()} {route}")
    for path, value in generated.items():
        if path.name.endswith("stream-v1.schema.json"):
            if value.get("properties", {}).get("schema_version", {}).get("const") != 1:
                failures.append(f"schema_version must be 1: {path.name}")
            if "event_type" not in value.get("properties", {}):
                failures.append(f"missing event_type discriminator: {path.name}")

    if failures:
        print("product contract check failed:")
        print("\n".join(f"- {failure}" for failure in failures))
        return 1
    print(f"product contracts OK ({len(generated)} files, {len(ids)} operations)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
