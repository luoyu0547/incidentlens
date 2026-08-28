"""SQLite persistence for the Agent Session product facade.

Follows the runtime.db / sqlite3 conventions of the sibling stores: an
idempotent ``migrate()``, validated Pydantic JSON in every ``record_json``
column, and conditional UPDATEs so concurrent writers cannot clobber each
other.  Message text is stored *already redacted* by the caller; raw tool
arguments, results and hidden provider reasoning never reach this store.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from incidentlens_control_plane.agent_sessions.types import (
    AgentMessage,
    AgentMessageRole,
    AgentSession,
    AgentSessionStatus,
)

_SESSION_COLUMNS = (
    "session_id",
    "target_id",
    "service_id",
    "title",
    "owner",
    "investigation_id",
    "status",
    "created_at",
    "updated_at",
)

_MESSAGE_COLUMNS = (
    "message_id",
    "session_id",
    "investigation_id",
    "agent_run_id",
    "role",
    "content_redacted",
    "transcript_sequence",
    "created_at",
)


class AgentSessionNotFound(Exception):
    """Raised when an agent session row is missing."""


class AgentSessionAlreadyExists(Exception):
    """Raised when a create targets an existing session id."""


class AgentMessageNotFound(Exception):
    """Raised when an agent message row is missing."""


class AgentMessageConflict(Exception):
    """Raised when a projected message id already exists with different content."""


def _iso(value) -> str | None:
    return value.isoformat() if value is not None else None


def _placeholders(count: int) -> str:
    return ", ".join("?" for _ in range(count))


class AgentSessionStore:
    """SQLite-backed store for agent sessions and projected messages."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]) -> None:
        self._connection_factory = connection_factory

    def migrate(self) -> None:
        """Create the agent session tables and indexes if they don't exist."""
        with self._connection_factory() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS agent_sessions (
                    session_id TEXT PRIMARY KEY,
                    target_id TEXT NOT NULL,
                    service_id TEXT,
                    title TEXT,
                    owner TEXT NOT NULL,
                    investigation_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_owner_update
                    ON agent_sessions(owner, updated_at);
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_target_update
                    ON agent_sessions(target_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_agent_sessions_investigation
                    ON agent_sessions(investigation_id);

                CREATE TABLE IF NOT EXISTS agent_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    investigation_id TEXT,
                    agent_run_id TEXT,
                    role TEXT NOT NULL,
                    content_redacted TEXT NOT NULL,
                    transcript_sequence INTEGER,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_messages_session_created
                    ON agent_messages(session_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_agent_messages_investigation_seq
                    ON agent_messages(investigation_id, transcript_sequence);
                """
            )
            conn.commit()

    # -- sessions -------------------------------------------------------------

    def create_session(self, session: AgentSession) -> AgentSession:
        """Persist a new session; raise AgentSessionAlreadyExists on a duplicate."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO agent_sessions ({", ".join(_SESSION_COLUMNS)})
                    VALUES ({_placeholders(len(_SESSION_COLUMNS))})
                    """,
                    (
                        session.session_id,
                        session.target_id,
                        session.service_id,
                        session.title,
                        session.owner,
                        session.investigation_id,
                        session.status.value,
                        _iso(session.created_at),
                        _iso(session.updated_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise AgentSessionAlreadyExists(
                    f"agent session already exists: {session.session_id}"
                ) from exc
        return session

    def get_session(self, session_id: str) -> AgentSession:
        """Return one session, or raise AgentSessionNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_SESSION_COLUMNS)}
                FROM agent_sessions WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
        if row is None:
            raise AgentSessionNotFound(f"agent session not found: {session_id}")
        return self._session_from_row(row)

    def list_sessions(
        self,
        *,
        owner: str | None = None,
        target_id: str | None = None,
        status: AgentSessionStatus | None = None,
    ) -> tuple[AgentSession, ...]:
        """Return sessions filtered by owner, target or status, newest first."""
        clauses: list[str] = []
        params: list[object] = []
        if owner is not None:
            clauses.append("owner = ?")
            params.append(owner)
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connection_factory() as conn:
            rows = conn.execute(
                f"""
                SELECT {", ".join(_SESSION_COLUMNS)}
                FROM agent_sessions {where_sql}
                ORDER BY updated_at DESC
                """,
                tuple(params),
            ).fetchall()
        return tuple(self._session_from_row(row) for row in rows)

    def find_session_by_investigation(self, investigation_id: str) -> AgentSession:
        """Return the session bound to *investigation_id*, or raise."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_SESSION_COLUMNS)}
                FROM agent_sessions WHERE investigation_id = ?
                """,
                (investigation_id,),
            ).fetchone()
        if row is None:
            raise AgentSessionNotFound(
                f"no agent session bound to investigation {investigation_id}"
            )
        return self._session_from_row(row)

    def bind_investigation(
        self,
        session_id: str,
        investigation_id: str,
        *,
        now,
        status: AgentSessionStatus,
    ) -> AgentSession:
        """Bind a session to its current investigation (CREATED/RUNNING session)."""
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT status FROM agent_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise AgentSessionNotFound(f"agent session not found: {session_id}")
            now_iso = _iso(now)
            conn.execute(
                """
                UPDATE agent_sessions
                SET investigation_id = ?, status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (investigation_id, status.value, now_iso, session_id),
            )
            conn.commit()
        return self.get_session(session_id)

    def update_session(self, session: AgentSession) -> AgentSession:
        """Replace a non-identity session row (title, status, updated_at)."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_sessions
                SET title = ?, service_id = ?, investigation_id = ?,
                    status = ?, updated_at = ?
                WHERE session_id = ?
                """,
                (
                    session.title,
                    session.service_id,
                    session.investigation_id,
                    session.status.value,
                    _iso(session.updated_at),
                    session.session_id,
                ),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise AgentSessionNotFound(f"agent session not found: {session.session_id}")
        return session

    # -- messages -------------------------------------------------------------

    def append_message(self, message: AgentMessage) -> AgentMessage:
        """Persist one projected message; raise AgentMessageConflict on a duplicate id."""
        with self._connection_factory() as conn:
            try:
                conn.execute(
                    f"""
                    INSERT INTO agent_messages ({", ".join(_MESSAGE_COLUMNS)})
                    VALUES ({_placeholders(len(_MESSAGE_COLUMNS))})
                    """,
                    (
                        message.message_id,
                        message.session_id,
                        message.investigation_id,
                        message.agent_run_id,
                        message.role.value,
                        message.content_redacted,
                        message.transcript_sequence,
                        _iso(message.created_at),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                existing = self.get_message(message.message_id)
                if existing == message:
                    return existing
                raise AgentMessageConflict(
                    f"projected message id already exists with different content: "
                    f"{message.message_id}"
                ) from exc
        return message

    def bind_message(
        self,
        message_id: str,
        *,
        investigation_id: str | None,
        agent_run_id: str | None,
        transcript_sequence: int | None,
    ) -> AgentMessage:
        """Attach a projected message to durable investigation transcript identity."""
        with self._connection_factory() as conn:
            cursor = conn.execute(
                """
                UPDATE agent_messages
                SET investigation_id = ?, agent_run_id = ?, transcript_sequence = ?
                WHERE message_id = ?
                """,
                (investigation_id, agent_run_id, transcript_sequence, message_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                raise AgentMessageNotFound(f"agent message not found: {message_id}")
        return self.get_message(message_id)

    def get_message(self, message_id: str) -> AgentMessage:
        """Return one projected message, or raise AgentMessageNotFound."""
        with self._connection_factory() as conn:
            row = conn.execute(
                f"""
                SELECT {", ".join(_MESSAGE_COLUMNS)}
                FROM agent_messages WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
        if row is None:
            raise AgentMessageNotFound(f"agent message not found: {message_id}")
        return self._message_from_row(row)

    def list_messages(
        self,
        session_id: str,
        *,
        after_message_id: str | None = None,
        limit: int = 100,
    ) -> tuple[AgentMessage, ...]:
        """Return messages for a session, oldest first.

        ``after_message_id`` is an opaque forward cursor: it returns messages
        that come after that message in deterministic (created_at, message_id)
        order, so a client can page forward without exposing numeric offsets.
        """
        if not (1 <= limit <= 500):
            raise ValueError("limit must be between 1 and 500")
        with self._connection_factory() as conn:
            params: list[object] = [session_id]
            clauses = ["session_id = ?"]
            if after_message_id is not None:
                row = conn.execute(
                    "SELECT created_at, message_id FROM agent_messages WHERE message_id = ?",
                    (after_message_id,),
                ).fetchone()
                if row is None:
                    raise AgentMessageNotFound(f"agent message not found: {after_message_id}")
                clauses.append("(created_at > ? OR (created_at = ? AND message_id > ?))")
                params.extend((row[0], row[0], after_message_id))
            params.append(limit)
            rows = conn.execute(
                f"""
                SELECT {", ".join(_MESSAGE_COLUMNS)}
                FROM agent_messages
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC, message_id ASC
                LIMIT ?
                """,
                tuple(params),
            ).fetchall()
        return tuple(self._message_from_row(row) for row in rows)

    def count_messages(self, session_id: str) -> int:
        """Return how many messages a session currently has."""
        with self._connection_factory() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM agent_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row[0])

    # -- row mapping ----------------------------------------------------------

    @staticmethod
    def _session_from_row(row) -> AgentSession:
        return AgentSession(
            session_id=row[0],
            target_id=row[1],
            service_id=row[2],
            title=row[3],
            owner=row[4],
            investigation_id=row[5],
            status=AgentSessionStatus(row[6]),
            created_at=datetime.fromisoformat(str(row[7])).astimezone(UTC),
            updated_at=datetime.fromisoformat(str(row[8])).astimezone(UTC),
        )

    @staticmethod
    def _message_from_row(row) -> AgentMessage:
        return AgentMessage(
            message_id=row[0],
            session_id=row[1],
            investigation_id=row[2],
            agent_run_id=row[3],
            role=AgentMessageRole(row[4]),
            content_redacted=row[5],
            transcript_sequence=row[6],
            created_at=datetime.fromisoformat(str(row[7])).astimezone(UTC),
        )


__all__ = [
    "AgentMessageConflict",
    "AgentMessageNotFound",
    "AgentSessionAlreadyExists",
    "AgentSessionNotFound",
    "AgentSessionStore",
]
