import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from incidentlens_control_plane.logs.store import LogSearchFilters, LogStore
from incidentlens_control_plane.logs.types import LogRecord, LogScope, LogSeverity, LogSourceKind


def make_store(tmp_path: Path) -> LogStore:
    store = LogStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    return store


def make_record(message: str, *, dedupe_key: str = "dedupe-1") -> LogRecord:
    return LogRecord(
        log_id="log-1",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor="offset:1",
        dedupe_key=dedupe_key,
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted=message,
        redaction_summary={"token": 1},
        normal_signal=None,
        correlation_key="trace:abc",
        evidence_ref_id=None,
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def test_append_batch_deduplicates_by_dedupe_key(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    first = make_record("[REDACTED_TOKEN] failed")
    duplicate = first.model_copy(update={"log_id": "log-2"})

    inserted = store.append_batch((first, duplicate))

    assert inserted == (first,)
    assert store.search(LogSearchFilters(project_id="payments"), limit=10) == (first,)


def test_fts_search_indexes_only_redacted_message(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append_batch((make_record("[REDACTED_TOKEN] database failed"),))

    results = store.search(LogSearchFilters(project_id="payments", text="database"), limit=10)

    assert len(results) == 1
    assert "abc123" not in results[0].model_dump_json()
    assert results[0].message_redacted == "[REDACTED_TOKEN] database failed"


def test_schema_has_no_raw_message_column(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.migrate()
    with sqlite3.connect(tmp_path / "runtime.db") as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(log_records)").fetchall()
        }

    assert "raw_message" not in columns
    assert "message_redacted" in columns


def test_active_opt_in_subscriptions_are_listed_for_restore(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    active = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=datetime(2026, 8, 12, tzinfo=UTC),
    )
    paused = store.pause_subscription(active.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC))

    assert paused.status.value == "paused"
    assert store.list_active_opt_in_subscriptions() == ()

    resumed = store.resume_subscription(
        active.subscription_id, now=datetime(2026, 8, 12, tzinfo=UTC)
    )
    assert resumed.status.value == "active"
    assert store.list_active_opt_in_subscriptions() == (resumed,)


def test_mark_subscription_error_updates_existing_row_only(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    subscription = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=now,
    )

    # A never-created id must NOT fabricate a row.
    assert store.mark_subscription_error("ghost-sub", "boom", now) is None
    assert store.get_subscription("ghost-sub") is None

    # An existing row is updated with the redacted summary.
    updated = store.mark_subscription_error(
        subscription.subscription_id, "[REDACTED_TOKEN] boom", now
    )
    assert updated is not None
    assert updated.status.value == "error"
    assert updated.last_error_redacted == "[REDACTED_TOKEN] boom"


def test_cursor_upsert_round_trips(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)

    store.upsert_cursor(
        subscription_id="sub-1",
        cursor="offset:42",
        generation="mtime=1:size=42",
        observed_at=now,
        now=now,
    )

    cursor = store.get_cursor("sub-1")
    assert cursor is not None
    assert cursor.cursor == "offset:42"
    assert cursor.generation == "mtime=1:size=42"


def _subscription_records(tmp_path: Path):
    """Insert three records whose cursors do NOT sort lexicographically."""
    store = make_store(tmp_path)
    now = datetime(2026, 8, 12, tzinfo=UTC)
    subscription = store.create_subscription(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        opt_in_streaming=True,
        created_by="alice",
        now=now,
    )
    records = (
        make_record("first", dedupe_key="d1").model_copy(
            update={
                "log_id": "log-s1",
                "subscription_id": subscription.subscription_id,
                "cursor": "file:offset=100",
            }
        ),
        make_record("second", dedupe_key="d2").model_copy(
            update={
                "log_id": "log-s2",
                "subscription_id": subscription.subscription_id,
                "cursor": "file:offset=99",
            }
        ),
        make_record("third", dedupe_key="d3").model_copy(
            update={
                "log_id": "log-s3",
                "subscription_id": subscription.subscription_id,
                "cursor": "file:offset=1000",
            }
        ),
    )
    store.append_batch(records)
    return store, subscription, records


def test_list_records_for_subscription_orders_by_insertion_not_cursor(
    tmp_path: Path,
) -> None:
    store, subscription, _records = _subscription_records(tmp_path)

    listed = store.list_records_for_subscription(subscription.subscription_id)

    # Insertion order (rowid), NOT the lexicographic cursor order
    # ("file:offset=100" < "file:offset=1000" < "file:offset=99" as strings).
    assert [r.cursor for r in listed] == [
        "file:offset=100",
        "file:offset=99",
        "file:offset=1000",
    ]


def test_list_records_for_subscription_after_cursor_resumes_by_rowid(
    tmp_path: Path,
) -> None:
    store, subscription, _records = _subscription_records(tmp_path)

    page1 = store.list_records_for_subscription(subscription.subscription_id, limit=2)
    assert [r.cursor for r in page1] == ["file:offset=100", "file:offset=99"]

    page2 = store.list_records_for_subscription(
        subscription.subscription_id,
        after_cursor=page1[-1].cursor,
        limit=100,
    )
    assert [r.cursor for r in page2] == ["file:offset=1000"]


def test_search_start_time_normalizes_to_utc(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    store.append_batch(
        (
            make_record("before", dedupe_key="t1").model_copy(
                update={
                    "log_id": "log-t1",
                    "observed_at": datetime(2026, 8, 12, 15, 0, tzinfo=UTC),
                }
            ),
            make_record("after", dedupe_key="t2").model_copy(
                update={
                    "log_id": "log-t2",
                    "observed_at": datetime(2026, 8, 12, 16, 30, tzinfo=UTC),
                }
            ),
        )
    )
    # 11:00 -05:00 == 16:00 UTC; 18:00 -05:00 == 23:00 UTC.
    start = datetime(2026, 8, 12, 11, 0, tzinfo=timezone(timedelta(hours=-5)))
    end = datetime(2026, 8, 12, 18, 0, tzinfo=timezone(timedelta(hours=-5)))

    results = store.search(
        LogSearchFilters(start_time=start, end_time=end), limit=10
    )

    assert [r.message_redacted for r in results] == ["after"]
