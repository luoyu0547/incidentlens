# IncidentLens Backend Product API Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the authenticated, durable, versioned FastAPI product contract required by the React Ink CLI and the read-only Web log observability workspace without replacing the existing investigation core.

**Architecture:** Keep ProjectRegistry, Investigation/AgentRun, Log, Evidence, Approval, ChangeSet, Recovery, and the current `/api/*` routes authoritative. Add an incremental `/api/v1` facade, durable Operations, Agent Sessions, indexed/replayable streams, product log cursors, and read-only Web projections. Existing routes remain mounted until real CLI/Web acceptance proves migration complete.

**Tech Stack:** Python 3.12, FastAPI, Pydantic 2, SQLite, AsyncSSH, pytest, pytest-asyncio, Ruff

**Spec:** `docs/superpowers/specs/2026-08-24-cloud-agent-cli-web-observability-design.md`

## Global Constraints

- Python remains `>=3.12,<3.13`; FastAPI remains `>=0.115,<1`; Pydantic remains `>=2.13,<3`.
- Preserve all existing `/api/*` routes and response bodies throughout this plan.
- All new product HTTP routes live under `/api/v1`; CLI and log streams live under `/ws/v1`.
- Every new Pydantic contract uses `ConfigDict(extra="forbid", frozen=True)`.
- Every v1 endpoint declares an explicit `response_model`, stable `operation_id`, and documented `ApiErrorResponse` cases.
- All v1 mutations derive actor identity from an authenticated `Principal`; request bodies never accept `created_by` or actor identity.
- All v1 mutations require `Idempotency-Key`; cookie-authenticated mutations additionally require CSRF.
- No response exposes credentials, authentication references, canonical approval intent/hash, raw provider payload, hidden reasoning, unredacted logs, backup plaintext, or transport objects.
- SQLite and the in-memory broker remain single-worker only in this phase.
- Migrations are additive, idempotent, and compatible with an existing `runtime.db`; do not introduce Alembic in this plan.
- A dangerous in-flight operation with uncertain outcome is never automatically replayed.
- Commit steps below are execution-time steps only. Do not combine unrelated user changes already present in the working tree.

---

### Task 1: Restore SSH Host Identity Verification

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/asyncssh_adapter.py:261-300`
- Modify: `apps/control-plane/src/incidentlens_control_plane/remote_ops/transport.py`
- Test: `tests/remote_ops/test_asyncssh_adapter.py`
- Test: `tests/integration/test_live_ssh_tools.py`

**Interfaces:**
- Consumes: `AsyncSshTransportFactory(client_key_paths=None, known_hosts_path=None)` and `TargetRegistration`.
- Produces:

```python
class HostKeyPolicy(StrEnum):
    STRICT = "strict"
    PINNED = "pinned"

class HostKeyVerification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy: HostKeyPolicy
    verified: bool
    known_hosts_source: str
    fingerprint_sha256: str | None = None

class RemoteHostKeyError(RemoteConnectionError):
    pass
```

- [ ] **Step 1: Replace the insecure expectation with a failing test**

```python
@pytest.mark.asyncio
async def test_default_connection_uses_asyncssh_known_hosts_resolution(target):
    with patch(ASYNCSSH_CONNECT, new_callable=AsyncMock) as connect:
        connect.return_value = _connection_with_sftp()
        await AsyncSshTransportFactory().connect(target)

    assert "known_hosts" not in connect.await_args.kwargs
    assert connect.await_args.kwargs.get("known_hosts") != ()
```

Add cases proving an explicit absolute path is passed and a host-key verification failure becomes `RemoteHostKeyError` without raw key material.

- [ ] **Step 2: Run the focused test and verify the insecure default fails**

Run: `uv run pytest tests/remote_ops/test_asyncssh_adapter.py -q`

Expected: FAIL because the current adapter passes `known_hosts=()`.

- [ ] **Step 3: Implement strict default verification**

Remove unconditional `known_hosts=()`. Omit the argument when `known_hosts_path is None`, allowing AsyncSSH to resolve the user's normal known-hosts files. When configured, require an absolute non-empty path and pass it explicitly. Map AsyncSSH host-key exceptions separately from network failures. Do not add TOFU or accept-any modes.

- [ ] **Step 4: Update disposable live fixtures**

Make live test fixtures create and explicitly pass the disposable target's known-hosts file. Keep key and port injection behavior intact.

- [ ] **Step 5: Verify SSH regression coverage**

Run:

```bash
uv run pytest tests/remote_ops/test_asyncssh_adapter.py -q
uv run pytest tests/integration/test_live_ssh_tools.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops
```

Expected: PASS; live tests may remain opt-in/skipped when their environment flag is absent.

- [ ] **Step 6: Commit the safety fix**

```bash
git add apps/control-plane/src/incidentlens_control_plane/remote_ops tests/remote_ops/test_asyncssh_adapter.py tests/integration/test_live_ssh_tools.py
git commit -m "fix(remote): require SSH host identity verification"
```

---

### Task 2: Add `/api/v1`, Request IDs, and Stable Errors

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/api/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/models.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/errors.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/request_id.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/router.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Test: `tests/api_v1/test_errors.py`
- Test: `tests/api_v1/test_versioning.py`

**Interfaces:**
- Consumes: `create_app(settings, transport_factory)`.
- Produces:

```python
JsonValue = str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"]

class ApiError(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)
    request_id: str

class ApiErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    error: ApiError

class ApiVersionView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    api_version: Literal["v1"] = "v1"
    stream_schema_versions: tuple[Literal[1], ...] = (1,)
    minimum_cli_protocol_version: str = "1.0.0"
    minimum_web_protocol_version: str = "1.0.0"

class ApiProblem(Exception):
    status_code: int
    code: str
    message: str
    details: dict[str, JsonValue]
```

Route: `GET /api/v1/version`, operation ID `getApiVersion`.

- [ ] **Step 1: Write failing version and validation-envelope tests**

```python
def test_v1_version_has_stable_contract(client):
    response = client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "stream_schema_versions": [1],
        "minimum_cli_protocol_version": "1.0.0",
        "minimum_web_protocol_version": "1.0.0",
    }
    assert response.headers["X-Request-ID"].startswith("req_")


def test_v1_validation_error_has_stable_envelope(client):
    response = client.get("/api/v1/version?unknown=x")
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "request_validation_failed"
```

Also test accepted inbound request IDs against `^[A-Za-z0-9._-]{1,80}$`, generated replacements for invalid IDs, and a redacted 500 body.

- [ ] **Step 2: Verify the v1 contract is absent**

Run: `uv run pytest tests/api_v1/test_errors.py tests/api_v1/test_versioning.py -q`

Expected: FAIL with 404 and legacy validation bodies.

- [ ] **Step 3: Implement request-ID middleware and exception handlers**

Store the request ID in `request.state.request_id`, return it as `X-Request-ID`, and normalize `ApiProblem`, `RequestValidationError`, v1 `HTTPException`, and uncaught exceptions. Never serialize exception text for 500 errors.

Stable codes introduced here:

```text
request_validation_failed authentication_required permission_denied
resource_not_found resource_conflict idempotency_key_required
idempotency_conflict idempotency_in_progress target_unreachable
host_key_verification_failed operation_not_cancellable cursor_invalid
approval_expired approval_already_decided approval_already_consumed
downstream_processing_failed internal_error
```

- [ ] **Step 4: Mount v1 without changing legacy routes**

Create `APIRouter(prefix="/api/v1")`, add the version route, then include it in `main.py` while leaving every existing router mounted. Add `expose_api_docs: bool = False`; when false use `docs_url=None`, `redoc_url=None`, and `openapi_url=None`, while retaining `application.openapi()` for offline export.

- [ ] **Step 5: Verify v1 and legacy compatibility**

Run:

```bash
uv run pytest tests/api_v1/test_errors.py tests/api_v1/test_versioning.py -q
uv run pytest tests/test_app.py tests/web -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/api apps/control-plane/src/incidentlens_control_plane/main.py
```

- [ ] **Step 6: Commit the API foundation**

```bash
git add apps/control-plane/src/incidentlens_control_plane/api apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/config.py tests/api_v1
git commit -m "feat(api): establish versioned product contract"
```

---

### Task 3: Add Authenticated Principals and CSRF-Safe Sessions

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/auth/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/auth/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/auth/dependencies.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/auth.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `tests/web/conftest.py`
- Test: `tests/auth/test_service.py`
- Test: `tests/api_v1/test_auth.py`

**Interfaces:**

```python
class PrincipalScope(StrEnum):
    READ = "read"
    OPERATE = "operate"
    APPROVE = "approve"
    ADMIN = "admin"

class AuthenticationMethod(StrEnum):
    BEARER = "bearer"
    SESSION_COOKIE = "session_cookie"

class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    principal_id: str
    display_name: str
    scopes: frozenset[PrincipalScope]
    allowed_target_ids: frozenset[str] | None = None
    authentication_method: AuthenticationMethod

async def get_principal(request: Request) -> Principal: ...
def require_scopes(*scopes: PrincipalScope) -> Callable[..., Principal]: ...
def authorize_target(principal: Principal, target_id: str) -> None: ...
```

Routes: `POST /api/v1/auth/session`, `GET /api/v1/principal`, `POST /api/v1/auth/logout`.

- [ ] **Step 1: Write authentication and impersonation tests**

Test missing/invalid bearer tokens, cookie GET without CSRF, cookie mutation rejection without `X-CSRF-Token`, bearer mutation without CSRF, target restrictions, and rejection of request-body actor fields.

```python
def test_body_actor_cannot_impersonate_principal(authenticated_client):
    response = authenticated_client.post(
        "/api/v1/auth/logout",
        headers={"X-CSRF-Token": authenticated_client.csrf},
        json={"created_by": "admin"},
    )
    assert response.status_code == 422
```

- [ ] **Step 2: Run tests and observe missing authentication**

Run: `uv run pytest tests/auth tests/api_v1/test_auth.py -q`

Expected: FAIL because v1 accepts unauthenticated requests and has no principal model.

- [ ] **Step 3: Implement static deployment profiles and bearer auth**

Add `INCIDENTLENS_AUTH_PROFILES_JSON`, containing SHA-256 token digests rather than plaintext. Compare token digests with `hmac.compare_digest`. Add settings for a `SecretStr` session signing key, TTL, trusted hosts, and secure-cookie behavior.

- [ ] **Step 4: Implement signed browser sessions and CSRF**

Sign a payload containing principal ID, issued/expiry times, and a CSRF nonce with HMAC-SHA256. Set `incidentlens_session` as HttpOnly, Secure, SameSite=Strict, Path=/. Require the matching CSRF header for cookie-authenticated mutations. Bearer clients do not use CSRF.

- [ ] **Step 5: Apply auth to v1 HTTP and future stream prefixes**

Protect `/api/v1/*`, `/ws/v1/*`, and `/events/v1/*`, exempting `/healthz` and session creation. Add `TrustedHostMiddleware`. Test fixtures must configure explicit profiles; do not add a TestClient-only bypass.

Keep legacy `/api/*` enabled temporarily behind `legacy_api_enabled=True` and emit a startup warning while enabled.

- [ ] **Step 6: Verify auth behavior**

Run:

```bash
uv run pytest tests/auth tests/api_v1/test_auth.py -q
uv run pytest tests/test_app.py tests/web -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/auth apps/control-plane/src/incidentlens_control_plane/api/routes/auth.py
```

- [ ] **Step 7: Commit authentication**

```bash
git add apps/control-plane/src/incidentlens_control_plane/auth apps/control-plane/src/incidentlens_control_plane/api/routes/auth.py apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py tests/auth tests/api_v1/test_auth.py tests/web/conftest.py
git commit -m "feat(auth): authenticate product API principals"
```

---

### Task 4: Persist Mutation Idempotency

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/idempotency/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/idempotency/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/idempotency/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/idempotency.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/idempotency/test_store.py`
- Test: `tests/api_v1/test_idempotency.py`

**Interfaces:**

```python
async def execute_idempotent[T: BaseModel](
    *,
    service: IdempotencyService,
    principal: Principal,
    method: str,
    route_key: str,
    idempotency_key: str,
    request_sha256: str,
    response_type: type[T],
    action: Callable[[], Awaitable[tuple[int, T]]],
) -> tuple[int, T, bool]: ...
```

Migration:

```sql
CREATE TABLE IF NOT EXISTS api_idempotency_keys (
    principal_id TEXT NOT NULL,
    method TEXT NOT NULL,
    route_key TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('in_progress', 'completed')),
    status_code INTEGER,
    response_json TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    expires_at TEXT NOT NULL,
    PRIMARY KEY (principal_id, method, route_key, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_api_idempotency_expiry
    ON api_idempotency_keys(expires_at);
```

- [ ] **Step 1: Write collision, replay, and in-progress tests**

```python
def test_same_key_different_request_is_conflict(authenticated_client):
    headers = {"Idempotency-Key": "target-create-1"}
    first = authenticated_client.post("/api/v1/test-idempotent", headers=headers, json={"value": "a"})
    second = authenticated_client.post("/api/v1/test-idempotent", headers=headers, json={"value": "b"})
    assert first.status_code == 201
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_conflict"
```

Test exact status/body replay and `Idempotency-Replayed: true`.

- [ ] **Step 2: Verify tests fail**

Run: `uv run pytest tests/idempotency tests/api_v1/test_idempotency.py -q`

- [ ] **Step 3: Add idempotent migration and atomic reservation**

Accept keys matching `^[A-Za-z0-9._:-]{1,200}$`. Hash method, stable route key, path parameters, and canonical body only; exclude auth, cookies, CSRF, and request ID. Use a transaction to reserve.

- [ ] **Step 4: Implement replay semantics**

Matching completed rows replay exact 2xx response/status. Mismatched hashes return `idempotency_conflict`. Active reservations return `idempotency_in_progress` with `Retry-After: 1`. Retain completed records for 24 hours. Leave unexpected 5xx reservations active for 60 seconds before expiry cleanup.

- [ ] **Step 5: Verify storage and API behavior**

Run:

```bash
uv run pytest tests/idempotency tests/api_v1/test_idempotency.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/idempotency apps/control-plane/src/incidentlens_control_plane/api/idempotency.py
```

- [ ] **Step 6: Commit idempotency**

```bash
git add apps/control-plane/src/incidentlens_control_plane/idempotency apps/control-plane/src/incidentlens_control_plane/api/idempotency.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/idempotency tests/api_v1/test_idempotency.py
git commit -m "feat(api): persist mutation idempotency"
```

---

### Task 5: Add the Target Product Facade

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/targets/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/targets/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/targets/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/targets.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/project_registry/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/targets/test_service.py`
- Test: `tests/api_v1/test_targets.py`

**Interfaces:**

```python
class TargetCreate(BaseModel):
    name: str
    host: str
    ssh_user: str
    ssh_port: int = 22
    authentication_ref: str
    host_key_policy: Literal["strict", "pinned"] = "strict"
    pinned_host_key_sha256: str | None = None
    optional_source_path: Path | None = None

class TargetPatch(BaseModel):
    name: str | None = None
    host: str | None = None
    ssh_user: str | None = None
    ssh_port: int | None = None
    authentication_ref: str | None = None
    host_key_policy: Literal["strict", "pinned"] | None = None
    pinned_host_key_sha256: str | None = None
    optional_source_path: Path | None = None
    expected_version: int

class TargetView(BaseModel):
    target_id: str
    name: str
    host: str
    ssh_user: str
    ssh_port: int
    authentication_configured: bool
    authentication_hint: str
    host_key_policy: Literal["strict", "pinned"]
    pinned_host_key_sha256: str | None
    optional_source_path: Path | None
    version: int
    created_at: datetime
    updated_at: datetime
```

Routes: create/list/get/patch/delete targets and `GET /api/v1/targets/{id}/services`.

Migration:

```sql
CREATE TABLE IF NOT EXISTS target_facade_bindings (
    target_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    registry_target_id TEXT NOT NULL,
    name TEXT NOT NULL,
    authentication_ref TEXT NOT NULL,
    host_key_policy TEXT NOT NULL CHECK (host_key_policy IN ('strict', 'pinned')),
    pinned_host_key_sha256 TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (project_id, registry_target_id)
);
```

- [ ] **Step 1: Write facade secrecy and preservation tests**

Test that responses never contain full `authentication_ref`, existing registry targets receive stable bindings, PATCH preserves services/allowed/protected paths, stale versions conflict, duplicate internal target IDs do not alias, and active investigations block deletion.

- [ ] **Step 2: Verify target facade is absent**

Run: `uv run pytest tests/targets tests/api_v1/test_targets.py -q`

- [ ] **Step 3: Implement additive target bindings**

Keep `projects.record_json` authoritative for host, user, port, services, and scope. Use the new table only for product identity, auth reference, host-key policy, safe display metadata, and facade version.

For existing targets, retain the registry target ID when globally unique; otherwise use `tgt_` plus the first 24 SHA-256 hex characters of `project_id + "\0" + registry_target_id`. New product targets create an internal project with no services. Service discovery still expands access only through existing Registry Proposals.

- [ ] **Step 4: Implement safe CRUD routes**

Derive auth and actor from Principal, require idempotency on mutations, use optimistic version checks, and return only an abbreviated auth hint. Resolve every service/scope request back through ProjectRegistry.

- [ ] **Step 5: Verify legacy registry behavior**

Run:

```bash
uv run pytest tests/targets tests/api_v1/test_targets.py -q
uv run pytest tests/project_registry tests/web/test_projects_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/targets apps/control-plane/src/incidentlens_control_plane/api/routes/targets.py
```

- [ ] **Step 6: Commit the facade**

```bash
git add apps/control-plane/src/incidentlens_control_plane/targets apps/control-plane/src/incidentlens_control_plane/api/routes/targets.py apps/control-plane/src/incidentlens_control_plane/project_registry/store.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/targets tests/api_v1/test_targets.py
git commit -m "feat(targets): add product target facade"
```

---

### Task 6: Persist the Operation State Machine

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/state_machine.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/operations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/operations/test_store.py`
- Test: `tests/operations/test_state_machine.py`
- Test: `tests/api_v1/test_operations.py`

**Interfaces:**

```python
class OperationKind(StrEnum):
    AGENT_MESSAGE = "agent_message"
    TARGET_TEST = "target_test"
    INVESTIGATION_START = "investigation_start"
    ROLLBACK = "rollback"
    REPORT_GENERATE = "report_generate"

class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCEL_REQUESTED = "cancel_requested"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNCERTAIN = "uncertain"
```

Migration creates `operations` and `operation_attempts`, indexed by claim status, session, and investigation. `OperationView` omits private `request_payload`.

- [ ] **Step 1: Write state-machine, claim, and authorization tests**

Test legal transitions, immutable terminal states, atomic one-worker claim, idempotent cancellation, private payload omission, owner/target authorization, and safe bounded errors.

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/operations tests/api_v1/test_operations.py -q`

Expected: FAIL because no operation resource exists.

- [ ] **Step 3: Implement migration, models, and state machine**

Transitions:

```text
queued -> running | cancel_requested | cancelled
running -> succeeded | failed | cancel_requested | uncertain
cancel_requested -> cancelled | failed | uncertain
terminal -> no transition
```

Claim with a conditional update in one SQLite transaction. Store only validated redacted payloads. Bound error messages to 2,000 redacted characters.

- [ ] **Step 4: Implement query and cancellation endpoints**

Add `GET /api/v1/operations/{id}` and idempotent `POST /api/v1/operations/{id}/cancel`. Emit durable `operation.*` events containing IDs/status/safe summaries only.

- [ ] **Step 5: Verify operation behavior**

Run:

```bash
uv run pytest tests/operations tests/api_v1/test_operations.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/operations apps/control-plane/src/incidentlens_control_plane/api/routes/operations.py
```

- [ ] **Step 6: Commit operations**

```bash
git add apps/control-plane/src/incidentlens_control_plane/operations apps/control-plane/src/incidentlens_control_plane/api/routes/operations.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/operations tests/api_v1/test_operations.py
git commit -m "feat(operations): persist long-running work"
```

---

### Task 7: Dispatch and Recover Durable Operations

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/dispatcher.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/recovery.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/operations/handlers.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/changes.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/api/routes/targets.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/changes.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/operations/test_dispatcher.py`
- Test: `tests/operations/test_recovery.py`
- Test: `tests/api_v1/test_target_test_operation.py`
- Test: `tests/api_v1/test_rollback_operation.py`

**Interfaces:**

```python
class OperationDispatcher:
    async def start(self) -> None: ...
    async def stop(self, *, grace_seconds: float) -> None: ...
    def register(self, kind: OperationKind, handler: OperationHandler) -> None: ...

class OperationRecovery:
    async def recover(self, *, now: datetime) -> OperationRecoverySummary: ...
```

Routes `POST /api/v1/targets/{id}/test` and `POST /api/v1/changesets/{id}/rollback` return `202 OperationAccepted`.

- [ ] **Step 1: Write recovery-no-replay tests**

```python
def test_running_rollback_becomes_uncertain_after_restart(runtime_factory):
    first = runtime_factory()
    operation = first.operations.enqueue(kind=OperationKind.ROLLBACK, ...)
    first.operation_store.transition(operation.operation_id, OperationStatus.RUNNING, now=NOW)

    second = runtime_factory()
    asyncio.run(second.operation_recovery.recover(now=LATER))

    assert second.operation_store.get(operation.operation_id).status == OperationStatus.UNCERTAIN
    assert second.changes.rollback_calls == []
```

Also test read-only target tests requeue, queued work survives, agent work reconciles from linked run, and report work requeues only without a durable result.

- [ ] **Step 2: Run tests and confirm missing dispatcher**

Run: `uv run pytest tests/operations/test_dispatcher.py tests/operations/test_recovery.py -q`

- [ ] **Step 3: Implement dispatcher lifecycle and handlers**

Start recovery before dispatcher. Serialize by session ID; allow independent target tests. Heartbeat every 10 seconds and classify older-than-30-second running rows on startup. Stop dispatcher before Investigation shutdown closes sessions.

- [ ] **Step 4: Replace critical BackgroundTasks**

Make v1 target test and rollback enqueue durable Operations. Change legacy rollback to enqueue the same operation while preserving its current 202 JSON body. Remove `_safe_rollback` only after the legacy route test proves compatibility.

- [ ] **Step 5: Verify recovery and legacy behavior**

Run:

```bash
uv run pytest tests/operations tests/api_v1/test_target_test_operation.py tests/api_v1/test_rollback_operation.py -q
uv run pytest tests/web/test_changes_api.py tests/investigation/test_recovery.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/operations apps/control-plane/src/incidentlens_control_plane/routes/changes.py
```

- [ ] **Step 6: Commit operation execution**

```bash
git add apps/control-plane/src/incidentlens_control_plane/operations apps/control-plane/src/incidentlens_control_plane/api/routes/targets.py apps/control-plane/src/incidentlens_control_plane/api/routes/changes.py apps/control-plane/src/incidentlens_control_plane/routes/changes.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/operations tests/api_v1/test_target_test_operation.py tests/api_v1/test_rollback_operation.py
git commit -m "feat(operations): dispatch and recover durable work"
```

---

### Task 8: Add Agent Session and Message Facades

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/agent_sessions/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent_sessions/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/agent_sessions/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/agent_sessions.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/operations/handlers.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/agent_sessions/test_store.py`
- Test: `tests/agent_sessions/test_service.py`
- Test: `tests/api_v1/test_agent_sessions.py`

**Interfaces:**

```python
class AgentSessionStatus(StrEnum):
    IDLE = "idle"
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
    FAILED = "failed"
    COMPLETED = "completed"

class AgentSessionCreate(BaseModel):
    target_id: str
    title: str | None = None
    service_id: str | None = None

class AgentMessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)

class AgentMessageAccepted(BaseModel):
    message_id: str
    operation_id: str
    accepted: Literal[True] = True
```

Migration creates `agent_sessions` and `agent_messages`, indexed by owner/update, target/update, and session/message ordering.

- [ ] **Step 1: Write fast-acceptance and facade-authority tests**

```python
def test_message_returns_202_before_agent_completes(authenticated_client, session_id):
    response = authenticated_client.post(
        f"/api/v1/agent-sessions/{session_id}/messages",
        headers={"Idempotency-Key": "message-1"},
        json={"content": "调查 payment-service 频繁重启"},
    )
    assert response.status_code == 202
    assert response.json()["accepted"] is True
    assert response.json()["operation_id"].startswith("op_")
```

Test first-message Investigation creation, follow-up reuse, terminal Investigation rollover, cancellation, resume, message pagination, and no raw tool/provider content in messages.

- [ ] **Step 2: Run tests and confirm the long-request gap**

Run: `uv run pytest tests/agent_sessions tests/api_v1/test_agent_sessions.py -q`

- [ ] **Step 3: Add session/message migrations and mappings**

Session is a facade, not a second Agent state machine. Map Investigation status into Session status. First message creates an Investigation and queues `AGENT_MESSAGE`; follow-ups append user transcript and queue resume work. If active Investigation is terminal, atomically bind a new one.

- [ ] **Step 4: Project assistant transcript safely**

Create deterministic message IDs from run ID and transcript sequence. Only redacted assistant text becomes message content. Tool arguments/results remain safe tool events. Emit `agent.text.delta` and `agent.message.completed` after durable transcript writes; never expose hidden reasoning.

- [ ] **Step 5: Add session routes**

Create/list/get/patch sessions, send/list messages, cancel, and resume. Only explicit cancel calls cancellation. Legacy Investigation routes remain mounted but new clients do not use long `start`.

- [ ] **Step 6: Verify session and Investigation regressions**

Run:

```bash
uv run pytest tests/agent_sessions tests/api_v1/test_agent_sessions.py -q
uv run pytest tests/investigation/test_orchestrator.py tests/investigation/test_transcript.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/agent_sessions apps/control-plane/src/incidentlens_control_plane/api/routes/agent_sessions.py
```

- [ ] **Step 7: Commit sessions**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent_sessions apps/control-plane/src/incidentlens_control_plane/api/routes/agent_sessions.py apps/control-plane/src/incidentlens_control_plane/operations/handlers.py apps/control-plane/src/incidentlens_control_plane/investigation/orchestrator.py apps/control-plane/src/incidentlens_control_plane/investigation/service.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/agent_sessions tests/api_v1/test_agent_sessions.py
git commit -m "feat(agent): add durable interactive sessions"
```

---

### Task 9: Index and Page Durable Product Events

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/events.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/operations/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/streams/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/events.py`
- Test: `tests/events/test_store.py`
- Test: `tests/api_v1/test_event_history.py`

**Interfaces:**

```python
class StreamEventEnvelope(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    sequence: int
    event_type: str
    session_id: str | None = None
    target_id: str | None = None
    investigation_id: str | None = None
    occurred_at: datetime
    payload: dict[str, JsonValue]

class EventPage(BaseModel):
    items: tuple[StreamEventEnvelope, ...]
    next_after_sequence: int
    has_more: bool
    latest_sequence: int
    earliest_available_sequence: int
```

Migration adds schema/session/target/investigation columns and indexes to `runtime_events` without renumbering events.

- [ ] **Step 1: Write a 1,501-event pagination test**

Fetch in 500-row pages and assert all 1,501 sequences are returned with no fixed-1,000 gap. Test AND filters, repeated event types as IN, unauthorized targets, and unknown stored event types.

- [ ] **Step 2: Verify current store fails full pagination**

Run: `uv run pytest tests/events/test_store.py tests/api_v1/test_event_history.py -q`

- [ ] **Step 3: Add event dimensions and indexes**

Backfill dimensions from safe payload IDs where deterministically available; leave unknown dimensions null. Require new publishers to provide dimensions directly. Preserve global sequence as the cursor.

- [ ] **Step 4: Implement filtered HTTP pages**

Add `GET /api/v1/events` with `after_sequence`, max 500, session/target/investigation/type filters, `limit + 1` has-more logic, stable empty cursors, and principal target filtering in SQL.

- [ ] **Step 5: Verify legacy event API**

Run:

```bash
uv run pytest tests/events tests/api_v1/test_event_history.py -q
uv run pytest tests/web/test_events_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/streams
```

- [ ] **Step 6: Commit indexed events**

```bash
git add apps/control-plane/src/incidentlens_control_plane/events apps/control-plane/src/incidentlens_control_plane/streams/types.py apps/control-plane/src/incidentlens_control_plane/api/routes/events.py apps/control-plane/src/incidentlens_control_plane/investigation/events.py apps/control-plane/src/incidentlens_control_plane/operations/service.py tests/events tests/api_v1/test_event_history.py
git commit -m "feat(events): add durable filtered event pages"
```

---

### Task 10: Add the Recoverable CLI Event WebSocket

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/streams/cli.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/ws/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/ws/cli_events.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/events/broker.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Test: `tests/streams/test_cli_stream.py`
- Test: `tests/api_v1/test_cli_websocket.py`

**Interfaces:**

```text
WS /ws/v1/cli-events?schema_version=1&after_sequence=1842
  &session_id=&target_id=&investigation_id=&event_type=
```

Control events: `stream.hello`, `stream.heartbeat`, `stream.gap`, `stream.slow_consumer`, all using the common envelope.

- [ ] **Step 1: Write full replay and overflow tests**

Test authenticated handshake, unsupported schema close 4406, 1,501-event replay before live, replay/live overlap dedupe, heartbeat, broker overflow, slow outbound client, unknown event types, and disconnect-without-cancel.

- [ ] **Step 2: Verify old WS behavior is insufficient**

Run: `uv run pytest tests/streams/test_cli_stream.py tests/api_v1/test_cli_websocket.py -q`

- [ ] **Step 3: Implement race-free replay-to-live transition**

Subscribe to broker before capturing the high-water sequence. Replay 500-row pages to that high water. In live mode, fill sequence gaps from durable storage. If unavailable, send `stream.gap` and close 1012.

- [ ] **Step 4: Make backpressure explicit**

Replace silent oldest-item deletion with per-subscriber overflow metadata. Bound outbound queues to 256 frames. On overflow, send `stream.slow_consumer` when possible and close 1013. Send heartbeats after 15 idle seconds. Do not retain unbounded dedupe sets.

- [ ] **Step 5: Verify new and old streams**

Run:

```bash
uv run pytest tests/streams/test_cli_stream.py tests/api_v1/test_cli_websocket.py -q
uv run pytest tests/events/test_broker.py tests/web/test_events_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/streams apps/control-plane/src/incidentlens_control_plane/api/ws
```

- [ ] **Step 6: Commit CLI streaming**

```bash
git add apps/control-plane/src/incidentlens_control_plane/streams/cli.py apps/control-plane/src/incidentlens_control_plane/api/ws apps/control-plane/src/incidentlens_control_plane/events/broker.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/config.py tests/streams tests/api_v1/test_cli_websocket.py
git commit -m "feat(streams): add recoverable CLI event stream"
```

---

### Task 11: Add Opaque Product Log Cursors and HTTP History

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/cursors.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/logs/views.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/service_logs.py`
- Test: `tests/logs/test_product_cursor.py`
- Test: `tests/api_v1/test_service_logs.py`

**Interfaces:**

```python
class LogPage(BaseModel):
    items: tuple[LogRecordView, ...]
    next_cursor: str | None
    previous_cursor: str | None
    has_more: bool
    snapshot_cursor: str | None

def encode_log_cursor(sequence: int) -> str: ...
def decode_log_cursor(cursor: str) -> int: ...
```

Migration adds unique `stream_sequence` to `log_records`, backfilled from rowid, plus service/sequence and service/time indexes. Source cursor remains untouched.

- [ ] **Step 1: Write opaque cursor and snapshot tests**

Test invalid cursor rejection (never restart at first row), mutual exclusion of before/after, multi-page stable snapshot despite new inserts, service authorization, severity/container filters, and preservation of existing source cursor behavior.

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/logs/test_product_cursor.py tests/api_v1/test_service_logs.py -q`

- [ ] **Step 3: Implement stream-sequence allocation and cursor codec**

Allocate sequence in the existing log write transaction. Encode as opaque `lc1_` plus URL-safe base64 integer. Do not expose numeric semantics to clients.

- [ ] **Step 4: Implement historical log pages**

Add `GET /api/v1/services/{service_id}/logs`, default limit 200, max 500. Resolve service through Target facade. Use `snapshot_cursor` as upper bound. Return redacted message as public `message`; initially return `{}` for structured fields unless already persisted and redacted.

- [ ] **Step 5: Verify legacy logs**

Run:

```bash
uv run pytest tests/logs/test_product_cursor.py tests/api_v1/test_service_logs.py -q
uv run pytest tests/logs/test_store.py tests/web/test_logs_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/api/routes/service_logs.py
```

- [ ] **Step 6: Commit log history**

```bash
git add apps/control-plane/src/incidentlens_control_plane/logs apps/control-plane/src/incidentlens_control_plane/api/routes/service_logs.py tests/logs/test_product_cursor.py tests/api_v1/test_service_logs.py
git commit -m "feat(logs): add stable product log cursors"
```

---

### Task 12: Add the Cursor-Based Log WebSocket

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/streams/logs.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/ws/logs.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Test: `tests/streams/test_log_stream.py`
- Test: `tests/api_v1/test_log_websocket.py`

**Interfaces:**

Client actions: `subscribe`, `update`, `pause`, `resume`, `ack`. Server event types: `log.subscribed`, `log.record`, `stream.heartbeat`, `stream.gap`, `stream.slow_consumer`, all in a `schema_version: 1` envelope.

Route: `WS /ws/v1/logs`.

- [ ] **Step 1: Write cursor backfill and backpressure tests**

Test first-frame timeout, auth, subscribe after cursor, paginated backlog, live transition, no `seen_dedupe_keys`, pause/resume, filter update, heartbeat, invalid cursor gap, max 500 unacked records, max 256 outbound frames, and disconnect not pausing/deleting durable subscriptions.

- [ ] **Step 2: Verify current WS replays from the beginning**

Run: `uv run pytest tests/streams/test_log_stream.py tests/api_v1/test_log_websocket.py -q`

- [ ] **Step 3: Implement the versioned stream state machine**

Register live feed before capturing high-water cursor, replay persisted rows in 500-record pages, then enter live. Dedupe by product sequence only. ACK advances the last acknowledged sequence. Pause suppresses delivery but does not stop collection; resume backfills from supplied cursor.

- [ ] **Step 4: Implement explicit gaps and slow consumers**

Invalid/unavailable cursor emits `stream.gap` with earliest/latest cursors and action `refetch_http_snapshot`, then closes 1012. Slow consumer emits signal and closes 1013. Updates may not widen principal target authorization.

- [ ] **Step 5: Verify old subscription WebSocket**

Run:

```bash
uv run pytest tests/streams/test_log_stream.py tests/api_v1/test_log_websocket.py -q
uv run pytest tests/logs/test_subscriptions.py tests/web/test_log_subscriptions_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/streams/logs.py apps/control-plane/src/incidentlens_control_plane/api/ws/logs.py
```

- [ ] **Step 6: Commit log streaming**

```bash
git add apps/control-plane/src/incidentlens_control_plane/streams/logs.py apps/control-plane/src/incidentlens_control_plane/api/ws/logs.py apps/control-plane/src/incidentlens_control_plane/logs/subscriptions.py apps/control-plane/src/incidentlens_control_plane/main.py tests/streams/test_log_stream.py tests/api_v1/test_log_websocket.py
git commit -m "feat(logs): add recoverable realtime log stream"
```

---

### Task 13: Expand Approval Product Details and Decision Auditing

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/approvals/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/approvals/store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/approvals/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/investigation/service.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/approvals.py`
- Test: `tests/approvals/test_actor_reason.py`
- Test: `tests/approvals/test_downstream_status.py`
- Test: `tests/api_v1/test_approvals.py`

**Interfaces:**

```python
class ApprovalDecisionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)

class ApprovalDownstreamStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"
```

`ApprovalDetailView` includes safe linkage, risk, redacted preview/diff/impact/verification/rollback, decision status, downstream status, authenticated actor/reason, lifecycle timestamps, and bounded downstream error code. It omits canonical intent/hash and raw arguments.

Migration adds linkage, risk, preview JSON, actor/reason, and downstream fields plus status/session/investigation/target indexes.

- [ ] **Step 1: Write persisted-vs-downstream tests**

```python
def test_decision_remains_committed_when_downstream_fails(authenticated_client, approval_id):
    response = authenticated_client.post(
        f"/api/v1/approvals/{approval_id}/approve",
        headers={"Idempotency-Key": "approve-1"},
        json={"reason": "Reviewed exact diff and rollback plan."},
    )
    assert response.json()["decision_status"] == "approved"
    assert response.json()["decided_by"] == "operator-a"
    assert response.json()["downstream_status"] == "failed"
```

Also test expiry, duplicate/contradictory decisions, required reason, body actor rejection, safe preview, target authorization, and READ-only principals unable to decide.

- [ ] **Step 2: Run tests and observe missing details**

Run: `uv run pytest tests/approvals tests/api_v1/test_approvals.py -q`

- [ ] **Step 3: Add approval columns and creation linkage**

Update every Approval creation site to provide target/session/investigation/run/tool/changeset linkage, risk, and redacted preview. Backfill deterministic fields where possible; retain existing rows.

- [ ] **Step 4: Separate durable decision from downstream handling**

Atomically persist decision, Principal ID, and reason. Then set downstream processing, invoke current linkage, and persist processed/failed. Never roll back a durable decision because downstream resume failed.

- [ ] **Step 5: Add list/detail/decision v1 routes**

Support pagination and filters. Require APPROVE scope only on decisions; Web READ principals can list/get. Preserve exact hash, TTL, and one-shot consume behavior.

- [ ] **Step 6: Verify approval regressions**

Run:

```bash
uv run pytest tests/approvals tests/api_v1/test_approvals.py -q
uv run pytest tests/web/test_approvals_api.py tests/investigation/test_recovery.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/approvals apps/control-plane/src/incidentlens_control_plane/api/routes/approvals.py
```

- [ ] **Step 7: Commit approval API**

```bash
git add apps/control-plane/src/incidentlens_control_plane/approvals apps/control-plane/src/incidentlens_control_plane/investigation/service.py apps/control-plane/src/incidentlens_control_plane/api/routes/approvals.py tests/approvals tests/api_v1/test_approvals.py
git commit -m "feat(approvals): audit operator decisions and outcomes"
```

---

### Task 14: Add Overview and Service Read Projections

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/overview.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/services.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/overview.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/services.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Test: `tests/projections/test_overview.py`
- Test: `tests/projections/test_services.py`
- Test: `tests/api_v1/test_overview_services.py`

**Interfaces:**

```python
class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"

class OverviewView(BaseModel):
    generated_at: datetime
    targets: tuple[OverviewTargetView, ...]
    service_counts: StatusCounts
    open_issue_count: int
    active_investigation_count: int
    pending_approval_count: int
    recent_resolutions: tuple[ResolutionSummary, ...]
```

Routes: `GET /api/v1/overview`, `GET /api/v1/services/{service_id}`.

- [ ] **Step 1: Write safe projection tests**

Test target/service status, authorized filtering, injected UTC windows, recent resolutions, unknown versus healthy distinction, and absence of auth refs, protected paths, execution handles, approval actions, and raw Docker internals.

- [ ] **Step 2: Run tests and verify missing product reads**

Run: `uv run pytest tests/projections/test_overview.py tests/projections/test_services.py tests/api_v1/test_overview_services.py -q`

- [ ] **Step 3: Implement on-demand projections**

Query existing stores; do not create duplicate tables. Use deterministic health rules based on recent target tests, subscriptions, ERROR evidence, and uncertain/failed active investigations. Return `UNKNOWN` when there is no recent evidence; never call absence of data healthy.

- [ ] **Step 4: Add GET-only routes**

Require READ scope. Expose safe service/container/log-source summaries and related issue/investigation IDs. No mutation link or action field appears in response models.

- [ ] **Step 5: Verify projections**

Run:

```bash
uv run pytest tests/projections/test_overview.py tests/projections/test_services.py tests/api_v1/test_overview_services.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes/overview.py apps/control-plane/src/incidentlens_control_plane/api/routes/services.py
```

- [ ] **Step 6: Commit read projections**

```bash
git add apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes/overview.py apps/control-plane/src/incidentlens_control_plane/api/routes/services.py apps/control-plane/src/incidentlens_control_plane/runtime.py tests/projections tests/api_v1/test_overview_services.py
git commit -m "feat(web-api): add overview and service projections"
```

---

### Task 15: Add Issue, Investigation Summary, and Evidence Projections

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/issues.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/investigations.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/projections/evidence.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/issues.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/investigation_summaries.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/evidence.py`
- Test: `tests/projections/test_issues.py`
- Test: `tests/projections/test_investigation_summary.py`
- Test: `tests/api_v1/test_web_read_models.py`

**Interfaces:**

`issue_id` is deterministic `iss_<investigation_id>`; no Issues table is introduced. `IssueView` contains symptom, severity/status, grounded root cause/confidence when available, safe evidence with log cursor, resolution, verification, and timestamps. `InvestigationSummaryView` contains milestones, hypothesis summaries, safe evidence, conclusion, pending approval IDs, change/verification summaries. `EvidenceDetailView` contains only `content_redacted` and safe provenance.

Routes: list/get issues, list investigations, get summary, get evidence.

- [ ] **Step 1: Write projection-not-duplication tests**

Assert no `issues` table is created, issue IDs map to Investigations, confidence remains null when absent, status mappings distinguish mitigated/resolved/failed/waiting approval, root cause requires grounded Conclusion, and Evidence never exposes raw content.

- [ ] **Step 2: Run focused tests**

Run: `uv run pytest tests/projections tests/api_v1/test_web_read_models.py -q`

- [ ] **Step 3: Implement deterministic projection rules**

Map Investigation statuses to Issue statuses. Derive resolution/verification only from ChangeSet and validation evidence. Derive milestones from durable events. Attach product log cursors for log evidence. Do not invent confidence or severity unsupported by stored metadata/evidence.

- [ ] **Step 4: Add GET-only v1 routes**

Authorize through Target facade, paginate lists, and exclude decision/action affordances. Pending approvals appear only as status/IDs.

- [ ] **Step 5: Verify domain regressions**

Run:

```bash
uv run pytest tests/projections tests/api_v1/test_web_read_models.py -q
uv run pytest tests/evidence tests/investigation/test_store.py tests/changes/test_store.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes
```

- [ ] **Step 6: Commit issue projections**

```bash
git add apps/control-plane/src/incidentlens_control_plane/projections apps/control-plane/src/incidentlens_control_plane/api/routes/issues.py apps/control-plane/src/incidentlens_control_plane/api/routes/investigation_summaries.py apps/control-plane/src/incidentlens_control_plane/api/routes/evidence.py tests/projections tests/api_v1/test_web_read_models.py
git commit -m "feat(web-api): project issues and investigation results"
```

---

### Task 16: Add the Workspace SSE Invalidation Stream

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/streams/workspace.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/api/routes/workspace_events.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Test: `tests/streams/test_workspace_stream.py`
- Test: `tests/api_v1/test_workspace_sse.py`

**Interfaces:**

```python
class WorkspaceResourceKind(StrEnum):
    OVERVIEW = "overview"
    TARGET = "target"
    SERVICE = "service"
    ISSUE = "issue"
    INVESTIGATION = "investigation"
    EVIDENCE = "evidence"

class WorkspaceResourceChanged(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    event_id: str
    event_type: Literal["resource.changed"] = "resource.changed"
    occurred_at: datetime
    resource_kind: WorkspaceResourceKind
    resource_id: str | None = None
    target_id: str | None = None
    service_id: str | None = None

class WorkspaceStreamGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    event_id: str
    event_type: Literal["stream.gap"] = "stream.gap"
    occurred_at: datetime
    reason: str
    action: Literal["reload_snapshot"] = "reload_snapshot"
```

Route:

```text
GET /events/v1/workspace?after_event_id=&target_id=
Accept: text/event-stream
```

SSE frames use `id: <event_id>`, a stable `event: resource.changed|stream.gap`, and compact JSON `data:`. The endpoint also accepts the standard `Last-Event-ID` header; the query value is used only when the header is absent.

- [ ] **Step 1: Write replay, heartbeat, and authorization tests**

Test bearer/cookie authentication, target filtering, `Last-Event-ID` replay, query fallback, malformed/unknown cursor gap, one resource invalidation per relevant durable event, heartbeat comments, disconnect cleanup, and no sensitive event payload forwarded.

```python
def test_workspace_sse_replays_after_last_event_id(authenticated_client, seeded_events):
    with authenticated_client.stream(
        "GET",
        "/events/v1/workspace",
        headers={**AUTH_HEADERS, "Last-Event-ID": seeded_events[2].event_id},
    ) as response:
        frames = read_sse_frames(response, count=2)
    assert [frame.id for frame in frames] == [seeded_events[3].event_id, seeded_events[4].event_id]
```

- [ ] **Step 2: Run tests and verify the browser stream is absent**

Run: `uv run pytest tests/streams/test_workspace_stream.py tests/api_v1/test_workspace_sse.py -q`

Expected: FAIL with 404.

- [ ] **Step 3: Implement durable event-to-resource invalidation mapping**

Map safe durable event types to resource kinds/IDs. Do not forward the original payload as the SSE business object. Events for unauthorized targets are filtered before serialization. Unknown durable event types are ignored unless they create a cursor gap.

- [ ] **Step 4: Implement replay-to-live SSE delivery**

Authenticate before creating `StreamingResponse`. Subscribe before capturing high-water sequence, replay durable pages, then deliver live invalidations. Resolve event IDs to sequences through the Event store. If the requested event was never present or is outside retained history, send `stream.gap`; do not silently begin at latest. Emit `: heartbeat <UTC>` comments after 15 idle seconds and `Cache-Control: no-cache, no-transform` plus `X-Accel-Buffering: no`.

- [ ] **Step 5: Verify SSE and existing event APIs**

```bash
uv run pytest tests/streams/test_workspace_stream.py tests/api_v1/test_workspace_sse.py -q
uv run pytest tests/api_v1/test_event_history.py tests/web/test_events_api.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/streams/workspace.py apps/control-plane/src/incidentlens_control_plane/api/routes/workspace_events.py
```

- [ ] **Step 6: Commit workspace events**

```bash
git add apps/control-plane/src/incidentlens_control_plane/streams/workspace.py apps/control-plane/src/incidentlens_control_plane/api/routes/workspace_events.py apps/control-plane/src/incidentlens_control_plane/main.py apps/control-plane/src/incidentlens_control_plane/config.py tests/streams/test_workspace_stream.py tests/api_v1/test_workspace_sse.py
git commit -m "feat(streams): add workspace invalidation events"
```

---

### Task 17: Export Contracts and Run Integrated Backend Acceptance

**Files:**
- Create: `scripts/export_product_contracts.py`
- Create: `scripts/check_product_contracts.py`
- Create: `packages/protocol/openapi/v1.json`
- Create: `packages/protocol/schema/cli-stream-v1.schema.json`
- Create: `packages/protocol/schema/log-stream-v1.schema.json`
- Create: `packages/protocol/schema/workspace-stream-v1.schema.json`
- Create: `tests/contracts/test_openapi_v1.py`
- Create: `tests/contracts/test_protocol_schemas.py`
- Create: `tests/acceptance/test_product_api_foundation.py`
- Modify: `pyproject.toml`
- Modify: `README.md`

**Interfaces:**
- Consumes: `create_app().openapi()`, `StreamEventEnvelope.model_json_schema()`, `LogStreamEnvelope.model_json_schema()`, and the workspace SSE union schema.
- Produces deterministic checked-in contract files consumed by `packages/protocol` in the CLI/Web plans.

- [ ] **Step 1: Write operation-ID and schema secrecy tests**

Assert every `/api/v1` operation has a unique stable operation ID and structured success response; common failures reference `ApiErrorResponse`. Assert no response schema contains `authentication_ref`, canonical intent/hash, request payload, client actor fields, backup plaintext, or provider payload.

- [ ] **Step 2: Write protocol-schema tests**

Require integer schema version 1 and discriminated envelope/action unions for CLI events, log events, and workspace SSE events, with deterministic serialization using sorted keys, two-space indentation, UTF-8, and no generated timestamp.

- [ ] **Step 3: Implement deterministic export/check scripts**

Export OpenAPI without enabling its network route. Export WS Pydantic schemas directly. `check_product_contracts.py` regenerates in memory/temp files and exits nonzero on drift.

- [ ] **Step 4: Write the integrated acceptance test**

With fake SSH/model adapters, cover authenticated target creation/idempotent replay, strict host verification, durable target-test Operation, Session/message 202 acceptance, runtime restart, CLI WS replay beyond 1,000 events, log HTTP/WS cursor recovery, slow consumer signal, Approval actor/reason and downstream failure, read-only Web projections, docs disabled, legacy routes present, and running rollback becoming UNCERTAIN without replay.

- [ ] **Step 5: Document deployment constraints**

README must state one Uvicorn worker, persistent local volume, required auth profiles/signing key, TLS reverse proxy for secure cookies, temporary legacy API coexistence, and mandatory stream-version negotiation.

- [ ] **Step 6: Run all backend gates**

```bash
uv run pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q
uv run python scripts/check_product_contracts.py
uv run pytest tests -q
uv run ruff check apps/control-plane/src tests scripts
```

Expected: all tests and contract drift checks PASS.

- [ ] **Step 7: Commit backend contract completion**

```bash
git add scripts/export_product_contracts.py scripts/check_product_contracts.py packages/protocol/openapi/v1.json packages/protocol/schema tests/contracts tests/acceptance/test_product_api_foundation.py pyproject.toml README.md
git commit -m "feat(api): publish CLI and Web product contracts"
```

---

## Backend Phase Acceptance

Before starting either client plan, verify:

```bash
uv sync
uv run python scripts/check_product_contracts.py
uv run pytest tests/contracts tests/acceptance/test_product_api_foundation.py -q
uv run pytest -q
uv run ruff check .
```

The backend phase is complete only when:

- SSH host verification is strict by default.
- Product API authentication, target authorization, CSRF, and idempotency work.
- Target facade preserves ProjectRegistry safety boundaries.
- Messages return 202 and run through durable Operations.
- Runtime restart recovers safe work and never replays uncertain mutation.
- CLI event replay has no fixed 1,000-event hole.
- Log history/live use opaque cursor recovery and explicit gap/backpressure semantics.
- Approval decisions record authenticated actor/reason and separate persisted decision from downstream handling.
- Web read models contain no execution affordances or sensitive values.
- Contract files are deterministic and ready for TypeScript generation.
- Every existing `/api/*` regression test still passes.
