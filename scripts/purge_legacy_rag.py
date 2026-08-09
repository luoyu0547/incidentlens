#!/usr/bin/env python3
"""Purge legacy RAG tables from an IncidentLens database.

This script drops only the tables listed in LEGACY_RAG_TABLES.  By default
it operates in dry-run mode and reports what would be deleted without making
any changes.  Pass ``--confirm-drop-legacy-rag`` to execute the drops.

Usage::

    python scripts/purge_legacy_rag.py DATABASE
    python scripts/purge_legacy_rag.py DATABASE --confirm-drop-legacy-rag
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Immutable allowlist -- only these tables may be dropped.  We never
# interpolate user-supplied names into SQL; only these constants are used.
LEGACY_RAG_TABLES: tuple[str, ...] = (
    "case_feedback",
    "case_usage_events",
    "case_review_actions",
    "case_embeddings",
    "case_fts",
    "case_memory",
    "incidentlens_schema_versions",
)


def _discover_existing(db_path: Path, allowlist: tuple[str, ...]) -> list[str]:
    """Return the subset of *allowlist* tables that actually exist in *db_path*."""
    conn = sqlite3.connect(str(db_path))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        conn.close()
    existing_names = {r[0] for r in rows}
    return [t for t in allowlist if t in existing_names]


def _drop_tables(db_path: Path, tables: list[str]) -> None:
    """Drop the listed tables in a single transaction."""
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            for table in tables:
                conn.execute(f"DROP TABLE {table}")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge legacy RAG tables from an IncidentLens SQLite database.",
    )
    parser.add_argument(
        "database",
        type=Path,
        help="Path to the SQLite database file.",
    )
    parser.add_argument(
        "--confirm-drop-legacy-rag",
        action="store_true",
        default=False,
        help="Actually execute the drops (default is dry-run).",
    )
    args = parser.parse_args(argv)

    db_path: Path = args.database

    # --- Validate the database path ---
    if not db_path.exists():
        print(f"Error: {db_path} does not exist.", file=sys.stderr)
        return 1
    if not db_path.is_file():
        print(f"Error: {db_path} is not a regular file.", file=sys.stderr)
        return 1

    # Quick SQLite header check
    try:
        with open(db_path, "rb") as fh:
            header = fh.read(16)
        if not header.startswith(b"SQLite format 3"):
            print(f"Error: {db_path} is not a valid SQLite database.", file=sys.stderr)
            return 1
    except OSError as exc:
        print(f"Error reading {db_path}: {exc}", file=sys.stderr)
        return 1

    # --- Discover allowlisted tables that exist ---
    tables_to_drop = _discover_existing(db_path, LEGACY_RAG_TABLES)

    if not tables_to_drop:
        print("No legacy RAG tables found. Nothing to do.")
        return 0

    print("Legacy RAG tables discovered:")
    for t in tables_to_drop:
        print(f"  - {t}")

    # --- Dry-run or confirmed drop ---
    if not args.confirm_drop_legacy_rag:
        print(
            "\nDry run: no tables were dropped. "
            "Re-run with --confirm-drop-legacy-rag to execute."
        )
        return 0

    print("\nDropping legacy RAG tables ...")
    _drop_tables(db_path, tables_to_drop)
    print("Done. Dropped:")
    for t in tables_to_drop:
        print(f"  - {t}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
