"""Adapter construction tests — no network access."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.transport import RemoteConnectionError

PATCH_TARGET = (
    "incidentlens_control_plane.remote_ops.asyncssh_adapter.asyncssh.connect"
)


@pytest.fixture
def target() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-a",
        host="dev-a.example.test",
        ssh_user="deploy",
        ssh_config_alias="dev-a",
    )


@pytest.fixture
def target_no_alias() -> TargetRegistration:
    return TargetRegistration(
        target_id="dev-b",
        host="dev-b.example.test",
        ssh_user="admin",
    )


def _make_mock_conn() -> Mock:
    """Create a mock SSH connection with sync close and async wait_closed."""
    conn = AsyncMock()
    conn.close = Mock()  # close() is sync in real AsyncSSH
    conn.wait_closed = AsyncMock()
    return conn


@pytest.mark.asyncio
async def test_connect_uses_ssh_config_alias_and_credentials(
    target: TargetRegistration,
) -> None:
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory()

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        transport = await factory.connect(target)

        mock_connect.assert_awaited_once_with(
            "dev-a",
            username="deploy",
            known_hosts=(),
            keepalive_interval=15,
            keepalive_count_max=3,
        )
        assert transport is not None


@pytest.mark.asyncio
async def test_connect_falls_back_to_host_when_no_alias(
    target_no_alias: TargetRegistration,
) -> None:
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory()

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        await factory.connect(target_no_alias)

        mock_connect.assert_awaited_once_with(
            "dev-b.example.test",
            username="admin",
            known_hosts=(),
            keepalive_interval=15,
            keepalive_count_max=3,
        )


@pytest.mark.asyncio
async def test_connect_maps_connection_error(
    target: TargetRegistration,
) -> None:
    import asyncssh

    factory = AsyncSshTransportFactory()

    with patch(
        PATCH_TARGET,
        new_callable=AsyncMock,
        side_effect=asyncssh.ConnectionLost("connection lost"),
    ):
        with pytest.raises(RemoteConnectionError):
            await factory.connect(target)


@pytest.mark.asyncio
async def test_connect_injects_test_key_and_port(
    target_no_alias: TargetRegistration,
) -> None:
    """Test-only ``client_key_paths`` and a registered port reach asyncssh."""
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    target = target_no_alias.model_copy(update={"port": 2222})
    factory = AsyncSshTransportFactory(client_key_paths=("/tmp/test-key",))

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        await factory.connect(target)

        mock_connect.assert_awaited_once_with(
            "dev-b.example.test",
            username="admin",
            port=2222,
            client_keys=["/tmp/test-key"],
            known_hosts=(),
            keepalive_interval=15,
            keepalive_count_max=3,
        )


@pytest.mark.asyncio
async def test_connect_injects_test_known_hosts_file(
    target_no_alias: TargetRegistration,
) -> None:
    """Test-only ``known_hosts_path`` reaches asyncssh for a disposable target."""
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory(known_hosts_path="/tmp/test-known-hosts")

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        await factory.connect(target_no_alias)

        mock_connect.assert_awaited_once_with(
            "dev-b.example.test",
            username="admin",
            known_hosts="/tmp/test-known-hosts",
            keepalive_interval=15,
            keepalive_count_max=3,
        )


@pytest.mark.asyncio
async def test_close_closes_sftp_and_connection(
    target: TargetRegistration,
) -> None:
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_sftp.exit = Mock()  # exit() is synchronous in real AsyncSSH
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory()

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        transport = await factory.connect(target)
        await transport.close()

        mock_sftp.exit.assert_called_once()
        mock_sftp.wait_closed.assert_awaited_once()
        mock_conn.close.assert_called_once()
        mock_conn.wait_closed.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_is_idempotent(target: TargetRegistration) -> None:
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_sftp.exit = Mock()  # exit() is synchronous in real AsyncSSH
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory()

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        transport = await factory.connect(target)
        await transport.close()
        await transport.close()

        assert mock_conn.close.call_count == 1
        assert mock_sftp.exit.call_count == 1
