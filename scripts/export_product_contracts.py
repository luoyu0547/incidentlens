#!/usr/bin/env python3
"""Export the checked-in, deterministic product contracts.

The exporter intentionally imports the application only to call ``openapi()``;
it never starts lifespan tasks or exposes an export route.  It also works on a
pre-v1 checkout, where the versioned API is not yet present, so contract work
can be landed independently of the backend feature commits.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "apps" / "control-plane" / "src"
OPENAPI_PATH = ROOT / "packages" / "protocol" / "openapi" / "v1.json"
SCHEMA_DIR = ROOT / "packages" / "protocol" / "schema"


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _fallback(name: str) -> dict[str, Any]:
    common = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"incidentlens://protocol/{name}",
        "title": name,
        "type": "object",
        "additionalProperties": False,
        "required": ["schema_version", "event_type", "sequence"],
        "properties": {
            "schema_version": {"const": 1, "type": "integer"},
            "event_type": {"type": "string", "minLength": 1},
            "sequence": {"type": "integer", "minimum": 0},
            "event_id": {"type": "string"},
            "payload": {"type": "object"},
        },
    }
    if name == "log-stream-v1":
        common["properties"].update({"cursor": {"type": "string"}, "message": {"type": "string"}})
    if name == "workspace-stream-v1":
        common["properties"]["resource_type"] = {"type": "string"}
    return common


def _model_schema(
    module_names: tuple[str, ...],
    class_names: tuple[str, ...],
    fallback: str,
) -> dict[str, Any]:
    sys.path.insert(0, str(SRC))
    for module_name in module_names:
        try:
            module = __import__(module_name, fromlist=list(class_names))
        except (ImportError, ModuleNotFoundError):
            continue
        for class_name in class_names:
            model = getattr(module, class_name, None)
            if model is not None and hasattr(model, "model_json_schema"):
                schema = model.model_json_schema()
                schema.setdefault("$schema", "https://json-schema.org/draft/2020-12/schema")
                schema.setdefault("$id", f"incidentlens://protocol/{fallback}")
                return schema
    return _fallback(fallback)


def openapi() -> dict[str, Any]:
    sys.path.insert(0, str(SRC))
    try:
        from incidentlens_control_plane.main import create_app

        document = create_app().openapi()
    except Exception as exc:  # pragma: no cover - useful on partial checkouts
        document = {
            "openapi": "3.1.0",
            "info": {"title": "IncidentLens", "version": "v1"},
            "paths": {},
        }
        print(
            f"warning: unable to import application ({exc}); "
            "exporting empty v1 surface",
            file=sys.stderr,
        )
    paths = {
        path: value
        for path, value in document.get("paths", {}).items()
        if path.startswith("/api/v1/") or path == "/api/v1"
    }
    schemas = document.get("components", {}).get("schemas", {})
    return {
        "openapi": document.get("openapi", "3.1.0"),
        "info": {"title": "IncidentLens Product API", "version": "v1"},
        "paths": paths,
        "components": {"schemas": schemas},
    }


def exports() -> dict[Path, dict[str, Any]]:
    return {
        OPENAPI_PATH: openapi(),
        SCHEMA_DIR / "cli-stream-v1.schema.json": _model_schema(
            ("incidentlens_control_plane.streams.types", "incidentlens_control_plane.streams.cli"),
            ("StreamEventEnvelope", "CliStreamEvent"), "cli-stream-v1",
        ),
        SCHEMA_DIR / "log-stream-v1.schema.json": _model_schema(
            ("incidentlens_control_plane.logs.views", "incidentlens_control_plane.logs.types"),
            ("LogStreamEnvelope", "LogPage", "LogRecordView"), "log-stream-v1",
        ),
        SCHEMA_DIR / "workspace-stream-v1.schema.json": _model_schema(
            (
                "incidentlens_control_plane.streams.workspace",
                "incidentlens_control_plane.api.routes.workspace_events",
            ),
            (
                "WorkspaceStreamEnvelope",
                "WorkspaceEvent",
                "WorkspaceSseEvent",
            ),
            "workspace-stream-v1",

        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="compare against checked-in files")
    args = parser.parse_args()
    mismatches: list[str] = []
    for path, value in exports().items():
        rendered = _json(value)
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != rendered:
                mismatches.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(rendered, encoding="utf-8")
    if mismatches:
        print("contract drift detected:")
        print("\n".join(f"- {item}" for item in mismatches))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
