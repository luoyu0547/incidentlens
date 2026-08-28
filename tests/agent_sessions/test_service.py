import sqlite3
from datetime import UTC, datetime

from incidentlens_control_plane.agent_sessions.service import AgentSessionService
from incidentlens_control_plane.agent_sessions.store import AgentSessionStore
from incidentlens_control_plane.agent_sessions.types import AgentMessageRole
from incidentlens_control_plane.operations.service import OperationService


def test_create_session_and_accept_message(tmp_path):
    database = tmp_path / "runtime.db"

    def connect():
        return sqlite3.connect(database)

    sessions = AgentSessionStore(connect)
    sessions.migrate()
    operations = OperationService(store=_operation_store(connect), publisher=_publisher())
    service = AgentSessionService(sessions=sessions, operations=operations)

    created = service.create_session(
        principal_id="operator-a",
        target_id="tgt_1",
        title="Payment incident",
        service_id="payment-service",
        now=datetime.now(UTC),
    )
    accepted = service.accept_message(
        session_id=created.session_id,
        principal_id="operator-a",
        content="调查 payment-service",
        now=datetime.now(UTC),
    )

    assert accepted.accepted is True
    assert accepted.operation_id.startswith("op_")
    message = sessions.get_message(accepted.message_id)
    assert message.role is AgentMessageRole.USER
    assert message.content_redacted == "调查 payment-service"


def _operation_store(connect):
    from incidentlens_control_plane.operations.store import OperationStore

    store = OperationStore(connect)
    store.migrate()
    return store


def _publisher():
    class Publisher:
        def operation_queued(self, operation):
            return None

    return Publisher()
