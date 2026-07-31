"""Non-destructive schema migration for case memory.

Preserves old cases and uses explicit schema versioning.
Deleting old tables instead of migrating is forbidden.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import Engine, text

logger = logging.getLogger(__name__)

CASE_SCHEMA_COMPONENT = "case_memory"
CASE_SCHEMA_VERSION = 5

# Mapping of legacy status values to the new governed status.
# "pending_review" maps to "draft"; "human_verified" is preserved;
# everything else maps to "draft" with a migration review record.
_LEGACY_STATUS_MAP: dict[str, str] = {
    "pending_review": "draft",
    "human_verified": "human_verified",
    "auto_resolved": "draft",
    "in_progress": "draft",
    "investigating": "draft",
    "resolved": "draft",
}


def _create_version_table(conn: Any) -> None:
    """Create the schema version tracking table if it does not exist."""
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS incidentlens_schema_versions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "component VARCHAR(128) NOT NULL, "
            "version INTEGER NOT NULL, "
            "applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
        )
    )


def _current_version(conn: Any) -> int:
    """Return the current schema version for case_memory, or 0."""
    result = conn.execute(
        text(
            "SELECT COALESCE(MAX(version), 0) FROM incidentlens_schema_versions "
            "WHERE component = :component"
        ),
        {"component": CASE_SCHEMA_COMPONENT},
    ).scalar()
    return int(result or 0)


def _create_or_extend_case_memory(conn: Any) -> None:
    """Create the case_memory table with the new governed columns.

    If a legacy table exists, add new columns via ALTER TABLE.
    Never drop the table.
    """
    # Check if case_memory already exists
    table_exists = conn.execute(
        text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='case_memory'"
        )
    ).scalar()

    if table_exists is None:
        # Fresh installation — create the full table
        conn.execute(
            text(
                "CREATE TABLE case_memory ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "incident_id VARCHAR(255), "
                "source_reference VARCHAR(512) DEFAULT '', "
                "status VARCHAR(64) NOT NULL DEFAULT 'draft', "
                "revision INTEGER NOT NULL DEFAULT 1, "
                "symptom TEXT NOT NULL DEFAULT '', "
                "affected_services_json TEXT NOT NULL DEFAULT '[]', "
                "root_cause_category VARCHAR(255) NOT NULL DEFAULT '', "
                "root_cause_description TEXT NOT NULL DEFAULT '', "
                "key_evidence_json TEXT NOT NULL DEFAULT '[]', "
                "investigation_path_json TEXT NOT NULL DEFAULT '[]', "
                "invalid_hypotheses_json TEXT NOT NULL DEFAULT '[]', "
                "resolution TEXT NOT NULL DEFAULT '', "
                "remediation_advice_json TEXT NOT NULL DEFAULT '[]', "
                "applicability_conditions_json TEXT NOT NULL DEFAULT '[]', "
                "inapplicability_conditions_json TEXT NOT NULL DEFAULT '[]', "
                "environment VARCHAR(255) NOT NULL DEFAULT '', "
                "service_version_exact VARCHAR(255) NOT NULL DEFAULT '', "
                "service_version_min VARCHAR(255) NOT NULL DEFAULT '', "
                "service_version_max VARCHAR(255) NOT NULL DEFAULT '', "
                "source_report_json TEXT NOT NULL DEFAULT '{}', "
                "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"
                ")"
            )
        )
        # Unique index on incident_id (nullable — only materialized cases have it)
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_case_memory_incident_id "
                "ON case_memory(incident_id) WHERE incident_id IS NOT NULL"
            )
        )
        return

    # Legacy table exists — add missing columns
    existing_cols = {
        row[1]
        for row in conn.execute(text("PRAGMA table_info(case_memory)")).fetchall()
    }

    new_columns: list[tuple[str, str]] = [
        ("incident_id", "VARCHAR(255)"),
        ("source_reference", "VARCHAR(512) DEFAULT ''"),
        ("revision", "INTEGER NOT NULL DEFAULT 1"),
        ("affected_services_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("root_cause_category", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("root_cause_description", "TEXT NOT NULL DEFAULT ''"),
        ("key_evidence_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("investigation_path_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("invalid_hypotheses_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("remediation_advice_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("applicability_conditions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("inapplicability_conditions_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("environment", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("service_version_exact", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("service_version_min", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("service_version_max", "VARCHAR(255) NOT NULL DEFAULT ''"),
        ("source_report_json", "TEXT NOT NULL DEFAULT '{}'"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in existing_cols:
            conn.execute(
                text(f"ALTER TABLE case_memory ADD COLUMN {col_name} {col_type}")
            )

    # Create unique index on incident_id if not present
    index_exists = conn.execute(
        text(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND name='uq_case_memory_incident_id'"
        )
    ).scalar()
    if index_exists is None:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX uq_case_memory_incident_id "
                "ON case_memory(incident_id) WHERE incident_id IS NOT NULL"
            )
        )


def _create_governance_tables(conn: Any) -> None:
    """Create the review actions, feedback, usage events, and embeddings tables."""
    # Review actions — append-only audit log
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS case_review_actions ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "case_id INTEGER NOT NULL, "
            "action VARCHAR(64) NOT NULL, "
            "actor VARCHAR(255) NOT NULL DEFAULT '', "
            "reason TEXT NOT NULL DEFAULT '', "
            "previous_status VARCHAR(64), "
            "new_status VARCHAR(64) NOT NULL, "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY (case_id) REFERENCES case_memory(id)"
            ")"
        )
    )

    # Feedback with idempotency
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS case_feedback ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "case_id INTEGER NOT NULL, "
            "idempotency_key VARCHAR(255) NOT NULL, "
            "rating VARCHAR(64) NOT NULL, "
            "comment TEXT NOT NULL DEFAULT '', "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY (case_id) REFERENCES case_memory(id), "
            "UNIQUE(idempotency_key)"
            ")"
        )
    )

    # Usage events with idempotency
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS case_usage_events ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "case_id INTEGER NOT NULL, "
            "event_type VARCHAR(64) NOT NULL, "
            "idempotency_key VARCHAR(255) NOT NULL, "
            "investigation_id VARCHAR(255), "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY (case_id) REFERENCES case_memory(id), "
            "UNIQUE(idempotency_key)"
            ")"
        )
    )

    # Embeddings for vector search
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS case_embeddings ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "case_id INTEGER NOT NULL, "
            "embedding_json TEXT NOT NULL DEFAULT '[]', "
            "dimension INTEGER NOT NULL DEFAULT 0, "
            "model_name VARCHAR(255) NOT NULL DEFAULT '', "
            "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
            "FOREIGN KEY (case_id) REFERENCES case_memory(id)"
            ")"
        )
    )


def _create_fts5(conn: Any) -> None:
    """Create the FTS5 virtual table for full-text search on verified cases."""
    conn.execute(
        text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS case_fts USING fts5("
            "case_id UNINDEXED, "
            "symptom, "
            "affected_services, "
            "root_cause_category, "
            "root_cause_description"
            ")"
        )
    )


def _migrate_legacy_values(conn: Any) -> None:
    """Map legacy status values and populate new columns from old ones.

    Only processes rows that have the legacy column layout (i.e., the table
    was created before this migration). Fresh installs have no legacy columns.
    """
    # Check if legacy columns exist
    existing_cols = {
        row[1]
        for row in conn.execute(text("PRAGMA table_info(case_memory)")).fetchall()
    }
    if "service" not in existing_cols:
        # Fresh install — no legacy columns to migrate
        return

    rows = conn.execute(
        text(
            "SELECT id, status, symptom, service, root_cause, resolution, "
            "evidence_summary FROM case_memory WHERE id NOT IN "
            "(SELECT id FROM case_memory WHERE incident_id IS NOT NULL)"
        )
    ).fetchall()

    for row in rows:
        case_id = row[0]
        old_status = row[1] or "pending_review"
        service = row[3] or ""
        root_cause = row[4] or ""
        resolution = row[5] or ""
        evidence_summary = row[6] or ""

        # Map legacy status
        new_status = _LEGACY_STATUS_MAP.get(old_status, "draft")

        # Build affected_services_json
        affected_services = json.dumps([service]) if service else "[]"

        # Build key_evidence_json from evidence_summary
        key_evidence: list[dict[str, str]] = []
        if evidence_summary:
            for ev_id in evidence_summary.split(","):
                ev_id = ev_id.strip()
                if ev_id:
                    key_evidence.append({"evidence_id": ev_id})
        key_evidence_json = json.dumps(key_evidence)

        conn.execute(
            text(
                "UPDATE case_memory SET "
                "status = :status, "
                "affected_services_json = :affected, "
                "root_cause_description = :root_cause, "
                "key_evidence_json = :evidence, "
                "resolution = :resolution, "
                "source_reference = :source_ref "
                "WHERE id = :case_id"
            ),
            {
                "status": new_status,
                "affected": affected_services,
                "root_cause": root_cause,
                "evidence": key_evidence_json,
                "resolution": resolution,
                "source_ref": f"legacy-case:{case_id}",
                "case_id": case_id,
            },
        )

        # If the old status was not in the standard set, record a review action
        if old_status not in _LEGACY_STATUS_MAP:
            conn.execute(
                text(
                    "INSERT INTO case_review_actions "
                    "(case_id, action, actor, reason, previous_status, new_status) "
                    "VALUES (:case_id, 'create', 'migration', :reason, :old, :new)"
                ),
                {
                    "case_id": case_id,
                    "reason": f"migrated from legacy status '{old_status}'",
                    "old": old_status,
                    "new": new_status,
                },
            )


def _rebuild_verified_fts(conn: Any) -> None:
    """Rebuild the FTS5 index for all human_verified cases."""
    conn.execute(text("DELETE FROM case_fts"))

    rows = conn.execute(
        text(
            "SELECT id, symptom, affected_services_json, "
            "root_cause_category, root_cause_description "
            "FROM case_memory WHERE status = 'human_verified'"
        )
    ).fetchall()

    for row in rows:
        case_id = row[0]
        symptom = row[1] or ""
        affected = row[2] or "[]"
        rc_cat = row[3] or ""
        rc_desc = row[4] or ""

        conn.execute(
            text(
                "INSERT INTO case_fts(case_id, symptom, affected_services, "
                "root_cause_category, root_cause_description) "
                "VALUES (:case_id, :symptom, :affected, :rc_cat, :rc_desc)"
            ),
            {
                "case_id": case_id,
                "symptom": symptom,
                "affected": affected,
                "rc_cat": rc_cat,
                "rc_desc": rc_desc,
            },
        )


def migrate_case_schema(engine: Engine) -> None:
    """Run the non-destructive case memory schema migration.

    This function is idempotent: calling it multiple times is safe.
    It preserves old tables and uses explicit schema versioning.
    """
    with engine.begin() as conn:
        _create_version_table(conn)
        if _current_version(conn) >= CASE_SCHEMA_VERSION:
            return

        _create_or_extend_case_memory(conn)
        _create_governance_tables(conn)
        _create_fts5(conn)
        _migrate_legacy_values(conn)
        _rebuild_verified_fts(conn)

        conn.execute(
            text(
                "INSERT INTO incidentlens_schema_versions(component, version) "
                "VALUES (:component, :version)"
            ),
            {"component": CASE_SCHEMA_COMPONENT, "version": CASE_SCHEMA_VERSION},
        )
