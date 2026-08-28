# Local Runtime and Project Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the persistent local Runtime, project/source-path registry, durable event journal, and shared HTTP/WebSocket API that later SSH, log, and Agent subsystems will use.

**Architecture:** A FastAPI application owns a `RuntimeServices` container created by an application lifespan. Domain models are immutable Pydantic values; SQLite repositories persist project records and runtime events; an in-memory broker fans durable events out to connected clients. This phase performs no SSH or Docker operations.

**Tech Stack:** Python 3.12, FastAPI 0.115+, Pydantic 2.13+, stdlib `sqlite3` and `asyncio`, pytest 8, Ruff.

## Global Constraints

- IncidentLens is a single-user tool running on the developer's local computer.
- Docker Compose over SSH is the only MVP remote runtime; this phase must not contact any remote server.
- The project registry stores paths and associations only; it never copies source code into SQLite.
- SSH private keys are never stored in the registry or exposed through API models.
- CLI and Web UI will consume the same local Runtime and event stream.
- Keep the existing `/healthz` response exactly `{"status": "ok", "remote_execution": "not_configured"}`.
- Python requirement remains `>=3.12,<3.13`; do not add a database ORM or message-broker dependency.

---

## File Structure

Create focused modules with these responsibilities:

```text
apps/control-plane/src/incidentlens_control_plane/
  config.py                         local data-directory resolution
  runtime.py                        service container and lifecycle construction
  events/
    __init__.py                     public event exports
    broker.py                       in-memory live fan-out
    store.py                        durable SQLite event journal
    types.py                        event domain values
  project_registry/
    __init__.py                     public registry exports
    store.py                        SQLite project repository
    types.py                        project/target/service domain values
  routes/
    __init__.py                     route package marker
    events.py                       HTTP snapshot and WebSocket event routes
    projects.py                     project CRUD routes
  main.py                           FastAPI application factory
tests/
  events/test_broker.py
  events/test_store.py
  project_registry/test_store.py
  project_registry/test_types.py
  web/test_events_api.py
  web/test_projects_api.py
  test_app.py
```

## Task 1: Define the Project Registry Domain

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_registry/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/project_registry/types.py`
- Create: `tests/project_registry/__init__.py`
- Create: `tests/project_registry/test_types.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, `ConfigDict`, `Field`, `field_validator`, `model_validator`.
- Produces: `ServiceRegistration`, `TargetRegistration`, `ProjectRegistration`, and `ProjectRecord`.

- [ ] **Step 1: Write failing validation tests**

```python
from datetime import UTC, datetime
from pathlib import Path

import pytest
from incidentlens_control_plane.project_registry.types import (
    ProjectRecord,
    ProjectRegistration,
    ServiceRegistration,
    TargetRegistration,
)
from pydantic import ValidationError


def valid_registration(tmp_path: Path) -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name="Payments",
        local_source_paths=(tmp_path.resolve(),),
        targets=(
            TargetRegistration(
                target_id="dev-a",
                host="dev-a.example.test",
                ssh_user="deploy",
                ssh_config_alias="dev-a",
            ),
        ),
        services=(
            ServiceRegistration(
                compose_service="payment-api",
                container_names=("payments-api-1",),
                local_source_path=tmp_path.resolve(),
                container_path_hints=("/app",),
                allowed_log_paths=("/var/log/payment/*.log",),
            ),
        ),
    )


def test_registration_accepts_paths_and_associations(tmp_path: Path) -> None:
    registration = valid_registration(tmp_path)
    record = ProjectRecord.from_registration(
        registration,
        created_at=datetime(2026, 8, 10, tzinfo=UTC),
    )

    assert record.project_id == "payments"
    assert record.services[0].local_source_path == tmp_path.resolve()
    assert record.created_at.tzinfo is UTC


def test_registration_rejects_relative_local_source_path() -> None:
    with pytest.raises(ValidationError, match="absolute"):
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(Path("relative/source"),),
            targets=(),
            services=(),
        )


def test_registration_rejects_duplicate_service_names(tmp_path: Path) -> None:
    service = ServiceRegistration(compose_service="api")
    with pytest.raises(ValidationError, match="compose_service"):
        ProjectRegistration(
            project_id="payments",
            display_name="Payments",
            local_source_paths=(tmp_path.resolve(),),
            targets=(),
            services=(service, service),
        )
```

- [ ] **Step 2: Run the tests and verify the module is missing**

Run: `uv run pytest tests/project_registry/test_types.py -q`

Expected: FAIL during collection with `ModuleNotFoundError` for `project_registry`.

- [ ] **Step 3: Implement immutable domain models and validators**

Implement the following exact public shapes in `project_registry/types.py`:

```python
class TargetRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_id: str = Field(min_length=1, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    ssh_user: str = Field(min_length=1, max_length=80)
    ssh_config_alias: str | None = Field(default=None, min_length=1, max_length=255)


class ServiceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    compose_service: str = Field(min_length=1, max_length=120)
    container_names: tuple[str, ...] = ()
    local_source_path: Path | None = None
    container_path_hints: tuple[str, ...] = ()
    allowed_log_paths: tuple[str, ...] = ()

    @field_validator("local_source_path")
    @classmethod
    def local_source_path_must_be_absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("local_source_path must be absolute")
        return value


class ProjectRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    display_name: str = Field(min_length=1, max_length=120)
    local_source_paths: tuple[Path, ...] = ()
    targets: tuple[TargetRegistration, ...] = ()
    services: tuple[ServiceRegistration, ...] = ()

    @field_validator("local_source_paths")
    @classmethod
    def local_paths_must_be_absolute(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        if any(not value.is_absolute() for value in values):
            raise ValueError("local_source_paths must be absolute")
        return values

    @model_validator(mode="after")
    def associations_must_be_unique(self) -> "ProjectRegistration":
        target_ids = [target.target_id for target in self.targets]
        if len(target_ids) != len(set(target_ids)):
            raise ValueError("target_id values must be unique")
        service_names = [service.compose_service for service in self.services]
        if len(service_names) != len(set(service_names)):
            raise ValueError("compose_service values must be unique")
        return self


class ProjectRecord(ProjectRegistration):
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_registration(
        cls, registration: ProjectRegistration, *, created_at: datetime
    ) -> "ProjectRecord":
        return cls(
            **registration.model_dump(),
            created_at=created_at,
            updated_at=created_at,
        )

    @field_validator("created_at", "updated_at")
    @classmethod
    def timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamps must be timezone-aware")
        return value
```

Use these validators exactly. Do not require paths to exist, because a registered workspace may be temporarily unmounted.

Export all four models from `project_registry/__init__.py`.

- [ ] **Step 4: Run the domain tests**

Run: `uv run pytest tests/project_registry/test_types.py -q`

Expected: PASS.

- [ ] **Step 5: Run Ruff and commit**

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/project_registry tests/project_registry`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_registry tests/project_registry
git commit -m "feat: define project registry domain"
```

## Task 2: Persist Projects in SQLite

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/project_registry/store.py`
- Create: `tests/project_registry/test_store.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/project_registry/__init__.py`

**Interfaces:**
- Consumes: `ProjectRecord`, `ProjectRegistration` from Task 1 and a `sqlite3.Connection` factory.
- Produces: `ProjectRegistryStore.migrate()`, `.create(registration, now)`, `.get(project_id)`, `.list()`, `.replace(registration, now)`, and `.delete(project_id)`; `ProjectAlreadyExists` and `ProjectNotFound`.

- [ ] **Step 1: Write failing repository tests**

```python
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest
from incidentlens_control_plane.project_registry.store import (
    ProjectAlreadyExists,
    ProjectNotFound,
    ProjectRegistryStore,
)
from incidentlens_control_plane.project_registry.types import ProjectRegistration


def connection_factory(path: Path):
    return lambda: sqlite3.connect(path)


def registration(path: Path, name: str = "Payments") -> ProjectRegistration:
    return ProjectRegistration(
        project_id="payments",
        display_name=name,
        local_source_paths=(path.resolve(),),
    )


def test_store_round_trips_project_across_connections(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)

    created = store.create(registration(tmp_path), now=now)
    reopened = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))

    assert reopened.get("payments") == created
    assert reopened.list() == (created,)


def test_store_rejects_duplicate_and_missing_projects(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    now = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=now)

    with pytest.raises(ProjectAlreadyExists):
        store.create(registration(tmp_path), now=now)
    with pytest.raises(ProjectNotFound):
        store.get("unknown")


def test_replace_preserves_created_at_and_updates_updated_at(tmp_path: Path) -> None:
    store = ProjectRegistryStore(connection_factory(tmp_path / "runtime.db"))
    store.migrate()
    created_at = datetime(2026, 8, 10, tzinfo=UTC)
    store.create(registration(tmp_path), now=created_at)

    replaced = store.replace(
        registration(tmp_path, name="Payments API"),
        now=created_at + timedelta(minutes=5),
    )

    assert replaced.created_at == created_at
    assert replaced.updated_at == created_at + timedelta(minutes=5)
```

- [ ] **Step 2: Run the tests and verify the store is missing**

Run: `uv run pytest tests/project_registry/test_store.py -q`

Expected: FAIL during collection because `project_registry.store` does not exist.

- [ ] **Step 3: Implement the SQLite repository**

Use this schema in `migrate()`:

```sql
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Serialize with `ProjectRecord.model_dump_json()` and deserialize with `ProjectRecord.model_validate_json()`. Open and close a fresh connection per method, use explicit transactions for writes, sort `list()` by `project_id`, and translate SQLite uniqueness errors into `ProjectAlreadyExists`. `replace` and `delete` must raise `ProjectNotFound` when no row changes.

- [ ] **Step 4: Run repository and domain tests**

Run: `uv run pytest tests/project_registry -q`

Expected: PASS.

- [ ] **Step 5: Run Ruff and commit**

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/project_registry tests/project_registry`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/project_registry tests/project_registry
git commit -m "feat: persist project registrations"
```

## Task 3: Add Durable Runtime Events and Live Fan-Out

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/events/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/events/types.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/events/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/events/broker.py`
- Create: `tests/events/__init__.py`
- Create: `tests/events/test_store.py`
- Create: `tests/events/test_broker.py`

**Interfaces:**
- Consumes: a `sqlite3.Connection` factory.
- Produces: `RuntimeEvent`, `RuntimeEventType`, `RuntimeEventStore.append()`, `.list_after()`, `RuntimeEventBroker.publish()`, and `.subscribe()`.

- [ ] **Step 1: Write failing event store tests**

```python
from datetime import UTC, datetime
from pathlib import Path
import sqlite3

from incidentlens_control_plane.events.store import RuntimeEventStore
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def test_event_store_returns_ordered_events_after_cursor(tmp_path: Path) -> None:
    store = RuntimeEventStore(lambda: sqlite3.connect(tmp_path / "runtime.db"))
    store.migrate()
    first = RuntimeEvent(
        event_id="evt-001",
        sequence=0,
        event_type=RuntimeEventType.PROJECT_CREATED,
        occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
        payload={"project_id": "payments"},
    )
    second = first.model_copy(
        update={"event_id": "evt-002", "event_type": RuntimeEventType.PROJECT_UPDATED}
    )

    stored_first = store.append(first)
    stored_second = store.append(second)

    assert stored_first.sequence == 1
    assert stored_second.sequence == 2
    assert store.list_after(1, limit=100) == (stored_second,)
```

- [ ] **Step 2: Write failing broker test**

```python
import asyncio
from datetime import UTC, datetime

from incidentlens_control_plane.events.broker import RuntimeEventBroker
from incidentlens_control_plane.events.types import RuntimeEvent, RuntimeEventType


def test_broker_delivers_event_to_active_subscriber() -> None:
    async def scenario() -> None:
        broker = RuntimeEventBroker(queue_size=4)
        event = RuntimeEvent(
            event_id="evt-001",
            sequence=1,
            event_type=RuntimeEventType.PROJECT_CREATED,
            occurred_at=datetime(2026, 8, 10, tzinfo=UTC),
            payload={"project_id": "payments"},
        )
        async with broker.subscribe() as queue:
            await broker.publish(event)
            assert await asyncio.wait_for(queue.get(), timeout=0.1) == event

    asyncio.run(scenario())
```

- [ ] **Step 3: Run tests and verify missing modules**

Run: `uv run pytest tests/events -q`

Expected: FAIL during collection because the events package does not exist.

- [ ] **Step 4: Implement event values, store, and broker**

Define:

```python
class RuntimeEventType(StrEnum):
    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"
    PROJECT_DELETED = "project.deleted"


class RuntimeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str = Field(min_length=1, max_length=120)
    sequence: int = Field(default=0, ge=0)
    event_type: RuntimeEventType
    occurred_at: datetime
    payload: dict[str, JsonValue]
```

Store events in an `INTEGER PRIMARY KEY AUTOINCREMENT` sequence column plus unique `event_id`, type, UTC timestamp, and JSON payload. `append()` ignores the input sequence and returns a copy containing the assigned sequence. `list_after(sequence, limit)` accepts limits from 1 through 1000 and returns ascending sequence order.

Implement `RuntimeEventBroker.subscribe()` as an `@asynccontextmanager` yielding a bounded `asyncio.Queue[RuntimeEvent]`. Register before yielding and unregister in `finally`. `publish()` must never block the Runtime: if a subscriber queue is full, remove its oldest item before adding the new event. Durable replay remains the store's responsibility.

- [ ] **Step 5: Run event tests, Ruff, and commit**

Run: `uv run pytest tests/events -q`

Expected: PASS.

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/events tests/events`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/events tests/events
git commit -m "feat: add durable runtime events"
```

## Task 4: Construct the Local Runtime Lifecycle

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `tests/test_app.py`

**Interfaces:**
- Consumes: `ProjectRegistryStore`, `RuntimeEventStore`, and `RuntimeEventBroker`.
- Produces: `RuntimeSettings`, `RuntimeServices`, `build_runtime(settings)`, and `create_app(settings=None)`.

- [ ] **Step 1: Extend application tests with isolated lifecycle construction**

```python
from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


def test_app_lifespan_creates_local_database(tmp_path: Path) -> None:
    settings = RuntimeSettings(data_dir=tmp_path / "incidentlens")
    app = create_app(settings)

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {
            "status": "ok",
            "remote_execution": "not_configured",
        }

    assert (settings.data_dir / "runtime.db").is_file()
```

Keep the existing `test_healthz_does_not_claim_a_remote_connection` unchanged.

- [ ] **Step 2: Run the focused test and verify missing interfaces**

Run: `uv run pytest tests/test_app.py -q`

Expected: FAIL because `RuntimeSettings` and `create_app` do not exist.

- [ ] **Step 3: Implement settings and Runtime construction**

Define:

```python
class RuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    data_dir: Path

    @classmethod
    def from_environment(cls) -> "RuntimeSettings":
        configured = os.environ.get("INCIDENTLENS_DATA_DIR")
        data_dir = Path(configured).expanduser() if configured else Path.home() / ".incidentlens"
        return cls(data_dir=data_dir.resolve())


@dataclass(frozen=True, slots=True)
class RuntimeServices:
    projects: ProjectRegistryStore
    events: RuntimeEventStore
    broker: RuntimeEventBroker


def build_runtime(settings: RuntimeSettings) -> RuntimeServices:
    settings.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    settings.data_dir.chmod(0o700)
    database_path = settings.data_dir / "runtime.db"

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(database_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    projects = ProjectRegistryStore(connect)
    events = RuntimeEventStore(connect)
    projects.migrate()
    events.migrate()
    return RuntimeServices(
        projects=projects,
        events=events,
        broker=RuntimeEventBroker(),
    )
```

`from_environment()` reads `INCIDENTLENS_DATA_DIR`; when absent, use `Path.home() / ".incidentlens"`. `build_runtime()` creates the data directory with mode `0o700`, uses `<data_dir>/runtime.db`, creates fresh SQLite connections with `PRAGMA foreign_keys = ON`, and runs both migrations.

Refactor `main.py` to expose `create_app(settings: RuntimeSettings | None = None) -> FastAPI`. Its lifespan builds `RuntimeServices`, stores it at `app.state.runtime`, and clears the reference on shutdown. Keep `app = create_app()` for Uvicorn and preserve the exact health contract.

- [ ] **Step 4: Run application and existing safety tests**

Run: `uv run pytest tests/test_app.py tests/project_registry tests/events tests/remote_ops tests/investigation -q`

Expected: PASS.

- [ ] **Step 5: Run Ruff and commit**

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane tests/test_app.py`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/runtime.py apps/control-plane/src/incidentlens_control_plane/main.py tests/test_app.py
git commit -m "feat: add local runtime lifecycle"
```

## Task 5: Expose Project Registry HTTP API

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/projects.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Create: `tests/web/__init__.py`
- Create: `tests/web/test_projects_api.py`

**Interfaces:**
- Consumes: `RuntimeServices`, project domain models/store, runtime event store/broker.
- Produces: `GET/POST /api/projects`, `GET/PUT/DELETE /api/projects/{project_id}`.

- [ ] **Step 1: Write failing project API test**

```python
from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


def test_project_crud_persists_and_emits_events(tmp_path: Path) -> None:
    app = create_app(RuntimeSettings(data_dir=tmp_path / "data"))
    payload = {
        "project_id": "payments",
        "display_name": "Payments",
        "local_source_paths": [str((tmp_path / "src").resolve())],
        "targets": [{
            "target_id": "dev-a",
            "host": "dev-a.example.test",
            "ssh_user": "deploy",
            "ssh_config_alias": "dev-a",
        }],
        "services": [{
            "compose_service": "payment-api",
            "container_names": [],
            "local_source_path": str((tmp_path / "src").resolve()),
            "container_path_hints": ["/app"],
            "allowed_log_paths": ["/var/log/payment/*.log"],
        }],
    }

    with TestClient(app) as client:
        created = client.post("/api/projects", json=payload)
        assert created.status_code == 201
        assert client.get("/api/projects/payments").json()["display_name"] == "Payments"
        assert len(client.get("/api/projects").json()) == 1
        events = client.get("/api/events", params={"after": 0}).json()
        assert events[0]["event_type"] == "project.created"
```

- [ ] **Step 2: Run the test and verify the route is absent**

Run: `uv run pytest tests/web/test_projects_api.py -q`

Expected: FAIL because `POST /api/projects` returns 404.

- [ ] **Step 3: Implement project routes and event publication**

Add a dependency:

```python
def get_runtime(request: Request) -> RuntimeServices:
    return cast(RuntimeServices, request.app.state.runtime)
```

Route behavior:

- `POST /api/projects`: validate `ProjectRegistration`, persist it, append `project.created`, publish the stored event, return 201.
- `GET /api/projects`: return records sorted by `project_id`.
- `GET /api/projects/{project_id}`: return a record or 404.
- `PUT /api/projects/{project_id}`: require body `project_id` to match the path, replace it, append/publish `project.updated`.
- `DELETE /api/projects/{project_id}`: delete it, append/publish `project.deleted`, return 204.
- Duplicate IDs return 409. Unknown IDs return 404. Validation errors use FastAPI's 422 response.

Generate event IDs with `uuid.uuid4().hex`, timestamps with `datetime.now(UTC)`, and include only `project_id` in event payloads. Register the router in `create_app()`.

Also create the `GET /api/events?after=<sequence>&limit=<1..1000>` snapshot route in `routes/events.py` during this task because the API test uses the durable event journal. It returns `RuntimeEventStore.list_after()`.

- [ ] **Step 4: Complete CRUD error tests**

Add these assertions to `tests/web/test_projects_api.py` using the `payload` fixture extracted from Step 1:

```python
def test_project_api_maps_conflicts_and_missing_records(
    client: TestClient, payload: dict[str, object]
) -> None:
    assert client.post("/api/projects", json=payload).status_code == 201
    assert client.post("/api/projects", json=payload).status_code == 409
    assert client.get("/api/projects/unknown").status_code == 404
    assert client.put("/api/projects/unknown", json=payload).status_code == 409
    assert client.delete("/api/projects/unknown").status_code == 404


def test_project_api_rejects_relative_source_path(
    client: TestClient, payload: dict[str, object]
) -> None:
    invalid = {**payload, "local_source_paths": ["relative/source"]}
    assert client.post("/api/projects", json=invalid).status_code == 422
```

The PUT expectation is 409 because the body contains `project_id="payments"` while the URL contains `unknown`; add a separate PUT test with matching `project_id="unknown"` and assert 404. Run:

`uv run pytest tests/web/test_projects_api.py -q`

Expected: PASS.

- [ ] **Step 5: Run the relevant suite, Ruff, and commit**

Run: `uv run pytest tests/test_app.py tests/web/test_projects_api.py tests/project_registry tests/events -q`

Expected: PASS.

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/routes apps/control-plane/src/incidentlens_control_plane/main.py tests/web`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes apps/control-plane/src/incidentlens_control_plane/main.py tests/web
git commit -m "feat: expose project registry api"
```

## Task 6: Add the Shared WebSocket Event Stream

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/events.py`
- Create: `tests/web/test_events_api.py`

**Interfaces:**
- Consumes: `RuntimeEventStore.list_after(sequence, limit)` and `RuntimeEventBroker.subscribe()`.
- Produces: `WS /api/events/ws?after=<sequence>` with durable catch-up followed by live events.

- [ ] **Step 1: Write the failing WebSocket replay/live test**

```python
from pathlib import Path

from fastapi.testclient import TestClient
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.main import create_app


def test_websocket_replays_then_streams_project_events(tmp_path: Path) -> None:
    app = create_app(RuntimeSettings(data_dir=tmp_path / "data"))
    payload = {
        "project_id": "payments",
        "display_name": "Payments",
        "local_source_paths": [],
        "targets": [],
        "services": [],
    }

    with TestClient(app) as client:
        client.post("/api/projects", json=payload)
        with client.websocket_connect("/api/events/ws?after=0") as socket:
            replayed = socket.receive_json()
            assert replayed["event_type"] == "project.created"

            client.put(
                "/api/projects/payments",
                json={**payload, "display_name": "Payments API"},
            )
            live = socket.receive_json()
            assert live["event_type"] == "project.updated"
            assert live["sequence"] > replayed["sequence"]
```

- [ ] **Step 2: Run the test and verify WebSocket route absence**

Run: `uv run pytest tests/web/test_events_api.py -q`

Expected: FAIL because `/api/events/ws` is not a WebSocket route.

- [ ] **Step 3: Implement replay-before-live streaming without a race**

The route must:

1. Accept the WebSocket.
2. Enter `broker.subscribe()` before reading the durable backlog.
3. Send `events.list_after(after, limit=1000)` in sequence order.
4. Track the highest sent sequence.
5. Read live queue events and skip any sequence already sent during replay.
6. Send each remaining event with `model_dump(mode="json")`.
7. Exit cleanly on `WebSocketDisconnect`.

Subscribing before replay avoids losing an event written between backlog lookup and live subscription; sequence de-duplication prevents sending it twice.

- [ ] **Step 4: Run WebSocket and complete API tests**

Run: `uv run pytest tests/web tests/test_app.py -q`

Expected: PASS without hanging after the WebSocket context closes.

- [ ] **Step 5: Run the full current suite, Ruff, and commit**

Run: `uv run pytest -q`

Expected: PASS.

Run: `uv run ruff check .`

Expected: PASS.

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes/events.py tests/web/test_events_api.py
git commit -m "feat: stream shared runtime events"
```

## Task 7: Document and Verify the Phase Boundary

**Files:**
- Modify: `README.md`
- Create: `docs/phase-1-local-runtime-verification.md`

**Interfaces:**
- Consumes: the application factory and project/event APIs completed above.
- Produces: reproducible local startup, registry API, and verification instructions; no new runtime API.

- [ ] **Step 1: Add an executable verification test to the document**

Document these exact commands and expected outcomes:

```bash
export INCIDENTLENS_DATA_DIR="$(mktemp -d)/incidentlens"
uv run uvicorn incidentlens_control_plane.main:app --port 8765
```

In a second terminal:

```bash
curl -sS http://127.0.0.1:8765/healthz
curl -sS -X POST http://127.0.0.1:8765/api/projects \
  -H 'content-type: application/json' \
  -d '{"project_id":"demo","display_name":"Demo","local_source_paths":[],"targets":[],"services":[]}'
curl -sS http://127.0.0.1:8765/api/projects
curl -sS 'http://127.0.0.1:8765/api/events?after=0'
```

Expected results: health continues to report remote execution unconfigured; project creation returns 201; list returns the persisted project; events contain `project.created`. Restart Uvicorn with the same data directory and confirm the project remains.

- [ ] **Step 2: Update README scope and startup instructions**

State that Phase 1 is a local-only Runtime and does not yet open SSH sessions. Document `INCIDENTLENS_DATA_DIR`, the project registry endpoints, event snapshot endpoint, WebSocket endpoint, and the existing `uv sync`, test, Ruff, and Uvicorn commands.

- [ ] **Step 3: Run all verification commands that do not require a long-lived server**

Run:

```bash
uv sync
uv run pytest -q
uv run ruff check .
```

Expected: all commands exit 0.

- [ ] **Step 4: Inspect the phase diff for scope**

Run: `git status --short` and `git diff --stat HEAD~6..HEAD`.

Expected: only Runtime, registry, events, routes, tests, README, and Phase 1 verification documentation are part of this phase; no SSH, Docker execution, model provider, or Web frontend implementation appears.

- [ ] **Step 5: Commit documentation**

```bash
git add README.md docs/phase-1-local-runtime-verification.md
git commit -m "docs: verify local runtime foundation"
```

## Phase Completion Gate

Before writing the Phase 2 implementation plan, verify all of the following:

- `uv run pytest -q` passes.
- `uv run ruff check .` passes.
- Registry records survive Runtime restart.
- Relative source paths and duplicate IDs are rejected.
- Durable events replay in sequence and live events arrive without gaps or duplicates.
- The application has not contacted SSH, Docker, a model API, or an external service.
- Existing remote operation policy and investigation guard tests still pass unchanged.
