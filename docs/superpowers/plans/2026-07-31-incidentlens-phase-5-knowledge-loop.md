# IncidentLens Phase 5 人工确认与组织记忆闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打通“调查报告自动沉淀—人工审核—正式检索—当前证据重新验证—反馈与评测”的完整知识闭环。

**Architecture:** 以关系型案例聚合作为当前快照，以追加式审核、反馈和使用事件提供审计；所有案例写操作经 `CaseService`，所有历史召回经 `HybridCaseRetriever`。调查运行时通过 `InvestigationMemoryCoordinator` 连接检索、候选假设、使用事件和终态沉淀，FastAPI、原生 Web 与评测存储只消费这些稳定接口。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、SQLite/FTS5、LangGraph、原生 HTML/CSS/JavaScript、pytest、Ruff、mypy、Docker Compose。

## Global Constraints

- 只有 `human_verified` 案例进入正式检索集合。
- 历史案例只能产生候选假设，不能继承已确认状态或置信度。
- 最终根因仍必须通过 Phase 4 的当前事故 Evidence 门禁。
- `report_ready` 按 `incident_id` 幂等生成 `agent_generated` 案例。
- 修改已确认案例时立即退出正式检索，重新确认后才恢复。
- `POST /api/cases` 只能创建 `draft`；客户端不能直接写目标状态。
- FTS5 必须完整离线可用；Embedding 未配置、超时、失败或维度不一致时降级为 `keyword_only`。
- API、CLI、恢复执行和重复 SSE 事件不得重复生成案例、反馈或使用事件。
- 数据升级必须保留旧案例并使用显式 schema version；禁止以删除旧表代替迁移。
- Web 与导出不得展示隐藏推理、API Key、Authorization、token 或未脱敏模型请求。
- 评测面板只展示实际 `RunRecord` 计算的结果；没有结果时显示空状态。
- 不新增生产写工具、自动修复、多 Agent、多租户、OAuth 或前端框架。

---

## 文件职责

### 案例领域与存储

- `apps/control-plane/src/incidentlens_control_plane/memory/domain.py`：案例、审核、反馈、使用事件、检索查询和结果的 Pydantic 契约与枚举。
- `apps/control-plane/src/incidentlens_control_plane/memory/models.py`：Phase 5 SQLAlchemy ORM 表，不包含状态机判断。
- `apps/control-plane/src/incidentlens_control_plane/memory/migrations.py`：显式、事务化、非破坏性的案例 schema 升级与 FTS5 创建。
- `apps/control-plane/src/incidentlens_control_plane/memory/repository.py`：接收 `Session` 的持久化原语、列表、历史、索引和统计查询。
- `apps/control-plane/src/incidentlens_control_plane/memory/service.py`：案例状态机、乐观锁、自动沉淀、审核和反馈事务。
- `apps/control-plane/src/incidentlens_control_plane/memory/embedding.py`：可插拔 Embedding 协议、禁用实现和受控错误。
- `apps/control-plane/src/incidentlens_control_plane/memory/retrieval.py`：硬过滤、FTS5、语义合并、排序、相似理由和降级。
- `apps/control-plane/src/incidentlens_control_plane/memory/integration.py`：调查与案例记忆之间的召回、候选假设、使用事件、终态沉淀和终态验证协调。

### 调查、API 与评测

- `apps/control-plane/src/incidentlens_control_plane/agent/state.py`：在公共调查状态增加关联案例字段。
- `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`：LLM 运行时调用记忆协调器，并在 load/resume/terminal 路径保持幂等。
- `apps/control-plane/src/incidentlens_control_plane/agent/baseline.py`：确定性基线使用同一记忆协调器。
- `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`：把 `InvestigationMemoryCoordinator` 注入两种运行时。
- `apps/control-plane/src/incidentlens_control_plane/routes/cases.py`：案例读取、草稿创建、编辑、审核、反馈和历史 API。
- `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`：响应关联案例，并提供调查导出入口。
- `apps/control-plane/src/incidentlens_control_plane/services/investigation_export.py`：构造带 schema version、大小限制和密钥脱敏的调查导出。
- `apps/control-plane/src/incidentlens_control_plane/evaluations/store.py`：实际评测运行、逐场景记录和聚合指标持久化。
- `apps/control-plane/src/incidentlens_control_plane/routes/evaluations.py`：读取最近完成的策略对比。
- `apps/control-plane/src/incidentlens_control_plane/main.py`：创建、注入并注册 Phase 5 组件。
- `apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py`：按外键安全顺序清理新增 Demo 表和 FTS 内容。
- `packages/evaluation/src/incidentlens_evaluation/metrics.py`：八项指标及真实历史案例误导率。
- `packages/evaluation/src/incidentlens_evaluation/runner.py`：保存实际运行、失败摘要和聚合结果。
- `packages/evaluation/src/incidentlens_evaluation/cli.py`：可在宿主机或 Compose
  控制平面容器内运行三策略/五场景并写入指定 SQLite。

### Web 与文档

- `apps/control-plane/static/index.html`：审核队列、案例详情、检索、反馈、导出和评测面板结构。
- `apps/control-plane/static/app.js`：真实 API 客户端、revision 冲突处理、转义和空状态。
- `apps/control-plane/static/styles.css`：治理组件、表格、状态徽章和响应式布局。
- `README.md`：Phase 5 演示、配置和项目边界。
- `docs/evaluation.md`：八项指标、持久化评测命令和误导率定义。

---

### Task 1: 冻结案例领域契约并完成非破坏性 schema 迁移

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/domain.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/migrations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/memory/models.py`
- Modify: `apps/control-plane/pyproject.toml`
- Modify: `pyproject.toml`
- Create: `tests/memory/test_case_domain.py`
- Create: `tests/memory/test_case_migrations.py`

**Interfaces:**
- Produces: `CaseStatus`, `ReviewAction`, `FeedbackRating`, `UsageEventType`, `CaseSnapshot`, `CaseDraft`, `CaseSearchQuery`, `CaseSearchHit`.
- Produces: `migrate_case_schema(engine: Engine) -> None`.
- Produces: ORM rows `CaseRow`, `CaseReviewActionRow`, `CaseFeedbackRow`, `CaseUsageEventRow`, `CaseEmbeddingRow`, `CaseSchemaVersionRow`.
- Consumes: existing SQLite engine and legacy `case_memory` columns.

- [ ] **Step 1: Write failing domain-contract tests**

```python
# tests/memory/test_case_domain.py
from pydantic import ValidationError
import pytest

from incidentlens_control_plane.memory.domain import (
    CaseDraft,
    CaseStatus,
    FeedbackRating,
)


def test_case_status_is_exactly_the_five_approved_values() -> None:
    assert {item.value for item in CaseStatus} == {
        "draft",
        "agent_generated",
        "human_verified",
        "deprecated",
        "rejected",
    }


def test_feedback_rating_rejects_unknown_value() -> None:
    with pytest.raises(ValidationError):
        CaseDraft(
            symptom="timeouts",
            affected_services=["order-service"],
            feedback="excellent",
        )


def test_case_draft_requires_symptom_and_service() -> None:
    with pytest.raises(ValidationError):
        CaseDraft(symptom="", affected_services=[])


def test_feedback_enum_is_exact() -> None:
    assert {item.value for item in FeedbackRating} == {
        "helpful", "partial", "irrelevant", "stale", "wrong"
    }
```

- [ ] **Step 2: Write failing migration tests against the legacy schema**

```python
# tests/memory/test_case_migrations.py
from sqlalchemy import inspect, text
from incidentlens_telemetry.database import create_engine

from incidentlens_control_plane.memory.migrations import migrate_case_schema


def test_legacy_case_is_preserved_and_mapped() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE case_memory ("
            "id INTEGER PRIMARY KEY, status VARCHAR(64), symptom TEXT, "
            "service VARCHAR(255), root_cause TEXT, resolution TEXT, "
            "evidence_summary TEXT, created_at DATETIME, updated_at DATETIME)"
        )
        conn.execute(
            text(
                "INSERT INTO case_memory "
                "(id,status,symptom,service,root_cause,resolution,evidence_summary) "
                "VALUES (1,'pending_review','timeout','order-service',"
                "'payment_delay','rollback','ev-1')"
            )
        )

    migrate_case_schema(engine)

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT status, affected_services_json, root_cause_description, "
                "key_evidence_json FROM case_memory WHERE id=1"
            )
        ).mappings().one()
        assert row["status"] == "draft"
        assert row["affected_services_json"] == '["order-service"]'
        assert row["root_cause_description"] == "payment_delay"
        assert "ev-1" in row["key_evidence_json"]


def test_migration_creates_governance_tables_and_real_fts5() -> None:
    engine = create_engine("sqlite:///:memory:")
    migrate_case_schema(engine)
    names = set(inspect(engine).get_table_names())
    assert {
        "case_memory",
        "case_review_actions",
        "case_feedback",
        "case_usage_events",
        "case_embeddings",
        "incidentlens_schema_versions",
    } <= names
    with engine.connect() as conn:
        sql = conn.execute(
            text("SELECT sql FROM sqlite_master WHERE name='case_fts'")
        ).scalar_one()
    assert "VIRTUAL TABLE" in sql.upper()
    assert "FTS5" in sql.upper()


def test_migration_is_idempotent() -> None:
    engine = create_engine("sqlite:///:memory:")
    migrate_case_schema(engine)
    migrate_case_schema(engine)
    with engine.connect() as conn:
        count = conn.execute(
            text(
                "SELECT COUNT(*) FROM incidentlens_schema_versions "
                "WHERE component='case_memory' AND version=5"
            )
        ).scalar_one()
    assert count == 1
```

- [ ] **Step 3: Run the focused tests and confirm the missing modules fail**

Run:

```bash
uv run pytest tests/memory/test_case_domain.py tests/memory/test_case_migrations.py -q
```

Expected: FAIL during import because `domain.py` and `migrations.py` do not exist.

- [ ] **Step 4: Add exact domain enums and validated commands**

```python
# apps/control-plane/src/incidentlens_control_plane/memory/domain.py
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CaseStatus(StrEnum):
    DRAFT = "draft"
    AGENT_GENERATED = "agent_generated"
    HUMAN_VERIFIED = "human_verified"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


class ReviewAction(StrEnum):
    CREATE = "create"
    MATERIALIZE = "materialize"
    EDIT = "edit"
    CONFIRM = "confirm"
    REJECT = "reject"
    DEPRECATE = "deprecate"


class FeedbackRating(StrEnum):
    HELPFUL = "helpful"
    PARTIAL = "partial"
    IRRELEVANT = "irrelevant"
    STALE = "stale"
    WRONG = "wrong"


class UsageEventType(StrEnum):
    RECALLED = "recalled"
    ADOPTED = "adopted"
    VALIDATED = "validated"
    MISLEADING = "misleading"


class CaseDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symptom: str = Field(min_length=1, max_length=4000)
    affected_services: list[str] = Field(min_length=1, max_length=20)
    root_cause_category: str = Field(default="", max_length=255)
    root_cause_description: str = Field(default="", max_length=8000)
    key_evidence: list[dict[str, Any]] = Field(default_factory=list)
    investigation_path: list[dict[str, Any]] = Field(default_factory=list)
    invalid_hypotheses: list[dict[str, Any]] = Field(default_factory=list)
    resolution: str = Field(default="", max_length=8000)
    remediation_advice: list[str] = Field(default_factory=list)
    applicability_conditions: list[str] = Field(default_factory=list)
    inapplicability_conditions: list[str] = Field(default_factory=list)
    environment: str = Field(default="", max_length=255)
    service_version_exact: str = Field(default="", max_length=255)
    service_version_min: str = Field(default="", max_length=255)
    service_version_max: str = Field(default="", max_length=255)

    @model_validator(mode="after")
    def validate_versions(self) -> "CaseDraft":
        if self.service_version_exact and (
            self.service_version_min or self.service_version_max
        ):
            raise ValueError("exact version cannot be combined with a version range")
        return self


```

Also define `CaseSnapshot`, `CaseSearchQuery`, `CaseSearchHit`, `CaseHistory`,
`FeedbackCommand`, and `ReviewCommand` in the same file using the exact fields from
the design spec. Do not add a `status` field to any client write command.

- [ ] **Step 5: Replace the simplified ORM and add explicit migration DDL**

Implement ORM rows with the exact table and column names from the design. Store
nested data as JSON text, store the vector as `vector_json`, and add unique
constraints for `case_memory.incident_id`, `case_feedback.idempotency_key`, and
`case_usage_events.idempotency_key`.

`migrate_case_schema` must:

```python
CASE_SCHEMA_COMPONENT = "case_memory"
CASE_SCHEMA_VERSION = 5


def migrate_case_schema(engine: Engine) -> None:
    with engine.begin() as conn:
        _create_version_table(conn)
        if _current_version(conn) >= CASE_SCHEMA_VERSION:
            return
        _create_or_extend_case_memory(conn)
        _create_governance_tables(conn)
        _create_fts5(conn)
        _migrate_legacy_values(conn)
        _rebuild_verified_fts(conn)
        conn.execute(
            text(
                "INSERT INTO incidentlens_schema_versions(component, version) "
                "VALUES (:component, :version)"
            ),
            {"component": CASE_SCHEMA_COMPONENT, "version": CASE_SCHEMA_VERSION},
        )
```

Use `ALTER TABLE ... ADD COLUMN` for legacy `case_memory`; never drop it. Keep old
legacy columns ignored by the new ORM. Map `pending_review -> draft`, preserve
`human_verified`, and map any other legacy status to `draft` with a migration review
record containing the original value. Keep legacy `incident_id` as `NULL` and set
`source_reference=f"legacy-case:{id}"`; new materialized cases use
`source_reference=f"incident:{incident_id}"`.

- [ ] **Step 6: Add the direct version parsing dependency**

Add `"packaging>=24,<27"` to the root and control-plane dependencies, then run:

```bash
uv lock
uv run pytest tests/memory/test_case_domain.py tests/memory/test_case_migrations.py -q
```

Expected: PASS.

- [ ] **Step 7: Run static checks for the new boundary**

Run:

```bash
uv run ruff check apps/control-plane/src/incidentlens_control_plane/memory tests/memory
uv run mypy apps/control-plane/src/incidentlens_control_plane/memory
```

Expected: both commands exit 0.

- [ ] **Step 8: Commit the domain and migration boundary**

```bash
git add pyproject.toml uv.lock apps/control-plane/pyproject.toml \
  apps/control-plane/src/incidentlens_control_plane/memory/domain.py \
  apps/control-plane/src/incidentlens_control_plane/memory/models.py \
  apps/control-plane/src/incidentlens_control_plane/memory/migrations.py \
  tests/memory/test_case_domain.py tests/memory/test_case_migrations.py
git commit -m "feat: add governed case memory schema"
```

---

### Task 2: 实现案例 Repository、状态机和事务化审核

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/memory/repository.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/service.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/memory/__init__.py`
- Replace: `tests/memory/test_case_retrieval.py`
- Create: `tests/memory/test_case_service.py`

**Interfaces:**
- Consumes: Task 1 的领域模型、ORM 和 `migrate_case_schema`.
- Produces: `CaseRepository.transaction() -> Iterator[Session]`.
- Produces: `CaseService.create_draft`, `materialize_from_investigation`, `edit`, `confirm`, `reject`, `deprecate`, `add_feedback`.
- Produces: `CaseNotFoundError`, `CaseConflictError`, `InvalidCaseTransitionError`.

Use these exact service signatures:

```python
def create_draft(self, draft: CaseDraft, actor: str) -> CaseSnapshot: ...
def materialize_from_investigation(
    self, state: InvestigationState
) -> CaseSnapshot: ...
def edit(
    self,
    case_id: int,
    expected_version: int,
    patch: CaseDraft,
    actor: str,
    reason: str = "",
) -> CaseSnapshot: ...
def confirm(
    self,
    case_id: int,
    expected_version: int,
    actor: str,
    reason: str = "",
) -> CaseSnapshot: ...
def reject(
    self,
    case_id: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> CaseSnapshot: ...
def deprecate(
    self,
    case_id: int,
    expected_version: int,
    actor: str,
    reason: str,
) -> CaseSnapshot: ...
def add_feedback(self, command: FeedbackCommand) -> FeedbackRecord: ...
```

- [ ] **Step 1: Write failing lifecycle tests**

```python
# tests/memory/test_case_service.py
import pytest
from incidentlens_contracts.models import InvestigationStatus
from incidentlens_telemetry.database import create_engine

from incidentlens_control_plane.agent.state import InvestigationState
from incidentlens_control_plane.memory.domain import CaseDraft, CaseStatus
from incidentlens_control_plane.memory.repository import CaseRepository
from incidentlens_control_plane.memory.service import (
    CaseConflictError,
    CaseService,
    InvalidCaseTransitionError,
)


@pytest.fixture
def service() -> CaseService:
    return CaseService(CaseRepository(create_engine("sqlite:///:memory:")))


def test_create_endpoint_contract_always_creates_draft(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    assert case.status is CaseStatus.DRAFT
    assert case.revision == 1


def test_materialize_is_idempotent_by_incident_id(service: CaseService) -> None:
    state = InvestigationState(
        incident_id="inc-1",
        status=InvestigationStatus.REPORT_READY,
        alert={"service": "order-service", "symptom": "timeout"},
        report={
            "root_service": "payment-service",
            "root_cause": "downstream-timeout",
            "evidence_ids": ["ev-1"],
            "findings": [{"evidence_id": "ev-1", "source_tool": "get_slow_traces"}],
        },
    )
    first = service.materialize_from_investigation(state)
    second = service.materialize_from_investigation(state)
    assert first.id == second.id
    assert second.status is CaseStatus.AGENT_GENERATED


def test_confirm_indexes_and_edit_removes_from_search(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(
            symptom="timeout",
            affected_services=["order-service"],
            root_cause_category="downstream-timeout",
            root_cause_description="payment service latency propagated upstream",
            key_evidence=[{"evidence_id": "ev-1", "source_tool": "get_slow_traces"}],
            resolution="remove the injected delay",
        ),
        actor="local-user",
    )
    verified = service.confirm(
        case.id, expected_version=case.revision, actor="reviewer", reason="checked"
    )
    assert verified.status is CaseStatus.HUMAN_VERIFIED
    edited = service.edit(
        case.id,
        expected_version=verified.revision,
        patch=CaseDraft(
            symptom="timeout updated",
            affected_services=["order-service"],
        ),
        actor="reviewer",
        reason="correct wording",
    )
    assert edited.status is CaseStatus.DRAFT


def test_stale_revision_is_rejected(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    with pytest.raises(CaseConflictError):
        service.confirm(case.id, expected_version=99, actor="reviewer")


def test_rejected_case_cannot_be_confirmed_without_edit(service: CaseService) -> None:
    case = service.create_draft(
        CaseDraft(symptom="timeout", affected_services=["order-service"]),
        actor="local-user",
    )
    rejected = service.reject(case.id, case.revision, "reviewer", "wrong cause")
    with pytest.raises(InvalidCaseTransitionError):
        service.confirm(rejected.id, rejected.revision, "reviewer")
```

- [ ] **Step 2: Run the lifecycle tests and verify they fail**

Run:

```bash
uv run pytest tests/memory/test_case_service.py -q
```

Expected: FAIL because `CaseService` is missing.

- [ ] **Step 3: Refactor Repository into session-based persistence primitives**

Use this public shape:

```python
class CaseRepository:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        migrate_case_schema(engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with Session(self.engine) as session:
            with session.begin():
                yield session

    def add_case(self, session: Session, row: CaseRow) -> CaseRow: ...
    def get_case(self, session: Session, case_id: int) -> CaseRow | None: ...
    def get_by_incident(self, session: Session, incident_id: str) -> CaseRow | None: ...
    def add_review(self, session: Session, row: CaseReviewActionRow) -> None: ...
    def add_feedback(self, session: Session, row: CaseFeedbackRow) -> None: ...
    def add_usage_event(self, session: Session, row: CaseUsageEventRow) -> None: ...
    def replace_fts(self, session: Session, case: CaseRow) -> None: ...
    def remove_fts(self, session: Session, case_id: int) -> None: ...
```

Catch only the unique-constraint errors needed for idempotency; re-raise unrelated
database failures so the transaction rolls back.

- [ ] **Step 4: Implement the explicit transition map and optimistic lock**

```python
ALLOWED_TRANSITIONS: dict[tuple[CaseStatus, ReviewAction], CaseStatus] = {
    (CaseStatus.DRAFT, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.AGENT_GENERATED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.HUMAN_VERIFIED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.REJECTED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.DEPRECATED, ReviewAction.EDIT): CaseStatus.DRAFT,
    (CaseStatus.DRAFT, ReviewAction.CONFIRM): CaseStatus.HUMAN_VERIFIED,
    (CaseStatus.AGENT_GENERATED, ReviewAction.CONFIRM): CaseStatus.HUMAN_VERIFIED,
    (CaseStatus.DRAFT, ReviewAction.REJECT): CaseStatus.REJECTED,
    (CaseStatus.AGENT_GENERATED, ReviewAction.REJECT): CaseStatus.REJECTED,
    (CaseStatus.HUMAN_VERIFIED, ReviewAction.DEPRECATE): CaseStatus.DEPRECATED,
}


def _assert_revision(row: CaseRow, expected_version: int) -> None:
    if row.revision != expected_version:
        raise CaseConflictError(
            f"case {row.id} revision is {row.revision}, expected {expected_version}"
        )
```

Each service method must load, check revision, check the transition, update the row,
increment `revision`, append `case_review_actions`, and update/remove FTS in one
`repository.transaction()`.

Before `confirm`, require non-empty `root_cause_category`,
`root_cause_description`, `key_evidence`, and at least one of `resolution` or
`remediation_advice`; return a domain validation error mapped to HTTP 422 when the
review content is incomplete. Legacy rows already marked `human_verified` remain
readable and are not silently demoted by migration.

- [ ] **Step 5: Implement exact materialization mapping**

`materialize_from_investigation` must reject non-`report_ready` states and map:

```python
draft = CaseDraft(
    symptom=str(state.alert.get("symptom") or state.alert),
    affected_services=list(dict.fromkeys([
        str(state.alert.get("service", "")),
        str(state.report.get("root_service", "")),
    ])) if state.report else [str(state.alert.get("service", ""))],
    root_cause_category=str(state.report.get("root_cause", "")),
    root_cause_description=str(state.report.get("root_cause", "")),
    key_evidence=list(state.report.get("findings", [])),
    investigation_path=[
        {"round": state.current_round, "phase": state.phase}
    ],
    invalid_hypotheses=[
        h.model_dump(mode="json")
        for h in state.hypotheses
        if str(h.status) == "ruled_out"
    ],
    remediation_advice=[],
)
```

Filter empty service strings before validation. Save the full report in
`source_report_json`. If a row with the same `incident_id` exists, return it without
adding a second review record.

- [ ] **Step 6: Replace legacy repository tests with governed behavior**

Keep coverage for keyword search for now only as an assertion that unverified cases
are absent from `case_fts`; Task 3 will own ranking behavior. Add tests for:

- repeating confirm after the first transition returns a `409` transition conflict;
- feedback idempotency key returns the original feedback;
- modifying a verified case removes its FTS row;
- deprecating removes FTS;
- review history remains ordered and append-only;
- a forced FTS insert failure rolls back both status and review action.

- [ ] **Step 7: Run focused tests and static checks**

```bash
uv run pytest tests/memory/test_case_domain.py \
  tests/memory/test_case_migrations.py \
  tests/memory/test_case_service.py \
  tests/memory/test_case_retrieval.py -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/memory tests/memory
uv run mypy apps/control-plane/src/incidentlens_control_plane/memory
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit the governed case aggregate**

```bash
git add apps/control-plane/src/incidentlens_control_plane/memory \
  tests/memory/test_case_service.py tests/memory/test_case_retrieval.py
git commit -m "feat: enforce case review lifecycle"
```

---

### Task 3: 实现 FTS5、结构化过滤与可降级混合检索

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/embedding.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/retrieval.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/memory/repository.py`
- Create: `tests/memory/test_hybrid_retrieval.py`
- Create: `tests/memory/test_embedding_fallback.py`

**Interfaces:**
- Consumes: `CaseRepository`, `CaseSearchQuery`, verified-case FTS5 rows.
- Produces: `EmbeddingProvider.embed_documents(texts)`, `embed_query(text)`, `identity`.
- Produces: `DisabledEmbeddingProvider`.
- Produces: `HybridCaseRetriever.search(query: CaseSearchQuery) -> list[CaseSearchHit]`.

- [ ] **Step 1: Write failing retrieval tests**

```python
# tests/memory/test_hybrid_retrieval.py
def test_only_verified_cases_are_returned(case_service, retriever) -> None:
    draft = case_service.create_draft(
        CaseDraft(symptom="payment timeout", affected_services=["order-service"]),
        "local-user",
    )
    verified = case_service.create_draft(
        CaseDraft(
            symptom="payment timeout",
            affected_services=["order-service"],
            root_cause_category="downstream-timeout",
            root_cause_description="payment latency propagated to orders",
            key_evidence=[{"evidence_id": "ev-timeout"}],
            resolution="remove downstream delay",
        ),
        "local-user",
    )
    case_service.confirm(verified.id, verified.revision, "reviewer")
    hits = retriever.search(CaseSearchQuery(text="payment timeout"))
    assert [hit.case_id for hit in hits] == [verified.id]
    assert draft.id not in {hit.case_id for hit in hits}


def test_same_symptom_different_root_causes_remain_separate(case_service, retriever) -> None:
    ids = []
    for cause in ("downstream-timeout", "deployment-regression"):
        case = case_service.create_draft(
            CaseDraft(
                symptom="order latency",
                affected_services=["order-service"],
                root_cause_category=cause,
                root_cause_description=f"verified historical cause: {cause}",
                key_evidence=[{"evidence_id": f"ev-{cause}"}],
                resolution="apply the reviewed remediation",
            ),
            "local-user",
        )
        ids.append(case_service.confirm(case.id, case.revision, "reviewer").id)
    hits = retriever.search(CaseSearchQuery(text="order latency"))
    assert {hit.case_id for hit in hits} == set(ids)


def test_semver_range_and_environment_are_hard_filters(case_service, retriever) -> None:
    case = case_service.create_draft(
        CaseDraft(
            symptom="pool exhaustion",
            affected_services=["order-service"],
            environment="staging",
            service_version_min="2.0.0",
            service_version_max="2.4.0",
            root_cause_category="database-pool-exhaustion",
            root_cause_description="connection acquisition saturated",
            key_evidence=[{"evidence_id": "ev-pool"}],
            resolution="right-size and release the pool",
        ),
        "local-user",
    )
    case_service.confirm(case.id, case.revision, "reviewer")
    assert retriever.search(
        CaseSearchQuery(
            text="pool exhaustion",
            environment="production",
            service_version="2.2.0",
        )
    ) == []
```

- [ ] **Step 2: Write failing fallback tests with a deterministic fake**

```python
# tests/memory/test_embedding_fallback.py
class FakeEmbeddingProvider:
    identity = EmbeddingIdentity(provider="fake", model="fake-v1", dimension=2)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "timeout" in text else [0.0, 1.0] for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return [1.0, 0.0]


class FailingEmbeddingProvider(FakeEmbeddingProvider):
    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailableError("provider_timeout")


def test_hybrid_result_exposes_component_scores(hybrid_retriever) -> None:
    hit = hybrid_retriever.search(CaseSearchQuery(text="timeout"))[0]
    assert hit.retrieval_mode == "hybrid"
    assert 0 <= hit.lexical_score <= 1
    assert 0 <= hit.semantic_score <= 1
    assert hit.similarity_reason


def test_embedding_failure_returns_keyword_only(keyword_case_repository) -> None:
    retriever = HybridCaseRetriever(
        keyword_case_repository,
        FailingEmbeddingProvider(),
    )
    hits = retriever.search(CaseSearchQuery(text="timeout"))
    assert hits
    assert all(hit.retrieval_mode == "keyword_only" for hit in hits)
    assert retriever.last_degradation_reason == "provider_timeout"
```

- [ ] **Step 3: Run tests and confirm the retrieval modules are missing**

```bash
uv run pytest tests/memory/test_hybrid_retrieval.py \
  tests/memory/test_embedding_fallback.py -q
```

Expected: FAIL during import.

- [ ] **Step 4: Implement the Embedding protocol and cosine helper**

```python
@runtime_checkable
class EmbeddingProvider(Protocol):
    identity: EmbeddingIdentity
    def embed_documents(self, texts: list[str]) -> list[list[float]]: ...
    def embed_query(self, text: str) -> list[float]: ...


class DisabledEmbeddingProvider:
    identity = EmbeddingIdentity(provider="disabled", model="", dimension=0)

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        raise EmbeddingUnavailableError("embedding_not_configured")

    def embed_query(self, text: str) -> list[float]:
        raise EmbeddingUnavailableError("embedding_not_configured")


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if len(left) != len(right):
        raise EmbeddingDimensionError(f"{len(left)} != {len(right)}")
    denominator = math.sqrt(sum(x * x for x in left)) * math.sqrt(
        sum(x * x for x in right)
    )
    return 0.0 if denominator == 0 else sum(
        x * y for x, y in zip(left, right, strict=True)
    ) / denominator
```

- [ ] **Step 5: Implement safe FTS and exact filter semantics**

Repository FTS must bind the query rather than concatenate SQL. Normalize user text
to quoted tokens before `MATCH`, cap FTS candidates at 20, and return the SQLite
`bm25(case_fts)` rank.

Implement:

```python
def version_matches(case: CaseSnapshot, requested: str) -> bool:
    if not requested:
        return True
    if case.service_version_exact:
        return requested == case.service_version_exact
    if not case.service_version_min and not case.service_version_max:
        return True
    try:
        current = Version(requested.removeprefix("v"))
        lower = (
            Version(case.service_version_min.removeprefix("v"))
            if case.service_version_min else None
        )
        upper = (
            Version(case.service_version_max.removeprefix("v"))
            if case.service_version_max else None
        )
    except InvalidVersion:
        return False
    return (lower is None or current >= lower) and (upper is None or current <= upper)
```

Apply service, category, environment, version, applicability and inapplicability
before scoring.

- [ ] **Step 6: Implement deterministic hybrid scoring and explanations**

Use these fixed weights:

```python
HYBRID_WEIGHTS = {
    "lexical": 0.45,
    "semantic": 0.35,
    "filter": 0.15,
    "feedback": 0.05,
}
KEYWORD_WEIGHTS = {
    "lexical": 0.70,
    "filter": 0.25,
    "feedback": 0.05,
}
```

Normalize lexical ranks within the candidate set, calculate cosine similarity in
`[0, 1]`, use exact filter matches for `filter_score`, and clamp feedback contribution
to `[0, 1]`. Sort by `(-total_score, case_id)`. Build reasons from explicit clauses
such as `symptom matched "timeout"`, `service matched order-service`,
`version 2.2.0 within [2.0.0,2.4.0]`, and `semantic score 0.91`.

- [ ] **Step 7: Run retrieval tests and all memory tests**

```bash
uv run pytest tests/memory -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/memory tests/memory
uv run mypy apps/control-plane/src/incidentlens_control_plane/memory
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit mixed retrieval**

```bash
git add apps/control-plane/src/incidentlens_control_plane/memory \
  tests/memory/test_hybrid_retrieval.py tests/memory/test_embedding_fallback.py
git commit -m "feat: add explainable hybrid case retrieval"
```

---

### Task 4: 将案例召回、候选假设、重新验证和自动沉淀接入两种 Agent 运行时

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/memory/integration.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/state.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/types.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/projection.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/runtime.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/baseline.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/factory.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/agent/prompts.py`
- Modify: `tests/agent/conftest.py`
- Create: `tests/agent/test_memory_integration.py`
- Modify: `tests/agent/test_runtime.py`
- Modify: `tests/agent/test_recovery.py`

**Interfaces:**
- Consumes: `HybridCaseRetriever`, `CaseService`, `CaseRepository`.
- Produces: `InvestigationMemoryCoordinator.prepare(incident_id, alert) -> MemoryPreparation`.
- Produces: `InvestigationMemoryCoordinator.finalize(state) -> InvestigationState`.
- Produces: state fields `case_id: int | None`, `case_status: str | None`.
- Produces: exact `recalled`, `adopted`, `validated`, `misleading` usage events.

- [ ] **Step 1: Write failing coordinator tests**

```python
# tests/agent/test_memory_integration.py
def test_prepare_turns_each_hit_into_a_candidate_with_traceable_id(coordinator) -> None:
    prepared = coordinator.prepare(
        "inc-current",
        {"service": "order-service", "symptom": "payment timeout"},
    )
    assert prepared.retrieved_cases
    case = prepared.retrieved_cases[0]
    hypothesis = prepared.hypotheses[0]
    assert case["hypothesis_id"] == hypothesis.id
    assert hypothesis.status == HypothesisStatus.ACTIVE
    assert hypothesis.confidence == 0.3
    assert coordinator.usage_counts("inc-current") == {
        "recalled": 1,
        "adopted": 1,
        "validated": 0,
        "misleading": 0,
    }


def test_guarded_terminal_report_validates_matching_prior(coordinator) -> None:
    state = report_ready_state(root_cause="downstream-timeout")
    state.retrieved_cases = [{
        "case_id": 1,
        "root_cause_category": "downstream-timeout",
        "hypothesis_id": "hist-1",
    }]
    finalized = coordinator.finalize(state)
    assert finalized.case_id is not None
    assert finalized.case_status == "agent_generated"
    assert coordinator.usage_counts(state.incident_id)["validated"] == 1


def test_guarded_different_cause_marks_adopted_prior_misleading(coordinator) -> None:
    state = report_ready_state(root_cause="deployment-regression")
    state.retrieved_cases = [{
        "case_id": 1,
        "root_cause_category": "downstream-timeout",
        "hypothesis_id": "hist-1",
    }]
    coordinator.finalize(state)
    events = coordinator.list_usage(state.incident_id)
    misleading = [event for event in events if event.event_type == "misleading"]
    assert misleading[0].details["accepted_evidence_ids"] == state.report["evidence_ids"]
```

- [ ] **Step 2: Write failing runtime idempotency tests**

Add tests proving:

- LLM `start` uses `prepare`;
- deterministic baseline uses the same prepared cases;
- terminal `run_round`, `load`, and `resume` expose the same `case_id`;
- repeated terminal operations produce one case and one terminal usage event;
- `needs_more_evidence` never creates a case;
- a database error during materialization leaves the guarded report intact, records
  `case_materialization_failed`, and retries on the next terminal `load`;
- prompt labels historical cases as priors with case and hypothesis IDs.

Run:

```bash
uv run pytest tests/agent/test_memory_integration.py \
  tests/agent/test_runtime.py tests/agent/test_recovery.py -q
```

Expected: FAIL because the coordinator and state fields are absent.

- [ ] **Step 3: Implement deterministic preparation and event idempotency**

Use a stable UUID5 so retries recreate the same candidate identity:

```python
def historical_hypothesis_id(incident_id: str, case_id: int) -> str:
    return str(uuid5(NAMESPACE_URL, f"incidentlens:{incident_id}:case:{case_id}"))
```

For every search hit, write:

```python
UsageEvent(
    case_id=hit.case_id,
    incident_id=incident_id,
    hypothesis_id=hypothesis_id,
    event_type=UsageEventType.RECALLED,
    idempotency_key=f"{incident_id}:{hit.case_id}:recalled",
    rank=rank,
    retrieval_mode=hit.retrieval_mode,
    lexical_score=hit.lexical_score,
    semantic_score=hit.semantic_score,
    filter_score=hit.filter_score,
    similarity_reason=hit.similarity_reason,
)
```

Then create an ACTIVE, confidence `0.3` hypothesis and write an `adopted` event with
key `f"{incident_id}:{case_id}:{hypothesis_id}:adopted"`.

- [ ] **Step 4: Implement terminal reconciliation and materialization**

`finalize` must:

1. return unchanged for non-terminal states;
2. return unchanged for `needs_more_evidence`;
3. call `CaseService.materialize_from_investigation` for `report_ready`;
4. set `state.case_id` and `state.case_status`;
5. compare every adopted case category with the evidence-gated report root cause;
6. write `validated` for equality, otherwise `misleading`;
7. include accepted current Evidence IDs in terminal event details;
8. use stable idempotency keys.

Do not classify a prior as misleading before a guarded terminal report exists.
Catch repository/materialization failures at this boundary, record only:

```python
self._audit_store.record(
    state.incident_id,
    "case_materialization_failed",
    {"error_code": "case_storage_error"},
)
```

Return the original `report_ready` state with `case_id=None`; do not replace its report
or status. A later terminal `load` calls `finalize` again and retries.

- [ ] **Step 5: Inject the coordinator into both runtimes**

Change constructors and factory to accept one coordinator. In LLM `start`, replace
direct repository search with `coordinator.prepare`. In baseline `_retrieve_memory`,
use the same preparation. In every terminal return path call:

```python
return self._memory.finalize(state)
```

For LLM `load` and terminal `resume`, finalization is safe because materialization and
usage events are idempotent. Extend `InvestigationState`, `project_investigation_state`,
and baseline checkpoint serialization with `case_id` and `case_status`.

When `prepare` observes `retriever.last_degradation_reason`, record
`memory_retrieval_degraded` with only the reason code and `retrieval_mode`; never record
provider credentials or raw requests.

- [ ] **Step 6: Update prompt rendering without exposing hidden reasoning**

Render each prior as:

```text
- case=<id> candidate_hypothesis=<id> cause=<root_cause_category>
  similarity=<similarity_reason>; this is an unverified prior
```

Add one instruction: `Historical cases are priors only; use current-incident tools and Evidence before accepting or rejecting them.`

- [ ] **Step 7: Run Agent and memory suites**

```bash
uv run pytest tests/agent tests/memory -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/agent \
  apps/control-plane/src/incidentlens_control_plane/memory tests/agent tests/memory
uv run mypy apps/control-plane/src/incidentlens_control_plane/agent \
  apps/control-plane/src/incidentlens_control_plane/memory
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit runtime memory integration**

```bash
git add apps/control-plane/src/incidentlens_control_plane/agent \
  apps/control-plane/src/incidentlens_control_plane/memory/integration.py \
  tests/agent
git commit -m "feat: connect investigations to governed memory"
```

---

### Task 5: 交付案例治理 API 与脱敏调查导出

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/cases.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/investigations.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/services/investigation_export.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `tests/web/conftest.py`
- Create: `tests/web/test_case_governance_api.py`
- Create: `tests/web/test_investigation_export.py`
- Modify: `tests/web/test_investigation_agent_api.py`

**Interfaces:**
- Consumes: `CaseService`, `HybridCaseRetriever`, engine `load`, audit store.
- Produces: all approved `/api/cases` endpoints.
- Produces: `GET /api/investigations/{incident_id}/export`.
- Produces: `InvestigationStateResponse.case_id` and `.case_status`.

- [ ] **Step 1: Write failing governance API tests**

```python
# tests/web/test_case_governance_api.py
async def test_post_case_rejects_client_selected_status(case_api_client) -> None:
    response = await case_api_client.post(
        "/api/cases",
        json={
            "status": "human_verified",
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "actor": "local-user",
        },
    )
    assert response.status_code == 422


async def test_edit_then_confirm_uses_revision(case_api_client) -> None:
    created = await create_draft(case_api_client)
    edited = await case_api_client.patch(
        f"/api/cases/{created['id']}",
        json={
            "expected_version": created["revision"],
            "actor": "reviewer",
            "reason": "correct root cause",
            "symptom": "timeout",
            "affected_services": ["order-service"],
            "root_cause_category": "downstream-timeout",
            "root_cause_description": "payment latency propagated to orders",
            "key_evidence": [{"evidence_id": "ev-1"}],
            "resolution": "remove downstream delay",
        },
    )
    confirmed = await case_api_client.post(
        f"/api/cases/{created['id']}/confirm",
        json={
            "expected_version": edited.json()["revision"],
            "actor": "reviewer",
            "reason": "evidence checked",
        },
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "human_verified"


async def test_stale_revision_is_409(case_api_client) -> None:
    created = await create_draft(case_api_client)
    response = await case_api_client.post(
        f"/api/cases/{created['id']}/reject",
        json={"expected_version": 99, "actor": "reviewer", "reason": "wrong"},
    )
    assert response.status_code == 409


async def test_search_exposes_mode_scores_and_reason(case_api_client) -> None:
    response = await case_api_client.get(
        "/api/cases/search",
        params={"q": "timeout", "service": "order-service"},
    )
    hit = response.json()["results"][0]
    assert hit["retrieval_mode"] in {"hybrid", "keyword_only"}
    assert set(hit) >= {"lexical_score", "semantic_score", "similarity_reason"}
```

- [ ] **Step 2: Write failing export tests**

```python
# tests/web/test_investigation_export.py
async def test_export_is_versioned_redacted_and_downloadable(export_client) -> None:
    response = await export_client.get("/api/investigations/inc-api/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert "attachment;" in response.headers["content-disposition"]
    body = response.json()
    assert body["schema_version"] == "incidentlens.investigation-export.v1"
    payload = json.dumps(body)
    assert "super-secret" not in payload
    assert "Authorization" not in payload


async def test_export_missing_investigation_is_404(export_client) -> None:
    response = await export_client.get("/api/investigations/missing/export")
    assert response.status_code == 404
```

- [ ] **Step 3: Run API tests and confirm routes fail**

```bash
uv run pytest tests/web/test_case_governance_api.py \
  tests/web/test_investigation_export.py -q
```

Expected: FAIL with missing endpoints or incompatible response models.

- [ ] **Step 4: Replace case route request/response models**

Implement:

```python
@router.post("", response_model=CaseResponse, status_code=201)
async def create_case(request: CreateDraftRequest) -> CaseResponse: ...

@router.get("", response_model=CaseListResponse)
async def list_cases(
    status: CaseStatus | None = None,
    incident_id: str | None = None,
    service: str | None = None,
    root_cause_category: str | None = None,
    environment: str | None = None,
    service_version: str | None = None,
    cursor: int | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> CaseListResponse: ...

@router.get("/search", response_model=CaseSearchResponse)
async def search_cases(
    q: str = Query(min_length=1, max_length=2000),
    service: str | None = None,
    root_cause_category: str | None = None,
    environment: str | None = None,
    service_version: str | None = None,
    limit: int = Query(default=10, ge=1, le=20),
) -> CaseSearchResponse: ...

@router.get("/{case_id}", response_model=CaseResponse)
async def get_case(case_id: int) -> CaseResponse: ...

@router.patch("/{case_id}", response_model=CaseResponse)
async def edit_case(case_id: int, request: EditCaseRequest) -> CaseResponse: ...

@router.post("/{case_id}/confirm", response_model=CaseResponse)
async def confirm_case(case_id: int, request: ReviewRequest) -> CaseResponse: ...

@router.post("/{case_id}/reject", response_model=CaseResponse)
async def reject_case(case_id: int, request: ReviewRequest) -> CaseResponse: ...

@router.post("/{case_id}/deprecate", response_model=CaseResponse)
async def deprecate_case(case_id: int, request: ReviewRequest) -> CaseResponse: ...

@router.post("/{case_id}/feedback", status_code=201)
async def add_feedback(case_id: int, request: FeedbackRequest) -> FeedbackResponse: ...

@router.get("/{case_id}/history", response_model=CaseHistoryResponse)
async def case_history(case_id: int) -> CaseHistoryResponse: ...
```

Map domain not-found to 404, conflict/transition to 409, and let Pydantic produce 422.

- [ ] **Step 5: Implement the export service with an exact size gate**

```python
EXPORT_SCHEMA_VERSION = "incidentlens.investigation-export.v1"
MAX_EXPORT_BYTES = 2_000_000


async def build_export(self, incident_id: str) -> dict[str, Any]:
    state = await self._engine.load(incident_id)
    if state is None:
        raise InvestigationExportNotFound(incident_id)
    payload = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "investigation": state.model_dump(mode="json"),
        "audit": self._audit_store.list_for_incident(incident_id),
        "case": self._case_service.get_by_incident(incident_id),
        "case_usage": self._case_service.list_usage(incident_id=incident_id),
    }
    safe = redact_sensitive_payload(payload)
    encoded = json.dumps(safe, separators=(",", ":")).encode()
    if len(encoded) > MAX_EXPORT_BYTES:
        raise InvestigationExportTooLarge(len(encoded))
    return safe
```

Return `413 Payload Too Large` for the size gate and a download filename
`incidentlens-{incident_id}.json`.

- [ ] **Step 6: Wire dependencies without mutable Any globals**

Use typed setters or a small route dependency container for `CaseService`,
`HybridCaseRetriever`, and `InvestigationExportService`. Extend test fixtures to inject
an in-memory repository. Ensure `create_app(engine_override=...)` supplies test
services rather than leaving production globals from a prior test.

- [ ] **Step 7: Run all Web and memory tests**

```bash
uv run pytest tests/web tests/memory -q
uv run ruff check apps/control-plane/src/incidentlens_control_plane/routes \
  apps/control-plane/src/incidentlens_control_plane/services tests/web
uv run mypy apps/control-plane/src/incidentlens_control_plane/routes \
  apps/control-plane/src/incidentlens_control_plane/services
```

Expected: all commands exit 0.

- [ ] **Step 8: Commit governance APIs and export**

```bash
git add apps/control-plane/src/incidentlens_control_plane/routes \
  apps/control-plane/src/incidentlens_control_plane/services/investigation_export.py \
  apps/control-plane/src/incidentlens_control_plane/main.py tests/web
git commit -m "feat: expose case governance and investigation export"
```

---

### Task 6: 持久化真实评测并提供三策略对比 API

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/evaluations/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/evaluations/store.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/routes/evaluations.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/metrics.py`
- Modify: `packages/evaluation/src/incidentlens_evaluation/runner.py`
- Create: `packages/evaluation/src/incidentlens_evaluation/cli.py`
- Modify: `tests/evaluation/test_metrics.py`
- Create: `tests/evaluation/test_run_store.py`
- Create: `tests/web/test_evaluation_api.py`

**Interfaces:**
- Consumes: actual `RunRecord`, actual case usage events.
- Produces: `EvaluationRunStore.start`, `record`, `complete`, `fail`, `latest_comparison`.
- Produces: `GET /api/evaluations/comparison`.
- Produces: exactly eight metrics, including `historical_case_misleading_rate`.

- [ ] **Step 1: Correct the metric contract with failing tests**

Replace the ambiguous tool `misleading_calls` metric test with:

```python
def test_historical_case_misleading_rate_uses_adopted_cases() -> None:
    result = compute_metrics([
        RunRecord(
            root_service_expected="payment-service",
            root_service_actual="payment-service",
            root_cause_type_expected="downstream-timeout",
            root_cause_type_actual="downstream-timeout",
            tool_calls=3,
            evidence_reference_correct=True,
            first_effective_round=1,
            duplicate_calls=0,
            historical_cases_adopted=2,
            historical_cases_misleading=1,
            latency_ms=100,
        )
    ])
    assert result.historical_case_misleading_rate == 0.5
```

`RunRecord` fields are:

```python
historical_cases_adopted: int = 0
historical_cases_misleading: int = 0
```

`EvaluationResult` must expose exactly:

```python
root_service_accuracy: float
root_cause_type_accuracy: float
evidence_reference_correctness: float
first_effective_hypothesis_round: float
average_tool_calls: float
duplicate_rate: float
historical_case_misleading_rate: float
average_latency_ms: float
```

- [ ] **Step 2: Write failing run-store and API tests**

```python
def test_failed_run_is_not_returned_as_completed(store) -> None:
    run_id = store.start("incidentlens_verified", "all")
    store.fail(run_id, "model_timeout")
    comparison = store.latest_comparison(scenario="all")
    assert comparison == []


async def test_comparison_returns_latest_completed_per_strategy(evaluation_client) -> None:
    response = await evaluation_client.get(
        "/api/evaluations/comparison", params={"scenario": "all"}
    )
    assert response.status_code == 200
    body = response.json()
    assert {row["strategy"] for row in body["runs"]} == {
        "react_no_memory",
        "memory_unverified",
        "incidentlens_verified",
    }
    assert set(body["runs"][0]["metrics"]) == {
        "root_service_accuracy",
        "root_cause_type_accuracy",
        "evidence_reference_correctness",
        "first_effective_hypothesis_round",
        "average_tool_calls",
        "duplicate_rate",
        "historical_case_misleading_rate",
        "average_latency_ms",
    }
```

- [ ] **Step 3: Run focused tests and confirm failures**

```bash
uv run pytest tests/evaluation/test_metrics.py \
  tests/evaluation/test_run_store.py tests/web/test_evaluation_api.py -q
```

Expected: FAIL because new fields, store, and route do not exist.

- [ ] **Step 4: Implement evaluation ORM and store transactions**

Use two tables:

```text
evaluation_runs:
  id, strategy, scenario, status, metrics_json, error_summary,
  started_at, completed_at

evaluation_run_records:
  id, run_id, scenario, record_json, created_at
```

Allow statuses `running / completed / failed`. `complete` requires at least one record,
stores metrics and completion time in one transaction, and rejects completing an
already failed run.

- [ ] **Step 5: Derive historical usage counts in the runner**

After each investigation, query usage events for its `incident_id` and set:

```python
historical_cases_adopted = sum(
    event.event_type == "adopted" for event in usage_events
)
historical_cases_misleading = sum(
    event.event_type == "misleading" for event in usage_events
)
```

Compute:

```python
total_adopted = sum(record.historical_cases_adopted for record in records)
historical_case_misleading_rate = (
    sum(record.historical_cases_misleading for record in records) / total_adopted
    if total_adopted else 0.0
)
```

Do not reuse tool empty/error counts for this metric.

- [ ] **Step 6: Persist runner success and failure**

Extend:

```python
def run_evaluation(
    strategy: str,
    scenario: str,
    *,
    store: EvaluationRunStore | None = None,
) -> EvaluationResult:
```

When a store is supplied, start before executing, record each actual `RunRecord`,
complete with `EvaluationResult`, and call `fail(run_id, safe_error_code)` before
re-raising exceptions.

- [ ] **Step 7: Add the CLI and comparison route**

`python -m incidentlens_evaluation.cli` arguments:

```text
--strategy react_no_memory|memory_unverified|incidentlens_verified|all
--scenario payment_delay|payment_error_rate|db_pool_exhaustion|
           dependency_unavailable|deployment_regression|all
--database-url sqlite:///control_plane.db
```

For `--strategy all`, run the three strategies in the documented order. Print JSON
containing run IDs and actual metrics. Never print scenario `root_cause_label`.

The API returns the latest completed run per strategy, its timestamp, records, and
metrics. With no completed data it returns `200 {"runs": []}`.

- [ ] **Step 8: Run evaluation and API tests**

```bash
uv run pytest tests/evaluation tests/web/test_evaluation_api.py -q
uv run ruff check packages/evaluation apps/control-plane/src/incidentlens_control_plane/evaluations \
  apps/control-plane/src/incidentlens_control_plane/routes/evaluations.py \
  tests/evaluation
uv run mypy packages/evaluation/src \
  apps/control-plane/src/incidentlens_control_plane/evaluations
```

Expected: all commands exit 0.

- [ ] **Step 9: Commit actual evaluation persistence**

```bash
git add apps/control-plane/src/incidentlens_control_plane/evaluations \
  apps/control-plane/src/incidentlens_control_plane/routes/evaluations.py \
  apps/control-plane/src/incidentlens_control_plane/main.py \
  packages/evaluation tests/evaluation \
  tests/web/test_evaluation_api.py
git commit -m "feat: persist actual evaluation comparisons"
```

---

### Task 7: 扩展原生 Web 页面完成知识治理操作

**Files:**
- Modify: `apps/control-plane/static/index.html`
- Modify: `apps/control-plane/static/app.js`
- Modify: `apps/control-plane/static/styles.css`
- Create: `tests/web/test_dashboard_contract.py`

**Interfaces:**
- Consumes: Tasks 5–6 的案例、导出和评测 API。
- Produces: 审核队列、编辑、反馈、检索、历史、导出和评测对比 UI。
- Preserves: 现有告警、调查时间线、工具、证据和报告面板。

- [ ] **Step 1: Write failing dashboard contract tests**

```python
# tests/web/test_dashboard_contract.py
from pathlib import Path

HTML = Path("apps/control-plane/static/index.html")
JS = Path("apps/control-plane/static/app.js")


def test_dashboard_contains_governance_regions() -> None:
    html = HTML.read_text()
    for element_id in (
        "case-review-queue",
        "case-editor",
        "case-search-form",
        "case-history",
        "case-feedback",
        "export-investigation-btn",
        "evaluation-comparison",
    ):
        assert f'id="{element_id}"' in html


def test_dashboard_uses_revision_and_real_endpoints() -> None:
    source = JS.read_text()
    assert "expected_version" in source
    assert "/api/cases/search" in source
    assert "/feedback" in source
    assert "/export" in source
    assert "/api/evaluations/comparison" in source
    assert "尚无实际运行结果" in source


def test_dashboard_never_labels_content_as_chain_of_thought() -> None:
    source = (HTML.read_text() + JS.read_text()).lower()
    assert "chain of thought" not in source
    assert "思维链" not in source
```

- [ ] **Step 2: Run the contract test and confirm missing regions**

```bash
uv run pytest tests/web/test_dashboard_contract.py -q
```

Expected: FAIL listing missing element IDs.

- [ ] **Step 3: Add semantic, accessible HTML regions**

Add:

- a review queue filtered to `agent_generated,draft`;
- a case editor with all structured fields and hidden `case-id`/`case-revision`;
- confirm/reject/deprecate actions with actor and reason;
- a search form for query, service, category, environment and version;
- result cards containing retrieval mode, component scores and similarity reason;
- five feedback buttons;
- ordered audit/feedback/usage history;
- investigation export download button;
- evaluation strategy table with eight metric rows.

Use `<label for>`, real `<button>`, table headers and `aria-live` status regions.

- [ ] **Step 4: Extract one safe JSON fetch helper**

```javascript
async function apiJson(path, options = {}) {
    const response = await fetch(`${API_BASE}${path}`, {
        ...options,
        headers: {
            'Content-Type': 'application/json',
            ...(options.headers || {}),
        },
    });
    const body = response.status === 204 ? null : await response.json();
    if (!response.ok) {
        const error = new Error(body?.detail || `HTTP ${response.status}`);
        error.status = response.status;
        error.body = body;
        throw error;
    }
    return body;
}
```

All dynamic text must use `textContent` or `escapeHtml`. Do not inject unescaped
root causes, evidence, comments, reasons or similarity text through `innerHTML`.

- [ ] **Step 5: Implement revision-safe review actions**

The editor stores the server revision. PATCH and actions send:

```javascript
{
    expected_version: selectedCase.revision,
    actor: actorInput.value || 'local-user',
    reason: reviewReason.value,
}
```

On `409`, display `案例已被其他操作更新，请重新加载`, fetch the latest detail, and
do not retry the old write automatically.

- [ ] **Step 6: Implement feedback, search, export and evaluation empty state**

- Feedback sends a deterministic browser-session idempotency key composed of case,
  incident, rating and a generated UI action UUID.
- Search renders `retrieval_mode`, scores and `similarity_reason`.
- Export uses a normal download link to the server endpoint, not client reconstruction.
- Evaluation renders three strategy columns from API data.
- When `runs` is empty, render exactly `尚无实际运行结果`.

- [ ] **Step 7: Run Web and static quality checks**

```bash
uv run pytest tests/web -q
uv run ruff check tests/web
```

Open the Compose dashboard and manually verify at desktop and 768px width:

- review queue loads;
- edit/confirm/reject/deprecate controls show server results;
- conflict message preserves server state;
- search scores and reasons remain readable;
- export downloads JSON;
- no evaluation data shows the approved empty state.

- [ ] **Step 8: Commit the governance dashboard**

```bash
git add apps/control-plane/static tests/web/test_dashboard_contract.py
git commit -m "feat: add case governance dashboard"
```

---

### Task 8: 完成 Demo 重置、Compose 闭环、质量门禁与文档

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/routes/scenarios.py`
- Modify: `packages/demo/src/incidentlens_demo/runner.py`
- Modify: `tests/demo/test_runner.py`
- Modify: `tests/test_test_topology.py`
- Create: `tests/integration/test_memory_governance_flow.py`
- Modify: `infra/compose/compose.yaml`
- Modify: `README.md`
- Modify: `docs/evaluation.md`
- Create: `docs/phase-5-live-verification.md`

**Interfaces:**
- Consumes: 完整 Phase 5 API、Agent、评测与 Web。
- Produces: 一个可复现的 Compose 知识闭环验收。
- Produces: 最终质量门禁和真实验证记录。

- [ ] **Step 1: Write the failing reset and topology tests**

Assert `DemoResetService` clears these Phase 5 tables before `case_memory`:

```python
[
    "case_feedback",
    "case_usage_events",
    "case_review_actions",
    "case_embeddings",
    "case_fts",
    "evaluation_run_records",
    "evaluation_runs",
    "case_memory",
]
```

Add `tests/integration/test_memory_governance_flow.py` to the topology parameter list
and assert it contains `pytestmark = pytest.mark.integration`.

- [ ] **Step 2: Write the Compose governance acceptance test**

The test must use public HTTP APIs:

```python
pytestmark = pytest.mark.integration


async def test_investigation_review_retrieval_feedback_and_export(compose_urls) -> None:
    async with httpx.AsyncClient(
        base_url=compose_urls["control_plane_url"]
    ) as setup_client:
        reset = await setup_client.post(
            "/api/scenarios/reset", params={"scope": "full"}
        )
        assert reset.status_code == 200

    runner = DemoRunner(
        control_plane_url=compose_urls["control_plane_url"],
        gateway_url=compose_urls["gateway_url"],
        traffic_count=5,
        compose=True,
        reset_scope="incident",
    )
    first = await runner.run("payment_delay")
    assert first.status == "passed"

    async with httpx.AsyncClient(base_url=compose_urls["control_plane_url"]) as client:
        cases = (await client.get(
            "/api/cases", params={"incident_id": first.incident_id}
        )).json()["results"]
        generated = cases[0]
        assert generated["status"] == "agent_generated"

        reviewed = await client.patch(
            f"/api/cases/{generated['id']}",
            json={
                "expected_version": generated["revision"],
                "actor": "compose-reviewer",
                "reason": "add reviewed remediation",
                "symptom": generated["symptom"],
                "affected_services": generated["affected_services"],
                "root_cause_category": generated["root_cause_category"],
                "root_cause_description": generated["root_cause_description"],
                "key_evidence": generated["key_evidence"],
                "resolution": "remove the injected payment delay",
            },
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["status"] == "draft"

        confirmed = await client.post(
            f"/api/cases/{generated['id']}/confirm",
            json={
                "expected_version": reviewed.json()["revision"],
                "actor": "compose-reviewer",
                "reason": "accepted current evidence",
            },
        )
        assert confirmed.json()["status"] == "human_verified"

        wrong = await client.post(
            "/api/cases",
            json={
                "symptom": generated["symptom"],
                "affected_services": ["order-service"],
                "root_cause_category": "deployment-regression",
                "root_cause_description": "legacy deployment hypothesis",
                "key_evidence": [{
                    "source_tool": "legacy-review",
                    "content": {"summary": "deployment preceded similar symptoms"},
                }],
                "resolution": "roll back the deployment",
                "actor": "compose-reviewer",
            },
        )
        wrong_verified = await client.post(
            f"/api/cases/{wrong.json()['id']}/confirm",
            json={
                "expected_version": wrong.json()["revision"],
                "actor": "compose-reviewer",
                "reason": "seed contradictory prior",
            },
        )
        assert wrong_verified.json()["status"] == "human_verified"

        memory_runner = DemoRunner(
            control_plane_url=compose_urls["control_plane_url"],
            gateway_url=compose_urls["gateway_url"],
            traffic_count=5,
            compose=True,
            reset_scope="incident",
            cleanup_after_run=False,
        )
        second = await memory_runner.run("payment_delay")
        history = (await client.get(
            f"/api/cases/{wrong.json()['id']}/history"
        )).json()
        assert any(
            event["event_type"] == "misleading"
            and event["incident_id"] == second.incident_id
            for event in history["usage_events"]
        )

        feedback = await client.post(
            f"/api/cases/{wrong.json()['id']}/feedback",
            json={
                "rating": "wrong",
                "actor": "compose-reviewer",
                "comment": "current evidence supports downstream timeout",
                "incident_id": second.incident_id,
                "idempotency_key": f"{second.incident_id}:wrong-feedback",
            },
        )
        assert feedback.status_code == 201

        export = await client.get(
            f"/api/investigations/{second.incident_id}/export"
        )
        assert export.status_code == 200
        assert export.json()["investigation"]["report"]["evidence_ids"]
        assert "root_cause_label" not in export.text

        cleanup = await client.post(
            "/api/scenarios/reset", params={"scope": "incident"}
        )
        assert cleanup.status_code == 200
```

- [ ] **Step 3: Run the integration test alone and confirm the missing behavior**

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest tests/integration/test_memory_governance_flow.py -m integration -vv
```

Expected before final wiring: FAIL at the first unimplemented reset, list filter, usage
event, feedback, or export assertion.

- [ ] **Step 4: Update reset and Compose configuration**

- Add `ResetScope(StrEnum)` with `FULL = "full"` and `INCIDENT = "incident"`.
- `POST /api/scenarios/reset?scope=full` clears child governance/event tables,
  `case_fts`, evaluation records and `case_memory`.
- `POST /api/scenarios/reset?scope=incident` clears scenarios, telemetry,
  investigation checkpoints and investigation audits while preserving all case,
  feedback, usage and evaluation tables.
- Keep `full` as the route and `DemoRunner` default for backward compatibility.
- Add `DemoRunner(reset_scope: Literal["full", "incident"] = "full",
  cleanup_after_run: bool = True)`; send the explicit query parameter on the initial
  reset and on the final reset when cleanup is enabled.
- Add unit tests proving `incident` preserves a confirmed case and `full` removes it.
- Add a unit test proving `cleanup_after_run=False` leaves the completed investigation
  available for export; the caller performs an explicit reset after inspection.
- Add optional Embedding configuration variables to the control plane without a
  default network provider.
- Keep `DisabledEmbeddingProvider` as the default.

- [ ] **Step 5: Run the deterministic Compose gates**

```bash
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest \
  tests/integration/test_compose_flow.py \
  tests/integration/test_scenario_acceptance.py \
  tests/integration/test_memory_governance_flow.py \
  -m integration -q
```

Expected: all deterministic Compose tests pass.

- [ ] **Step 6: Run and persist all strategy comparisons**

Run the installed evaluation package inside the control-plane container so it writes
the same `/data/control_plane.db` read by the API:

```bash
docker compose -f infra/compose/compose.yaml exec -T control-plane \
  python -m incidentlens_evaluation.cli \
  --strategy all \
  --scenario all \
  --database-url sqlite:////data/control_plane.db
```

Then:

```bash
curl -fsS "http://localhost:8003/api/evaluations/comparison?scenario=all"
```

Expected: three completed strategies; each response contains all eight metrics and no
fixed example values.

- [ ] **Step 7: Run all local quality gates**

```bash
uv run pytest -m "not integration and not live_llm" -q
uv run ruff check .
uv run mypy apps packages
git grep -n -I -E \
  '(sk-[A-Za-z0-9_-]{20,}|Bearer[[:space:]]+[A-Za-z0-9._-]{20,}|api[_-]?key[[:space:]]*=[[:space:]]*[\"'\"'][^\"'\"']+)' \
  -- ':!docs/phase-5-live-verification.md'
```

Expected: tests pass, Ruff and mypy exit 0, secret scan produces no matches.

- [ ] **Step 8: Update README and evaluation documentation**

Document:

- five case states and allowed actions;
- auto-materialization behavior;
- FTS5 default and Embedding degradation;
- review, search, feedback and export API examples;
- `python -m incidentlens_evaluation.cli` command;
- exact definition `historical_case_misleading_rate = misleading adopted cases / adopted cases`;
- dashboard workflow;
- project boundaries and no production remediation claim.

- [ ] **Step 9: Record actual Phase 5 verification**

Create `docs/phase-5-live-verification.md` only after Steps 5–7 pass. Record:

- actual UTC timestamp;
- git commit;
- deterministic Compose command and result counts;
- generated/confirmed/recalled/misleading case IDs;
- exported incident ID and schema version;
- evaluation run IDs;
- three-strategy metric response with secrets and raw prompts removed;
- Ruff, mypy, unit and secret-scan results.

Do not write example passing values.

- [ ] **Step 10: Commit Phase 5 acceptance and documentation**

```bash
git add apps/control-plane/src/incidentlens_control_plane/services/demo_reset.py \
  apps/control-plane/src/incidentlens_control_plane/routes/scenarios.py \
  packages/demo/src/incidentlens_demo/runner.py tests/demo/test_runner.py \
  tests/test_test_topology.py tests/integration/test_memory_governance_flow.py \
  infra/compose/compose.yaml README.md docs/evaluation.md \
  docs/phase-5-live-verification.md
git commit -m "test: verify phase 5 knowledge loop"
```

---

## 里程碑与退出标准

### M1：案例治理可信

- 旧案例非破坏性迁移；
- 五状态转换、revision 和审核审计通过；
- 只有 `human_verified` 进入 FTS5；
- 修改或废弃立即退出检索。

### M2：历史记忆可解释且不会绕过证据

- FTS5 始终离线可用；
- 混合检索暴露分项得分与相似理由；
- 历史案例生成可追溯的候选假设；
- guarded report 使用当前 Evidence；
- 错误历史方向被记录为 `misleading`。

### M3：产品闭环可演示

- `report_ready` 幂等产生 `agent_generated`；
- API 和 Web 能修改、确认、驳回、废弃、反馈和查询历史；
- 调查 JSON 可下载且脱敏；
- 三策略、五场景、八指标来自实际运行；
- Compose 闭环验收通过。

### Definition of Done

Phase 5 完成时，以下命令全部成功：

```bash
uv run pytest -m "not integration and not live_llm" -q
uv run ruff check .
uv run mypy apps packages
INCIDENTLENS_AGENT_MODE=deterministic_baseline \
uv run pytest \
  tests/integration/test_compose_flow.py \
  tests/integration/test_scenario_acceptance.py \
  tests/integration/test_memory_governance_flow.py \
  -m integration -q
```

并且真实验证文档包含实际运行记录，而不是预期值或手写成功率。
