"""Unit tests for the ``execute_idempotent`` helper (2xx-only replay contract).

These drive :func:`execute_idempotent` directly (no HTTP layer) to pin the
semantics future mutation routes depend on: only a 2xx outcome records a
replayable completed result, and a 4xx/5xx re-arms the short lease so a later
same-key retry re-runs instead of replaying a non-2xx.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from incidentlens_control_plane.api.idempotency import execute_idempotent
from incidentlens_control_plane.auth.types import (
    AuthenticationMethod,
    Principal,
    PrincipalScope,
)
from incidentlens_control_plane.idempotency.service import IdempotencyService
from incidentlens_control_plane.idempotency.store import IdempotencyStore
from incidentlens_control_plane.idempotency.types import IdempotencyState
from pydantic import BaseModel


class Result(BaseModel):
    """A minimal response body model for the helper tests."""

    value: str


PRINCIPAL = Principal(
    principal_id="operator-a",
    display_name="Operator A",
    scopes=frozenset({PrincipalScope.OPERATE}),
    authentication_method=AuthenticationMethod.BEARER,
)

#: The idempotency identity shared across a replayed/retried key.
IDENTITY = {
    "method": "POST",
    "route_key": "/api/v1/things",
    "idempotency_key": "key-1",
}


def make_service(tmp_path: Path) -> IdempotencyService:
    store = IdempotencyStore(lambda: sqlite3.connect(tmp_path / "idem.db"))
    store.migrate()
    return IdempotencyService(store)


async def test_non_2xx_result_is_not_pinned_for_replay(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    calls = {"n": 0}

    async def action_four_oh_four() -> tuple[int, Result]:
        calls["n"] += 1
        return 404, Result(value="reject")

    status, body, replayed = await execute_idempotent(
        service=service,
        principal=PRINCIPAL,
        **IDENTITY,
        request_sha256="sha-4xx",
        response_type=Result,
        action=action_four_oh_four,
    )
    assert (status, body.value, replayed) == (404, "reject", False)

    # The 404 is NOT pinned as a replayable completed result.
    record = service.get(principal_id=PRINCIPAL.principal_id, **IDENTITY)
    assert record is not None
    assert record.state == IdempotencyState.IN_PROGRESS
    assert record.status_code is None
    assert record.response_json is None

    # Force the failed lease to expire, then a same-key retry re-runs and the
    # 2xx outcome becomes the replayable one.
    service.keep_alive_after_failure(
        principal_id=PRINCIPAL.principal_id,
        **IDENTITY,
        now=datetime.now(UTC) - timedelta(seconds=121),
    )

    async def action_created() -> tuple[int, Result]:
        calls["n"] += 1
        return 201, Result(value="ok")

    status, body, replayed = await execute_idempotent(
        service=service,
        principal=PRINCIPAL,
        **IDENTITY,
        request_sha256="sha-4xx",  # same key + same hash must NOT replay
        response_type=Result,
        action=action_created,
    )
    assert (status, body.value, replayed) == (201, "ok", False)
    assert calls["n"] == 2

    # And now that a 2xx is stored, a third call replays it exactly.
    status, body, replayed = await execute_idempotent(
        service=service,
        principal=PRINCIPAL,
        **IDENTITY,
        request_sha256="sha-4xx",
        response_type=Result,
        action=action_created,
    )
    assert (status, body.value, replayed) == (201, "ok", True)
    assert calls["n"] == 2


async def test_five_xx_result_is_not_pinned_for_replay(tmp_path: Path) -> None:
    service = make_service(tmp_path)
    calls = {"n": 0}

    async def action_failure() -> tuple[int, Result]:
        calls["n"] += 1
        return 502, Result(value="downstream")

    status, body, replayed = await execute_idempotent(
        service=service,
        principal=PRINCIPAL,
        **IDENTITY,
        request_sha256="sha-5xx",
        response_type=Result,
        action=action_failure,
    )
    assert (status, body.value, replayed) == (502, "downstream", False)

    record = service.get(principal_id=PRINCIPAL.principal_id, **IDENTITY)
    assert record is not None
    assert record.state == IdempotencyState.IN_PROGRESS
    assert record.status_code is None
    assert record.response_json is None
