import sqlite3
from datetime import UTC, datetime

from incidentlens_control_plane.agent_sessions.store import AgentSessionStore
from incidentlens_control_plane.agent_sessions.types import (
    AgentMessage,
    AgentMessageRole,
    AgentSession,
    AgentSessionStatus,
)

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def make_store(tmp_path):
    store = AgentSessionStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    return store


def session(session_id="ses_1"):
    return AgentSession(
        session_id=session_id,
        target_id="tgt_1",
        service_id=None,
        title="Incident",
        owner="operator-a",
        investigation_id=None,
        status=AgentSessionStatus.IDLE,
        created_at=NOW,
        updated_at=NOW,
    )


def message(message_id="msg_1", content="hello"):
    return AgentMessage(
        message_id=message_id,
        session_id="ses_1",
        investigation_id=None,
        agent_run_id=None,
        role=AgentMessageRole.USER,
        content_redacted=content,
        transcript_sequence=None,
        created_at=NOW,
    )


def test_store_round_trips_timezone_aware_rows_and_pages_messages(tmp_path):
    store = make_store(tmp_path)
    store.create_session(session())
    store.append_message(message())

    loaded = store.get_session("ses_1")
    assert loaded.created_at == NOW
    assert loaded.created_at.tzinfo is not None
    assert store.list_messages("ses_1", limit=10)[0].message_id == "msg_1"


def test_store_migration_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    store.migrate()
    store.migrate()


def test_store_updates_projected_message_binding(tmp_path):
    store = make_store(tmp_path)
    store.create_session(session())
    store.append_message(message())
    updated = store.bind_message(
        "msg_1", investigation_id="inv_1", agent_run_id="run_1", transcript_sequence=1
    )
    assert updated.investigation_id == "inv_1"
    assert updated.agent_run_id == "run_1"
    assert updated.transcript_sequence == 1
