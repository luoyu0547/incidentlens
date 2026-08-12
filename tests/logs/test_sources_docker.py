"""Docker log source tests."""

import pytest
from incidentlens_control_plane.logs.sources import DockerLogSource
from incidentlens_control_plane.logs.types import (
    LogQueryRequest,
    LogScope,
    LogSourceKind,
)
from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport

from logs.conftest import docker_subscription


@pytest.mark.asyncio
async def test_docker_log_source_uses_fixed_bounded_logs_argv(
    target_registration,
) -> None:
    transport = FakeChangeTransport()
    transport.docker_logs[("payments-api-1", 50)] = (
        b"2026-08-12T10:00:00Z ERROR token=abc\n"
    )
    source = DockerLogSource(lambda target: transport)

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.DOCKER,
        scope=LogScope.CONTAINER,
        source_ref="payments-api-1",
        tail_lines=50,
        persist=False,
        create_evidence=False,
    )

    lines = await source.query(request, target_registration)

    assert lines[0].text.endswith("ERROR token=abc")
    assert transport.run_argv_calls == [
        (
            "docker",
            "logs",
            "--timestamps",
            "--tail",
            "50",
            "--",
            "payments-api-1",
        )
    ]


@pytest.mark.asyncio
async def test_docker_stream_uses_since_cursor_and_follow_argv(
    target_registration,
) -> None:
    transport = FakeChangeTransport()
    transport.process_chunks = [
        b"2026-08-12T10:00:00Z INFO one\n",
        b"2026-08-12T10:00:01Z INFO two\n",
    ]
    source = DockerLogSource(lambda target: transport)

    lines = []
    async for line in source.stream(
        subscription=docker_subscription("payments-api-1"),
        target=target_registration,
        cursor="docker:time=2026-08-12T09:59:59Z:seq=0",
    ):
        lines.append(line)
        if len(lines) == 2:
            break

    assert [line.cursor for line in lines] == [
        "docker:time=2026-08-12T10:00:00Z:seq=1",
        "docker:time=2026-08-12T10:00:01Z:seq=2",
    ]
    assert transport.open_process_calls == [
        (
            (
                "docker",
                "logs",
                "--timestamps",
                "--follow",
                "--since",
                "2026-08-12T09:59:59Z",
                "--",
                "payments-api-1",
            ),
            None,
        )
    ]
