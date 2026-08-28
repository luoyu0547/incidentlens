from __future__ import annotations

from datetime import UTC, datetime

import pytest
from incidentlens_control_plane.logs.cursors import (
    decode_log_cursor,
    encode_log_cursor,
)
from incidentlens_control_plane.logs.types import (
    LogRecord,
    LogScope,
    LogSeverity,
    LogSourceKind,
)


def make_record(message: str, *, dedupe_key: str) -> LogRecord:
    return LogRecord(
        log_id=f"log-{dedupe_key}",
        subscription_id=None,
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        cursor=f"offset:{dedupe_key}",
        dedupe_key=dedupe_key,
        observed_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
        event_time=None,
        severity=LogSeverity.ERROR,
        message_redacted=message,
        redaction_summary={},
        created_at=datetime(2026, 8, 12, 10, 0, tzinfo=UTC),
    )


def test_product_cursor_round_trip() -> None:
    cursor = encode_log_cursor(42)
    assert decode_log_cursor(cursor) == 42


def _insert_records(store, count: int = 3) -> None:
    records = tuple(
        make_record(f"message-{index}", dedupe_key=f"dedupe-{index}").model_copy(
            update={
                "log_id": f"log-{index}",
                "service_name": "payment-api",
                "target_id": "dev-a",
                "severity": LogSeverity.ERROR if index == 0 else LogSeverity.INFO,
            }
        )
        for index in range(count)
    )
    store.append_batch(records)


def test_product_page_has_stable_sequences_and_snapshot(store) -> None:
    _insert_records(store)
    page, has_more = store.list_product_page(
        service_name="payment-api", limit=2, allowed_target_ids=frozenset({"dev-a"})
    )

    assert [record.stream_sequence for record in page] == [1, 2]
    assert has_more is True
    snapshot = page[-1].stream_sequence
    _insert_records(store, count=3)
    next_page, next_more = store.list_product_page(
        service_name="payment-api",
        after_sequence=snapshot,
        snapshot_sequence=snapshot + 1,
        limit=10,
        allowed_target_ids=frozenset({"dev-a"}),
    )
    assert [record.stream_sequence for record in next_page] == [3]
    assert next_more is False


def test_product_page_filters_severity_and_authorized_targets(store) -> None:
    _insert_records(store)
    hidden = make_record("hidden", dedupe_key="hidden").model_copy(
        update={"log_id": "hidden", "target_id": "other", "severity": LogSeverity.ERROR}
    )
    store.append_batch((hidden,))

    page, has_more = store.list_product_page(
        service_name="payment-api",
        severity=LogSeverity.ERROR.value,
        limit=10,
        allowed_target_ids=frozenset({"dev-a"}),
    )
    assert [record.message_redacted for record in page] == ["message-0"]
    assert has_more is False


def test_product_page_rejects_mutual_cursors(store) -> None:
    with pytest.raises(ValueError):
        store.list_product_page(
            service_name="payment-api", before_sequence=1, after_sequence=0
        )
