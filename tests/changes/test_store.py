"""Tests for ChangeSetStore and ChangeSet transitions."""

from __future__ import annotations

import sqlite3

import pytest
from incidentlens_control_plane.changes.store import (
    ChangeSetNotFound,
    ChangeSetStore,
    InvalidChangeTransition,
)
from incidentlens_control_plane.changes.types import (
    ChangeSetStatus,
    FileChange,
)


@pytest.fixture()
def store(tmp_path) -> ChangeSetStore:
    db_path = tmp_path / "changes.db"

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    s = ChangeSetStore(connect)
    s.migrate()
    return s


@pytest.fixture()
def draft_change() -> FileChange:
    return FileChange(
        file_change_id="fc-1",
        scope="target",
        remote_path="/opt/app/.env",
        expected_sha256=None,
        replacement_sha256="a" * 64,
        diff_text="+NEW_LINE",
        original_metadata={},
        local_backup_ref=None,
        remote_backup_path="/backup/.env",
        temp_path=None,
        applied=False,
        validation_result=None,
        rollback_result=None,
    )


class TestChangeSetTransitions:
    def test_draft_to_preflighted(self, store, draft_change):
        changeset = store.create(draft_change)
        assert changeset.status == ChangeSetStatus.DRAFT

        result = store.transition(
            changeset.changeset_id, ChangeSetStatus.PREFLIGHTED
        )
        assert result.status == ChangeSetStatus.PREFLIGHTED

    def test_full_happy_path(self, store, draft_change):
        changeset = store.create(draft_change)
        cs_id = changeset.changeset_id

        store.transition(cs_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(cs_id, ChangeSetStatus.LOCALLY_BACKED_UP)
        store.transition(cs_id, ChangeSetStatus.REMOTELY_BACKED_UP)
        store.transition(cs_id, ChangeSetStatus.APPLIED)
        store.transition(cs_id, ChangeSetStatus.VALIDATED)
        result = store.transition(cs_id, ChangeSetStatus.VERIFIED)

        assert result.status == ChangeSetStatus.VERIFIED

    def test_cannot_apply_before_both_backups(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )

        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)

    def test_cannot_skip_preflighted(self, store, draft_change):
        changeset = store.create(draft_change)

        with pytest.raises(InvalidChangeTransition):
            store.transition(
                changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
            )

    def test_cannot_skip_remotely_backed_up(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )

        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)

    def test_failed_from_draft(self, store, draft_change):
        changeset = store.create(draft_change)
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_failed_from_preflighted(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_failed_from_locally_backed_up(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_failed_from_remotely_backed_up(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_failed_from_applied(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_failed_from_validated(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
        store.transition(changeset.changeset_id, ChangeSetStatus.VALIDATED)
        result = store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)
        assert result.status == ChangeSetStatus.FAILED

    def test_cannot_rolled_back_from_draft(self, store, draft_change):
        changeset = store.create(draft_change)
        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.ROLLED_BACK)

    def test_cannot_rolled_back_without_apply(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )

        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.ROLLED_BACK)

    def test_rolled_back_after_applied(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
        result = store.transition(
            changeset.changeset_id, ChangeSetStatus.ROLLED_BACK
        )
        assert result.status == ChangeSetStatus.ROLLED_BACK

    def test_cannot_transition_terminal_verified(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
        store.transition(changeset.changeset_id, ChangeSetStatus.VALIDATED)
        store.transition(changeset.changeset_id, ChangeSetStatus.VERIFIED)

        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)

    def test_cannot_transition_terminal_failed(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.FAILED)

        with pytest.raises(InvalidChangeTransition):
            store.transition(
                changeset.changeset_id, ChangeSetStatus.PREFLIGHTED
            )

    def test_cannot_transition_terminal_rolled_back(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)
        store.transition(
            changeset.changeset_id, ChangeSetStatus.LOCALLY_BACKED_UP
        )
        store.transition(
            changeset.changeset_id, ChangeSetStatus.REMOTELY_BACKED_UP
        )
        store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)
        store.transition(changeset.changeset_id, ChangeSetStatus.ROLLED_BACK)

        with pytest.raises(InvalidChangeTransition):
            store.transition(changeset.changeset_id, ChangeSetStatus.APPLIED)

    def test_changeset_not_found(self, store):
        with pytest.raises(ChangeSetNotFound):
            store.transition("nonexistent", ChangeSetStatus.PREFLIGHTED)

    def test_store_preserves_files(self, store, draft_change):
        changeset = store.create(draft_change)
        assert len(changeset.files) == 1
        assert changeset.files[0].file_change_id == "fc-1"

    def test_store_assigns_timestamps(self, store, draft_change):
        changeset = store.create(draft_change)
        assert changeset.created_at is not None
        assert changeset.updated_at is not None

    def test_store_persists_transition(self, store, draft_change):
        changeset = store.create(draft_change)
        store.transition(changeset.changeset_id, ChangeSetStatus.PREFLIGHTED)

        fetched = store.get(changeset.changeset_id)
        assert fetched is not None
        assert fetched.status == ChangeSetStatus.PREFLIGHTED
