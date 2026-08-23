#!/usr/bin/env python3
"""Project persisted conclusions into a publishable cloud-acceptance trace.

The live recorder captures Runtime events while the TUI is open. Conclusions
are also durable SQLite records; older runs made before ``conclusion.created``
was introduced therefore need one explicit, post-run report projection. This
script never invents conclusions: every citation must belong to the same run
in ``evidence_refs`` or export fails closed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path


def export(runtime_db: Path, source_trace: Path, output_trace: Path) -> None:
    records = [
        json.loads(line)
        for line in source_trace.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not records:
        raise ValueError("source trace is empty")

    with sqlite3.connect(runtime_db) as connection:
        rows = connection.execute(
            """
            SELECT c.agent_run_id, c.investigation_id, c.record_json
            FROM conclusions AS c
            JOIN agent_runs AS r ON r.agent_run_id = c.agent_run_id
            WHERE json_extract(r.record_json, '$.kind') = 'parent'
            ORDER BY c.created_at
            """
        ).fetchall()
        if len(rows) < 2:
            raise ValueError("at least two persisted parent conclusions are required")

        conclusions: list[dict[str, object]] = []
        owned_ids: set[str] = set()
        investigation_ids: set[str] = set()
        run_ids: set[str] = set()
        for run_id, investigation_id, raw in rows:
            conclusion = json.loads(raw)
            citations = conclusion.get("evidence_ids")
            if not isinstance(citations, list) or not citations:
                raise ValueError("persisted conclusion has no evidence citations")
            placeholders = ",".join("?" for _ in citations)
            found = {
                row[0]
                for row in connection.execute(
                    f"""
                    SELECT evidence_ref_id FROM evidence_refs
                    WHERE agent_run_id = ? AND evidence_ref_id IN ({placeholders})
                    """,  # noqa: S608 - placeholders are generated, values stay bound
                    (run_id, *citations),
                )
            }
            if found != set(citations):
                raise ValueError("conclusion cites evidence not owned by its parent run")
            conclusions.append(conclusion)
            owned_ids.update(found)
            investigation_ids.add(investigation_id)
            run_ids.add(run_id)

    if len(investigation_ids) != 1 or len(run_ids) != 1:
        raise ValueError("acceptance export must contain one parent investigation run")

    records.append(
        {
            "sequence": "post-run-report-1",
            "occurred_at": datetime.now(UTC).isoformat(),
            "event_type": "report.generated",
            "payload": {
                "source": "persisted_runtime_projection",
                "investigation_id": next(iter(investigation_ids)),
                "run_id": next(iter(run_ids)),
                "evidence": sorted(owned_ids),
                "conclusions": conclusions,
            },
        }
    )
    output_trace.parent.mkdir(parents=True, exist_ok=True)
    output_trace.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-db", type=Path, required=True)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    export(args.runtime_db, args.trace, args.output)


if __name__ == "__main__":
    main()
