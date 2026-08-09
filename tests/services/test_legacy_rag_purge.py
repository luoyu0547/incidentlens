"""Tests for the legacy RAG purge script.

Verifies:
  - Dry-run mode leaves all tables untouched
  - Confirmed mode drops only allowlisted tables
  - Non-RAG tables survive both modes
  - Invalid database paths are rejected
  - Non-SQLite files are rejected
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = str(Path(__file__).resolve().parents[2] / "scripts" / "purge_legacy_rag.py")


@pytest.fixture()
def legacy_db(tmp_path: Path) -> Path:
    """Create a SQLite database with allowlisted RAG tables and a non-RAG table."""
    db_path = tmp_path / "test_legacy.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    # Allowlisted RAG tables
    cur.execute("CREATE TABLE case_feedback (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE case_usage_events (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE case_review_actions (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE case_embeddings (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE incidentlens_schema_versions (id INTEGER PRIMARY KEY)")

    # case_fts is a virtual table; we create it as a regular table for simplicity
    cur.execute("CREATE TABLE case_fts (id INTEGER PRIMARY KEY)")
    cur.execute("CREATE TABLE case_memory (id INTEGER PRIMARY KEY)")

    # Non-RAG table that must survive
    cur.execute(
        "CREATE TABLE investigation_audits (id INTEGER PRIMARY KEY, incident_id TEXT)"
    )
    cur.execute(
        "INSERT INTO investigation_audits (incident_id) VALUES ('inc-1')"
    )
    cur.execute("INSERT INTO case_memory (id) VALUES (1)")

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture()
def non_rag_db(tmp_path: Path) -> Path:
    """Create a database with no RAG tables."""
    db_path = tmp_path / "clean.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        "CREATE TABLE investigation_audits (id INTEGER PRIMARY KEY, incident_id TEXT)"
    )
    cur.execute("INSERT INTO investigation_audits (incident_id) VALUES ('inc-1')")
    conn.commit()
    conn.close()
    return db_path


def _run_purge(db_path: Path, confirm: bool = False) -> subprocess.CompletedProcess:
    """Run the purge script as a subprocess."""
    args = [sys.executable, SCRIPT, str(db_path)]
    if confirm:
        args.append("--confirm-drop-legacy-rag")
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


def _table_names(db_path: Path) -> set[str]:
    """Return the set of user-created table names in the database."""
    conn = sqlite3.connect(str(db_path))
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    conn.close()
    return {r[0] for r in rows}


def test_dry_run_preserves_all_tables(legacy_db: Path) -> None:
    """Without --confirm-drop-legacy-rag, no tables should be dropped."""
    before = _table_names(legacy_db)
    result = _run_purge(legacy_db, confirm=False)
    assert result.returncode == 0, result.stderr
    after = _table_names(legacy_db)
    assert before == after


def test_dry_run_output_mentions_tables(legacy_db: Path) -> None:
    """Dry-run output should list the tables that would be dropped."""
    result = _run_purge(legacy_db, confirm=False)
    assert "case_memory" in result.stdout.lower() or "case_memory" in result.stderr.lower()


def test_confirmed_purge_drops_only_allowlisted(legacy_db: Path) -> None:
    """With confirmation, only allowlisted tables should be dropped."""
    result = _run_purge(legacy_db, confirm=True)
    assert result.returncode == 0, result.stderr
    after = _table_names(legacy_db)
    # investigation_audits must survive
    assert "investigation_audits" in after
    # All allowlisted RAG tables must be gone
    for table in ("case_feedback", "case_usage_events", "case_review_actions",
                  "case_embeddings", "case_fts", "case_memory",
                  "incidentlens_schema_versions"):
        assert table not in after, f"{table} should have been dropped"


def test_confirmed_purge_preserves_non_rag_data(non_rag_db: Path) -> None:
    """Confirmed purge on a database with no RAG tables should be a no-op."""
    before = _table_names(non_rag_db)
    result = _run_purge(non_rag_db, confirm=True)
    assert result.returncode == 0, result.stderr
    after = _table_names(non_rag_db)
    assert before == after


def test_nonexistent_database_exits_nonzero(tmp_path: Path) -> None:
    """Purging a nonexistent path should fail."""
    result = _run_purge(tmp_path / "nope.db", confirm=True)
    assert result.returncode != 0


def test_non_sqlite_file_exits_nonzero(tmp_path: Path) -> None:
    """Purging a file that is not a SQLite database should fail."""
    bad = tmp_path / "not.db"
    bad.write_text("not a database")
    result = _run_purge(bad, confirm=True)
    assert result.returncode != 0
