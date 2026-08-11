import sqlite3

import pytest
from incidentlens_control_plane.approvals.service import ApprovalService
from incidentlens_control_plane.approvals.store import ApprovalStore
from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.store import RuntimeEventStore


@pytest.fixture()
def approval_service(tmp_path):
    """Create an ApprovalService backed by a temporary SQLite database."""
    db_path = tmp_path / "approvals.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    approvals = ApprovalStore(connect)
    events = RuntimeEventStore(connect)
    broker = RuntimeEventBroker()
    approvals.migrate()
    events.migrate()

    return ApprovalService(
        approvals=approvals,
        events=events,
        broker=broker,
    )
