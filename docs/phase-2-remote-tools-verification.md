# Phase 2: Remote Tools Verification

This document verifies Phase 2 (persistent, safe remote tools) against a
**disposable Docker SSH target** (`infra/test-ssh`). It describes how to run
the opt-in live acceptance test, how to drive the remote tools manually, where
backups are stored, how the approval flow works, and how to recover and clean
up.

> **WARNING — disposable target only.**
> The `infra/test-ssh` container runs a plain OpenSSH server with a random
> private key and a temporary workspace. It is rebuilt and destroyed by the
> test. **Do not** point these verification steps at a real production server
> the first time you run them. Production verification must use a non-critical
> server first, with a registered project whose `allowed_host_paths` are
> limited to the files you intend to inspect.

## Prerequisites

- Docker with Compose v2 (the `docker compose` subcommand).
- `ssh-keygen` on the host.
- The Python toolchain (`uv sync` already done).

The live acceptance test is **opt-in** and skips otherwise:

```bash
uv run pytest -q                                  # unit tests, live test skipped
INCIDENTLENS_RUN_LIVE_SSH=1 uv run pytest tests/integration/test_live_ssh_tools.py -q
```

When the variable is absent, or when Docker/`ssh-keygen`/the Docker daemon is
unavailable, the fixture skips with a clear reason. The private client key is
generated fresh under a `tmp_path` and is never committed; the compose project
is torn down with `docker compose down --volumes` in a finalizer.

## What the live test verifies

The fixture starts `infra/test-ssh`, captures the container's ephemeral host
key into a `known_hosts` file, injects a freshly generated ed25519 client key
via `AsyncSshTransportFactory(client_key_paths=..., known_hosts_path=...)`,
and waits until a real SSH connection succeeds. Eight acceptance points are
then verified against the live container:

1. Two `SessionManager.connect()` calls reuse one session ID.
2. A persistent shell `cd /workspace/service` changes the next `pwd`.
3. `read` / `list_dir` / `stat` / `search` return bounded results.
4. A large multi-location edit (3+ non-adjacent replacements) creates an
   encrypted local backup and a timestamped same-directory remote backup
   before replacing the file.
5. A stale expected SHA-256 blocks a second edit.
6. `rm -rf` is rejected without contacting the transport.
7. `docker restart` produces an approval request and is not executed before
   approval.
8. A forced validation failure leaves the original bytes untouched.

## Registering the target

The live fixture registers a target whose SSH user is `incidentlens` and whose
only allowed host root is `/workspace`. A production registration is JSON of
the same shape; the connection parameters (host, user, port, credentials,
allowed roots) are resolved **only** from the registry, never from the client.

```json
{
  "project_id": "payments",
  "display_name": "Payments",
  "targets": [
    {
      "target_id": "payments-prod",
      "host": "10.0.0.12",
      "ssh_user": "deploy",
      "port": 22,
      "compose_working_directory": "/srv/payments",
      "compose_project_name": "payments"
    }
  ],
  "services": [
    {
      "compose_service": "payment-api",
      "container_names": ["payments-api-1", "payments-api-2"],
      "allowed_host_paths": ["/srv/payments", "/var/log/payments"],
      "allowed_container_paths": ["/app"],
      "protected_remote_paths": ["/srv/payments/.env"]
    }
  ]
}
```

`allowed_host_paths` are the only files/directories the tools may read or
change. Paths are checked with lexical containment (`PurePosixPath`
`is_relative_to`), so `..` traversal and prefix collisions are rejected, and
symlinks are rejected. Writes to protected paths (`.env`, compose files,
systemd units, anything under `/etc`, and `protected_remote_paths`) require an
exact approval.

## Driving the remote tools

Connect through the HTTP API or directly through the Python API. The examples
below use the Python API against the disposable container.

```python
import asyncio
from pathlib import PurePosixPath

from incidentlens_control_plane.project_registry.types import TargetRegistration
from incidentlens_control_plane.remote_ops.asyncssh_adapter import AsyncSshTransportFactory
from incidentlens_control_plane.remote_ops.sessions import SessionManager
from incidentlens_control_plane.remote_ops.shell import PersistentShell

factory = AsyncSshTransportFactory(
    client_key_paths=("/tmp/id_ed25519",),
    known_hosts_path="/tmp/known_hosts",
)
target = TargetRegistration(
    target_id="live-ssh",
    host="127.0.0.1",
    ssh_user="incidentlens",
    port=49153,  # docker compose port test-ssh 22
)


async def demo() -> None:
    sessions = SessionManager(factory)
    session = await sessions.connect(target)      # one persistent connection

    # Persistent shell: cd survives across commands.
    shell = PersistentShell(await session.transport.open_shell())
    await shell.execute("cd /workspace/service", timeout=10)
    pwd = await shell.execute("pwd", timeout=10)
    assert pwd.stdout.rstrip() == b"/workspace/service"
    await shell.close()

    await sessions.disconnect(target.target_id)


asyncio.run(demo())
```

Read/list/stat/search go through `RemoteToolGateway`, which enforces the
service's allowed roots:

```python
result = await gateway.read(
    project_id="payments",
    target_id="payments-prod",
    service="payment-api",
    path=PurePosixPath("/srv/payments/app.py"),
)
print(result.sha256, result.truncated)
```

`edit` performs a backup-before-replace transaction. Every replacement must
match exactly; the expected SHA-256 is re-verified before the first rename:

```python
await gateway.edit(
    project_id="payments",
    target_id="payments-prod",
    service="payment-api",
    path=PurePosixPath("/srv/payments/app.py"),
    expected_sha256="<sha256 of the current remote file>",
    replacements=(
        TextReplacement(old_text="VERSION = \"1.0\"", new_text="VERSION = \"1.1\""),
        TextReplacement(old_text="timeout=30", new_text="timeout=60"),
    ),
)
```

## Backups

Two backups are created **before** any replacement, in this order:

1. **Encrypted local backup** — an AES-256-GCM blob under the ChangeManager's
   vault root (`<data-dir>/backups/<target_id>/<incident_id>/<changeset_id>/`)
   plus a `key.bin` key file. Each blob is authenticated with AAD carrying the
   remote path and plaintext SHA-256. Inspect the metadata for an applied
   changeset from the journal:

   ```bash
   sqlite3 <data-dir>/changes.db \
     "select local_backup_ref, remote_backup_path, replacement_sha256 \
      from file_changes where changeset_id = 'chs-...';"
   ```

   The `local_backup_ref` names the encrypted blob; it cannot be read without
   the vault key and decrypts only if the SHA-256 matches.

2. **Timestamped remote backup** — a copy of the original file placed in the
   same directory as the target, named
   `<file>.incidentlens-backup.<YYYYmmddTHHMMSS.ffffffZ>`. Example:
   `/srv/payments/app.py.incidentlens-backup.20260811T081234.123456Z`.

Rollback restores each applied file from its verified remote backup (or removes
a newly-created file), and renames it back into place. A rollback of a
service-interrupting change (protected path) requires an exact approval.

## Approval flow for Docker restart / rm

`docker restart`, `docker rm`, and compose `up`/`down` never execute directly.
They produce an exact, single-use approval:

1. Call `gateway.docker_action(DockerActionRequest(...))` without an
   `approval_id`. The gateway returns `approved=False` plus an `approval_id`
   and **does not contact the transport** — the container is untouched.
2. A human inspects the request (container name and action are resolved from
   the registration, never from the model) and approves it.
3. The action runs only after the approval is consumed, and only if the intent
   hash, TTL (15 minutes), and single-use status all match.

```python
result = await gateway.docker_action(request)        # approved=False, approval_id
await approvals.approve(result.approval_id)          # human decision
await gateway.docker_action(request, approval_id=result.approval_id)  # executes
```

The same gate protects `rm -rf`, which is **permanently forbidden**: the policy
classifies every recursive-plus-force `rm` variant as `FORBIDDEN`, and the
gateway rejects it before any approval or transport contact.

## Recovery and cleanup

- **Rollback an applied changeset**:
  `await gateway.restore(changeset_id=result.changeset_id)`.
- **Disconnect a target**: `await sessions.disconnect(target_id)`.
- **Tear down the disposable container**:

  ```bash
  docker compose -f infra/test-ssh/compose.yaml -p incidentlens-live down --volumes
  ```

- A failed multi-file change restores already-applied files in reverse order
  and keeps the changeset journal (status `ROLLED_BACK` or `FAILED`) as
  evidence.

## Phase 2 scope

Implements:

- Existing local SSH configuration, SSH agent/keys, and host-key verification.
- One persistent SSH connection with SFTP, command, and shell channels.
- Host and container Read, List, Search, Stat (bounded).
- Large multi-location Edit/Write with mandatory two-backup ordering.
- Persistent current-directory/shell state.
- Permanent recursive-force `rm` denial.
- Exact approval for Docker/service-impacting operations.
- Encrypted local plus timestamped remote backup.
- Stale-write detection, atomic replace, multi-file rollback.
- Durable audit events and local inspection APIs.

Does NOT implement:

- A general-purpose shell or SSH tool exposed to the model.
- Any server-side IncidentLens agent installed on remote hosts.
- Phase 3 agentic log investigation.
