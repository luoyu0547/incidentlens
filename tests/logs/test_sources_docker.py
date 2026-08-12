"""Docker log source tests."""

import pytest


@pytest.mark.asyncio
async def test_docker_log_source_uses_fixed_bounded_logs_argv(
    target_registration,
) -> None:
    from incidentlens_control_plane.logs.sources import DockerLogSource
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeChangeTransport

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
