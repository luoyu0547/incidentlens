"""Tests for case schema migrations — TDD RED phase."""

from incidentlens_control_plane.memory.migrations import migrate_case_schema
from incidentlens_telemetry.database import create_engine
from sqlalchemy import inspect, text


def test_legacy_case_is_preserved_and_mapped() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE case_memory ("
            "id INTEGER PRIMARY KEY, status VARCHAR(64), symptom TEXT, "
            "service VARCHAR(255), root_cause TEXT, resolution TEXT, "
            "evidence_summary TEXT, created_at DATETIME, updated_at DATETIME)"
        )
        conn.execute(
            text(
                "INSERT INTO case_memory "
                "(id,status,symptom,service,root_cause,resolution,evidence_summary) "
                "VALUES (1,'pending_review','timeout','order-service',"
                "'payment_delay','rollback','ev-1')"
            )
        )

    migrate_case_schema(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, affected_services_json, root_cause_description, "
                "key_evidence_json FROM case_memory WHERE id=1"
            )
        ).mappings().one()
        assert row["status"] == "draft"
        assert row["affected_services_json"] == '["order-service"]'
        assert row["root_cause_description"] == "payment_delay"
        assert "ev-1" in row["key_evidence_json"]


def test_migration_creates_governance_tables_and_real_fts5() -> None:
    engine = create_engine("sqlite:///:memory:")
    migrate_case_schema(engine)
    names = set(inspect(engine).get_table_names())
    assert {
        "case_memory",
        "case_review_actions",
        "case_feedback",
        "case_usage_events",
        "case_embeddings",
        "incidentlens_schema_versions",
    } <= names
    with engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name='case_fts'")
        ).scalar_one()
    assert "VIRTUAL TABLE" in sql.upper()
    assert "FTS5" in sql.upper()


def test_migration_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    migrate_case_schema(engine)
    migrate_case_schema(engine)
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM incidentlens_schema_versions "
                "WHERE component='case_memory' AND version=6"
            )
        ).scalar_one()
    assert count == 1
