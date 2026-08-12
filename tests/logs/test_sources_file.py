"""File log source tests."""

from pathlib import PurePosixPath

import pytest


@pytest.mark.asyncio
async def test_file_log_source_reads_tail_without_loading_full_file(
    target_registration,
) -> None:
    from incidentlens_control_plane.logs.sources import FileLogSource
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    factory = FakeTransportFactory()
    session = await SessionManager(factory).connect(target_registration)
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        b"old\n" + b"x" * 10_000 + b"\nERROR token=abc\n"
    )
    source = FileLogSource(SessionManager(factory))

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        tail_lines=1,
        persist=False,
        create_evidence=False,
    )

    lines = await source.query(
        request, target_registration, PurePosixPath("/var/log/payment/app.log")
    )

    assert len(lines) == 1
    assert lines[0].text == "ERROR token=abc"
    assert lines[0].cursor.startswith("file:")


@pytest.mark.asyncio
async def test_file_log_source_reads_tail_of_large_file(
    target_registration,
) -> None:
    """On-demand query of a file larger than the read bound must return the TAIL."""
    from incidentlens_control_plane.logs.sources import FileLogSource
    from incidentlens_control_plane.logs.types import (
        LogQueryRequest,
        LogScope,
        LogSourceKind,
    )
    from incidentlens_control_plane.remote_ops.fakes import FakeTransportFactory
    from incidentlens_control_plane.remote_ops.sessions import SessionManager

    factory = FakeTransportFactory()
    session = await SessionManager(factory).connect(target_registration)
    head_marker = b"HEAD_MARKER\n"
    tail_marker = b"\nTAIL_MARKER\n"
    session.transport._files[PurePosixPath("/var/log/payment/app.log")] = (
        head_marker + b"x" * (20 * 1024 * 1024) + tail_marker
    )
    source = FileLogSource(SessionManager(factory))

    request = LogQueryRequest(
        project_id="payments",
        target_id="dev-a",
        service_name="payment-api",
        source_kind=LogSourceKind.FILE,
        scope=LogScope.HOST,
        source_ref="/var/log/payment/app.log",
        tail_lines=5,
        persist=False,
        create_evidence=False,
    )

    lines = await source.query(
        request, target_registration, PurePosixPath("/var/log/payment/app.log")
    )

    text = "\n".join(line.text for line in lines)
    assert "TAIL_MARKER" in text
    assert "HEAD_MARKER" not in text
