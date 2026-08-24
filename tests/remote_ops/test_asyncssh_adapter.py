"""Adapter construction tests — no network access."""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest
from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.transport import (
    RemoteConnectionError,
    RemoteHostKeyError,
)

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
            keepalive_interval=15,
            keepalive_count_max=3,
        )
        assert "known_hosts" not in mock_connect.await_args.kwargs
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
            keepalive_interval=15,
            keepalive_count_max=3,
        )
        assert "known_hosts" not in mock_connect.await_args.kwargs


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
            keepalive_interval=15,
            keepalive_count_max=3,
        )
        assert "known_hosts" not in mock_connect.await_args.kwargs


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
async def test_default_connection_uses_asyncssh_known_hosts_resolution(
    target: TargetRegistration,
) -> None:
    """Without ``known_hosts_path``, AsyncSSH resolves default known-hosts files."""
    mock_conn = _make_mock_conn()
    mock_sftp = AsyncMock()
    mock_conn.start_sftp_client = AsyncMock(return_value=mock_sftp)

    factory = AsyncSshTransportFactory()

    with patch(PATCH_TARGET, new_callable=AsyncMock) as mock_connect:
        mock_connect.return_value = mock_conn
        await factory.connect(target)

    assert "known_hosts" not in mock_connect.await_args.kwargs
    assert mock_connect.await_args.kwargs.get("known_hosts") != ()


def test_known_hosts_path_must_be_absolute_and_non_empty() -> None:
    """A configured ``known_hosts_path`` must be a non-empty absolute path."""
    with pytest.raises(ValueError, match="absolute"):
        AsyncSshTransportFactory(known_hosts_path="relative/known_hosts")
    with pytest.raises(ValueError, match="non-empty"):
        AsyncSshTransportFactory(known_hosts_path="  ")
    with pytest.raises(ValueError, match="non-empty"):
        AsyncSshTransportFactory(known_hosts_path="")


@pytest.mark.asyncio
async def test_host_key_failure_maps_to_remote_host_key_error(
    target: TargetRegistration,
) -> None:
    """A host-key verification failure surfaces as ``RemoteHostKeyError``.

    ``asyncssh`` raises ``HostKeyNotVerifiable`` when the server host key is
    not trusted.  The adapter must translate it to the domain error and must
    not leak any raw key material (public key blob/fingerprint) into the
    message.
    """
    import asyncssh

    key_text = (
        asyncssh.generate_private_key("ssh-ed25519").export_public_key().decode()
    )
    factory = AsyncSshTransportFactory()

    with patch(
        PATCH_TARGET,
        new_callable=AsyncMock,
        side_effect=asyncssh.HostKeyNotVerifiable(
            f"Host key is not trusted for host {target.host}"
        ),
    ):
        with pytest.raises(RemoteHostKeyError) as exc_info:
            await factory.connect(target)

    message = str(exc_info.value)
    assert "Host key is not trusted" in message
    assert "ssh-ed25519" not in message
    assert key_text not in message


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
