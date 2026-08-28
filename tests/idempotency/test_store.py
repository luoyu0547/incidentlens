"""Store-level tests for mutation idempotency persistence.

These tests drive :class:`IdempotencyStore` directly against a throwaway
SQLite file so the atomic reservation, replay/conflict/in-progress outcomes,
TTL lifecycle, and pruning are verified without the HTTP layer.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from incidentlens_control_plane.idempotency.store import (
    COMPLETED_RETENTION_SECONDS,
    FAILED_RESERVATION_LEASE_SECONDS,
    RESERVATION_LEASE_SECONDS,
    IdempotencyStore,
)
from incidentlens_control_plane.idempotency.types import (
    IdempotencyState,
    ReservationStatus,
)

PK = {
    "principal_id": "operator-a",
    "method": "POST",
    "route_key": "/api/v1/test-idempotent",
    "idempotency_key": "target-create-1",
}

NOW = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def make_store(tmp_path: Path) -> tuple[IdempotencyStore, Path]:
    db_path = tmp_path / "idempotency.db"
    store = IdempotencyStore(lambda: sqlite3.connect(db_path))
    store.migrate()
    return store, db_path


def test_migrate_creates_table_index_and_is_idempotent(tmp_path: Path) -> None:
    store, db_path = make_store(tmp_path)
    with sqlite3.connect(db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "api_idempotency_keys" in tables
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_api_idempotency_expiry" in indexes

    # Re-running migrate must be a clean no-op (rows survive).
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.migrate()
    assert store.get(**PK) is not None


def test_reserve_inserts_in_progress_row(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    reservation = store.reserve(request_sha256="sha-a", now=NOW, **PK)

    assert reservation.status == ReservationStatus.RESERVED
    record = store.get(**PK)
    assert record is not None
    assert record.state == IdempotencyState.IN_PROGRESS
    assert record.request_sha256 == "sha-a"
    assert record.created_at == NOW
    assert record.completed_at is None
    assert record.expires_at == NOW + timedelta(seconds=RESERVATION_LEASE_SECONDS)


def test_lease_policy_separates_fresh_and_failure_leases(tmp_path: Path) -> None:
    """A fresh reservation is held much longer than a failed one is re-armed.

    A legitimate slow mutation retried, say, 90 seconds in must NOT double
    execute: only a non-2xx/failed reservation is reclaimable after the short
    60s lease.
    """
    assert RESERVATION_LEASE_SECONDS > FAILED_RESERVATION_LEASE_SECONDS


def test_reserve_same_key_different_hash_is_conflict(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.mark_completed(
        status_code=201, response_json='{"value":"a"}', now=NOW, **PK
    )

    reservation = store.reserve(request_sha256="sha-b", now=NOW, **PK)

    assert reservation.status == ReservationStatus.CONFLICT


def test_reserve_replays_matching_completed_row(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.mark_completed(
        status_code=201, response_json='{"value":"a"}', now=NOW, **PK
    )

    reservation = store.reserve(request_sha256="sha-a", now=NOW, **PK)

    assert reservation.status == ReservationStatus.REPLAY
    assert reservation.status_code == 201
    assert reservation.response_json == '{"value":"a"}'


def test_reserve_while_in_progress_returns_in_progress(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)

    reservation = store.reserve(request_sha256="sha-a", now=NOW, **PK)

    assert reservation.status == ReservationStatus.IN_PROGRESS


def test_mark_completed_sets_24h_retention(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.mark_completed(
        status_code=201, response_json='{"value":"a"}', now=NOW, **PK
    )

    record = store.get(**PK)
    assert record is not None
    assert record.state == IdempotencyState.COMPLETED
    assert record.completed_at == NOW
    assert record.expires_at == NOW + timedelta(
        seconds=COMPLETED_RETENTION_SECONDS
    )


def test_completed_row_is_reclaimed_after_retention(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.mark_completed(
        status_code=201, response_json='{"value":"a"}', now=NOW, **PK
    )

    later = NOW + timedelta(seconds=COMPLETED_RETENTION_SECONDS + 1)
    reservation = store.reserve(request_sha256="sha-a", now=later, **PK)

    assert reservation.status == ReservationStatus.RESERVED


def test_rearm_lease_refreshes_short_ttl_and_stays_in_progress(
    tmp_path: Path,
) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.rearm_lease(now=NOW, **PK)

    record = store.get(**PK)
    assert record is not None
    assert record.state == IdempotencyState.IN_PROGRESS
    assert record.expires_at == NOW + timedelta(
        seconds=FAILED_RESERVATION_LEASE_SECONDS
    )


def test_fresh_in_progress_is_held_through_short_lease(tmp_path: Path) -> None:
    """A legit slow mutation retried ~90s in is not double-executed."""
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)

    later = NOW + timedelta(seconds=FAILED_RESERVATION_LEASE_SECONDS + 1)
    reservation = store.reserve(request_sha256="sha-a", now=later, **PK)

    assert reservation.status == ReservationStatus.IN_PROGRESS


def test_rearmed_in_progress_is_reclaimed_after_short_lease(
    tmp_path: Path,
) -> None:
    """A failed (non-2xx) reservation is reclaimable after ~60s."""
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.rearm_lease(now=NOW, **PK)

    later = NOW + timedelta(seconds=FAILED_RESERVATION_LEASE_SECONDS + 1)
    reservation = store.reserve(request_sha256="sha-a", now=later, **PK)

    assert reservation.status == ReservationStatus.RESERVED
    assert store.get(**PK).state == IdempotencyState.IN_PROGRESS


def test_fresh_in_progress_is_reclaimed_after_long_lease(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)

    later = NOW + timedelta(seconds=RESERVATION_LEASE_SECONDS + 1)
    reservation = store.reserve(request_sha256="sha-a", now=later, **PK)

    assert reservation.status == ReservationStatus.RESERVED
    assert store.get(**PK).state == IdempotencyState.IN_PROGRESS


def test_prune_expired_removes_rows(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)
    store.mark_completed(
        status_code=201, response_json='{"value":"a"}', now=NOW, **PK
    )

    later = NOW + timedelta(hours=25)
    removed = store.prune_expired(later)

    assert removed == 1
    assert store.get(**PK) is None


def test_reservations_are_scoped_by_principal_and_route(
    tmp_path: Path,
) -> None:
    store, _ = make_store(tmp_path)
    store.reserve(request_sha256="sha-a", now=NOW, **PK)

    different_principal = dict(PK, principal_id="operator-b")
    reservation = store.reserve(
        request_sha256="sha-b", now=NOW, **different_principal
    )
    assert reservation.status == ReservationStatus.RESERVED

    different_route = dict(PK, route_key="/api/v1/test-idempotent/other")
    reservation = store.reserve(
        request_sha256="sha-b", now=NOW, **different_route
    )
    assert reservation.status == ReservationStatus.RESERVED

    different_key = dict(PK, idempotency_key="target-create-2")
    reservation = store.reserve(
        request_sha256="sha-b", now=NOW, **different_key
    )
    assert reservation.status == ReservationStatus.RESERVED
