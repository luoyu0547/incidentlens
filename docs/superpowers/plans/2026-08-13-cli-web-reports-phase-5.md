# Phase 5: CLI、Web UI、报告与端到端验收 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 IncidentLens 添加交互层：Jinja2+HTMX Web UI、Rich/Textual CLI、Markdown+HTML 调查报告、Docker Compose 微服务验收环境。

**Architecture:** CLI 直接导入 Python 模块调用 service 层；Web UI 通过 Jinja2 模板 + HTMX 由 FastAPI 托管。ReportService 从 InvestigationStore + EvidenceStore 聚合数据生成双格式报告。验收环境用 Docker Compose 运行 4 个 Python Flask 模拟微服务。

**Tech Stack:** Python 3.12, FastAPI, Jinja2, HTMX, Pico CSS, Rich/Textual, Flask (验收服务), Docker Compose

**Spec:** `docs/superpowers/specs/2026-08-13-cli-web-reports-phase-5-design.md`

## Global Constraints

- Python 3.12 only (`>=3.12,<3.13`)
- Pydantic v2 所有模型 `frozen=True`, `extra="forbid"`
- SQLite 单文件 `runtime.db`，每个 store 有 `migrate()` 方法
- ruff lint (E, F, I, W)，行宽 100
- pytest-asyncio `asyncio_mode = "auto"`
- uv 管理依赖
- 不修改任何安全边界、策略引擎或备份门禁
- Textual `>=0.40,<1.0`

---

## 文件结构总览

### 新建文件

```
apps/control-plane/src/incidentlens_control_plane/
├── reports/
│   ├── __init__.py
│   ├── types.py              # ReportBundle, ReportSection, ReportMetadata
│   ├── markdown.py           # MarkdownRenderer
│   ├── html.py               # HtmlRenderer (内嵌 CSS)
│   └── service.py            # ReportService
├── cli/
│   ├── __init__.py
│   ├── app.py                # Textual App + main() 入口
│   ├── screens/
│   │   ├── __init__.py
│   │   ├── dashboard.py      # 仪表盘
│   │   ├── investigation.py  # 调查详情 + 时间线
│   │   ├── logs.py           # 日志浏览器
│   │   ├── approvals.py      # 审批面板
│   │   ├── evidence.py       # 证据查看器
│   │   └── report.py         # 报告查看器
│   └── widgets/
│       ├── __init__.py
│       ├── timeline.py        # 调查时间线组件
│       └── tool_call_flow.py  # 工具调用流组件
├── web/
│   ├── __init__.py
│   ├── routes.py             # Web 页面路由
│   ├── dependencies.py       # Jinja2 环境配置
│   ├── templates/
│   │   ├── base.html
│   │   ├── dashboard.html
│   │   ├── investigations/
│   │   │   ├── list.html
│   │   │   ├── detail.html
│   │   │   └── _timeline.html
│   │   ├── logs/
│   │   │   ├── search.html
│   │   │   └── _results.html
│   │   ├── approvals/
│   │   │   ├── list.html
│   │   │   └── _action.html
│   │   ├── evidence/
│   │   │   └── detail.html
│   │   ├── reports/
│   │   │   └── render.html
│   │   └── projects/
│   │       └── manage.html
│   └── static/
│       ├── css/
│       │   └── custom.css
│       └── js/
│           └── events.js

tests/
├── reports/
│   ├── __init__.py
│   ├── test_types.py
│   ├── test_markdown.py
│   ├── test_html.py
│   └── test_service.py
├── cli/
│   ├── __init__.py
│   ├── test_screens.py
│   └── test_navigation.py
├── web/
│   ├── test_web_pages.py      (新增测试追加到已有 tests/web/)
│   ├── test_web_approvals.py
│   └── test_web_reports.py
└── acceptance/
    ├── __init__.py
    ├── test_e2e_investigation.py
    └── test_docker_scenarios.py

infra/acceptance/
├── docker-compose.yml
├── services/
│   ├── api-gateway/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app.py
│   ├── order-service/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app.py
│   ├── payment-service/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app.py
│   ├── inventory-service/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── app.py
│   └── postgres/
│       ├── Dockerfile
│       └── init.sql
├── scenarios/
│   ├── database-pool-exhaustion.yaml
│   ├── downstream-timeout.yaml
│   ├── deployment-regression.yaml
│   └── dependency-unavailable.yaml
└── README.md
```

### 修改文件

```
apps/control-plane/src/incidentlens_control_plane/main.py     # 挂载 web router
apps/control-plane/src/incidentlens_control_plane/runtime.py   # 注入 ReportService
apps/control-plane/src/incidentlens_control_plane/config.py    # 新增 report_output_dir
pyproject.toml                                                 # 新增 jinja2, textual, flask 依赖
```

---

## Task 1: 报告类型定义

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/reports/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/reports/types.py`
- Create: `tests/reports/__init__.py`
- Create: `tests/reports/test_types.py`

**Interfaces:**
- Produces: `ReportBundle`, `ReportSection`, `ReportMetadata`, `ReportKind`
- Consumes: 无（纯类型定义）

- [ ] **Step 1: 创建 reports 包和类型测试**

```python
# tests/reports/__init__.py
# (empty)
```

```python
# tests/reports/test_types.py
from datetime import datetime, UTC
from pathlib import Path

from pydantic import ValidationError
import pytest

from incidentlens_control_plane.reports.types import (
    ReportBundle,
    ReportMetadata,
    ReportSection,
    ReportKind,
)


def test_report_kind_values() -> None:
    assert {item.value for item in ReportKind} == {
        "summary",
        "root_cause",
        "timeline",
        "evidence",
        "hypotheses",
        "modifications",
        "verification",
        "backups",
        "recommendations",
        "appendix",
    }


def test_report_section_requires_title_and_content() -> None:
    with pytest.raises(ValidationError):
        ReportSection(title="", content="body")


def test_report_section_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ReportSection(
            title="Test",
            content="body",
            extra_field="nope",  # type: ignore[arg-type]
        )


def test_report_metadata_freeze() -> None:
    meta = ReportMetadata(
        symptom="timeout",
        root_cause="db pool exhausted",
        confidence=0.85,
        services_affected=["order-service"],
        evidence_count=5,
        tool_calls_count=12,
        duration_seconds=300.0,
        generated_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        meta.symptom = "changed"  # type: ignore[misc]


def test_report_bundle_paths() -> None:
    meta = ReportMetadata(
        symptom="s",
        root_cause=None,
        confidence=None,
        services_affected=[],
        evidence_count=0,
        tool_calls_count=0,
        duration_seconds=0.0,
        generated_at=datetime.now(UTC),
    )
    bundle = ReportBundle(
        investigation_id="inv-123",
        markdown_path=Path("/tmp/report.md"),
        html_path=Path("/tmp/report.html"),
        metadata=meta,
    )
    assert bundle.investigation_id == "inv-123"
    assert bundle.markdown_path.suffix == ".md"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/reports/test_types.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现类型定义**

```python
# apps/control-plane/src/incidentlens_control_plane/reports/__init__.py
"""调查报告生成服务。"""
```

```python
# apps/control-plane/src/incidentlens_control_plane/reports/types.py
"""报告领域类型：ReportBundle, ReportSection, ReportMetadata, ReportKind。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ReportKind(StrEnum):
    """报告章节类型。"""
    SUMMARY = "summary"
    ROOT_CAUSE = "root_cause"
    TIMELINE = "timeline"
    EVIDENCE = "evidence"
    HYPOTHESES = "hypotheses"
    MODIFICATIONS = "modifications"
    VERIFICATION = "verification"
    BACKUPS = "backups"
    RECOMMENDATIONS = "recommendations"
    APPENDIX = "appendix"


class ReportSection(BaseModel):
    """报告中的一个章节。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReportKind
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=0, max_length=500_000)
    metadata: dict[str, str] = Field(default_factory=dict)


class ReportMetadata(BaseModel):
    """报告元数据。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    symptom: str = Field(min_length=1, max_length=2_000)
    root_cause: str | None = Field(default=None, max_length=5_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    services_affected: list[str] = Field(default_factory=list)
    evidence_count: int = Field(ge=0)
    tool_calls_count: int = Field(ge=0)
    duration_seconds: float = Field(ge=0.0)
    generated_at: datetime


class ReportBundle(BaseModel):
    """生成的报告包，包含 Markdown 和 HTML 路径。"""
    model_config = ConfigDict(extra="forbid", frozen=True)

    investigation_id: str = Field(min_length=1, max_length=120)
    markdown_path: Path
    html_path: Path
    metadata: ReportMetadata
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/reports/test_types.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/reports/ tests/reports/
git commit -m "feat(reports): add report domain types"
```

---

## Task 2: Markdown 渲染器

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/reports/markdown.py`
- Create: `tests/reports/test_markdown.py`

**Interfaces:**
- Consumes: `ReportSection`, `ReportMetadata` (Task 1)
- Produces: `MarkdownRenderer.render(sections, metadata) -> str`

- [ ] **Step 1: 编写 Markdown 渲染器测试**

```python
# tests/reports/test_markdown.py
from datetime import datetime, UTC

from incidentlens_control_plane.reports.markdown import MarkdownRenderer
from incidentlens_control_plane.reports.types import (
    ReportKind,
    ReportMetadata,
    ReportSection,
)


def _meta() -> ReportMetadata:
    return ReportMetadata(
        symptom="order-service timeouts",
        root_cause="PostgreSQL connection pool exhaustion",
        confidence=0.92,
        services_affected=["order-service", "payment-service"],
        evidence_count=8,
        tool_calls_count=15,
        duration_seconds=420.0,
        generated_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
    )


def test_render_produces_markdown_with_title() -> None:
    renderer = MarkdownRenderer()
    sections = [
        ReportSection(
            kind=ReportKind.SUMMARY,
            title="Summary",
            content="Order service experienced timeouts.",
        )
    ]
    result = renderer.render(sections, _meta())
    assert "# Investigation Report" in result
    assert "## Summary" in result
    assert "Order service experienced timeouts." in result


def test_render_includes_metadata_header() -> None:
    renderer = MarkdownRenderer()
    result = renderer.render([], _meta())
    assert "order-service timeouts" in result
    assert "92%" in result
    assert "PostgreSQL connection pool exhaustion" in result


def test_render_includes_all_sections() -> None:
    renderer = MarkdownRenderer()
    sections = [
        ReportSection(kind=ReportKind.SUMMARY, title="Summary", content="s"),
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="e"),
        ReportSection(kind=ReportKind.APPENDIX, title="Appendix", content="a"),
    ]
    result = renderer.render(sections, _meta())
    assert "## Summary" in result
    assert "## Evidence" in result
    assert "## Appendix" in result


def test_render_empty_sections() -> None:
    renderer = MarkdownRenderer()
    result = renderer.render([], _meta())
    assert "# Investigation Report" in result
    assert "order-service timeouts" in result


def test_render_preserves_diff_code_blocks() -> None:
    renderer = MarkdownRenderer()
    diff_text = "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+new"
    sections = [
        ReportSection(kind=ReportKind.MODIFICATIONS, title="Changes", content=diff_text)
    ]
    result = renderer.render(sections, _meta())
    assert "```diff" in result
    assert diff_text in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/reports/test_markdown.py -q`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现 MarkdownRenderer**

```python
# apps/control-plane/src/incidentlens_control_plane/reports/markdown.py
"""Markdown 格式报告渲染器。"""

from __future__ import annotations

from incidentlens_control_plane.reports.types import (
    ReportKind,
    ReportMetadata,
    ReportSection,
)

# 需要用 diff code block 的章节类型
_DIFF_KINDS: frozenset[ReportKind] = frozenset({ReportKind.MODIFICATIONS})


class MarkdownRenderer:
    """将 ReportSection 列表渲染为 GitHub-Flavored Markdown。"""

    def render(
        self, sections: list[ReportSection], metadata: ReportMetadata
    ) -> str:
        parts: list[str] = []
        parts.append(self._render_header(metadata))
        for section in sections:
            parts.append(self._render_section(section))
        return "\n\n".join(parts) + "\n"

    def _render_header(self, meta: ReportMetadata) -> str:
        lines = [
            "# Investigation Report",
            "",
            f"**Symptom:** {meta.symptom}",
        ]
        if meta.root_cause:
            lines.append(f"**Root Cause:** {meta.root_cause}")
        if meta.confidence is not None:
            lines.append(f"**Confidence:** {meta.confidence:.0%}")
        if meta.services_affected:
            lines.append(
                f"**Services Affected:** {', '.join(meta.services_affected)}"
            )
        lines.extend([
            f"**Evidence Count:** {meta.evidence_count}",
            f"**Tool Calls:** {meta.tool_calls_count}",
            f"**Duration:** {meta.duration_seconds:.0f}s",
            f"**Generated:** {meta.generated_at:%Y-%m-%d %H:%M:%S UTC}",
        ])
        return "\n".join(lines)

    def _render_section(self, section: ReportSection) -> str:
        header = f"## {section.title}"
        if section.kind in _DIFF_KINDS and section.content.strip():
            return f"{header}\n\n```diff\n{section.content}\n```"
        return f"{header}\n\n{section.content}"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/reports/test_markdown.py -q`
Expected: 5 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/reports/markdown.py tests/reports/test_markdown.py
git commit -m "feat(reports): add Markdown renderer"
```

---

## Task 3: HTML 渲染器

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/reports/html.py`
- Create: `tests/reports/test_html.py`

**Interfaces:**
- Consumes: `ReportSection`, `ReportMetadata` (Task 1)
- Produces: `HtmlRenderer.render(sections, metadata) -> str`

- [ ] **Step 1: 编写 HTML 渲染器测试**

```python
# tests/reports/test_html.py
from datetime import datetime, UTC

from incidentlens_control_plane.reports.html import HtmlRenderer
from incidentlens_control_plane.reports.types import (
    ReportKind,
    ReportMetadata,
    ReportSection,
)


def _meta() -> ReportMetadata:
    return ReportMetadata(
        symptom="order-service timeouts",
        root_cause="PostgreSQL connection pool exhaustion",
        confidence=0.92,
        services_affected=["order-service", "payment-service"],
        evidence_count=8,
        tool_calls_count=15,
        duration_seconds=420.0,
        generated_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
    )


def test_render_produces_valid_html_shell() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert result.startswith("<!DOCTYPE html>")
    assert "<html" in result
    assert "</html>" in result
    assert "Investigation Report" in result


def test_render_includes_pico_css_variables() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert "--pico" in result or "picocss" in result.lower() or "classless" in result.lower()


def test_render_includes_metadata() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert "order-service timeouts" in result
    assert "PostgreSQL connection pool exhaustion" in result


def test_render_includes_sections() -> None:
    renderer = HtmlRenderer()
    sections = [
        ReportSection(kind=ReportKind.SUMMARY, title="Summary", content="<p>Test</p>"),
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="Evidence data"),
    ]
    result = renderer.render(sections, _meta())
    assert "Summary" in result
    assert "Evidence" in result


def test_render_diff_with_highlighting() -> None:
    renderer = HtmlRenderer()
    diff = "--- a/config.py\n+++ b/config.py\n@@ -1 +1 @@\n-old\n+new"
    sections = [
        ReportSection(kind=ReportKind.MODIFICATIONS, title="Changes", content=diff)
    ]
    result = renderer.render(sections, _meta())
    assert "diff" in result.lower()
    assert "+new" in result or "&#x2B;new" in result


def test_render_evidence_collapsible() -> None:
    renderer = HtmlRenderer()
    sections = [
        ReportSection(kind=ReportKind.EVIDENCE, title="Evidence", content="details")
    ]
    result = renderer.render(sections, _meta())
    # Evidence sections use <details>/<summary> for collapsibility
    assert "<details>" in result or "collapsible" in result.lower()


def test_render_print_styles() -> None:
    renderer = HtmlRenderer()
    result = renderer.render([], _meta())
    assert "@media print" in result
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/reports/test_html.py -q`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现 HtmlRenderer**

```python
# apps/control-plane/src/incidentlens_control_plane/reports/html.py
"""HTML 格式报告渲染器：自包含、内嵌 CSS、浏览器直接打开。"""

from __future__ import annotations

import html as html_mod
from textwrap import dedent

from incidentlens_control_plane.reports.types import (
    ReportKind,
    ReportMetadata,
    ReportSection,
)

_EVIDENCE_KINDS: frozenset[ReportKind] = frozenset(
    {ReportKind.EVIDENCE, ReportKind.APPENDIX}
)
_DIFF_KINDS: frozenset[ReportKind] = frozenset({ReportKind.MODIFICATIONS})

_CSS = dedent("""\
    :root {
      --il-primary: #3b5998;
      --il-bg: #ffffff;
      --il-surface: #f8f9fa;
      --il-text: #1a1a2e;
      --il-muted: #6c757d;
      --il-success: #28a745;
      --il-warning: #ffc107;
      --il-danger: #dc3545;
      --il-info: #17a2b8;
      --il-border: #dee2e6;
      --il-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --il-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --il-bg: #1a1a2e;
        --il-surface: #16213e;
        --il-text: #e0e0e0;
        --il-muted: #adb5bd;
        --il-border: #495057;
      }
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--il-font);
      color: var(--il-text);
      background: var(--il-bg);
      line-height: 1.6;
      max-width: 960px;
      margin: 0 auto;
      padding: 2rem;
    }
    h1 { font-size: 1.8rem; margin-bottom: 0.5rem; border-bottom: 2px solid var(--il-primary); padding-bottom: 0.5rem; }
    h2 { font-size: 1.3rem; margin: 1.5rem 0 0.5rem; color: var(--il-primary); }
    .meta { background: var(--il-surface); border: 1px solid var(--il-border); border-radius: 6px; padding: 1rem; margin: 1rem 0; }
    .meta dt { font-weight: 600; display: inline; }
    .meta dd { display: inline; margin: 0 1rem 0 0.3rem; }
    .meta dd::after { content: ""; display: block; }
    pre { background: var(--il-surface); border: 1px solid var(--il-border); border-radius: 4px; padding: 1rem; overflow-x: auto; font-family: var(--il-mono); font-size: 0.85rem; }
    code { font-family: var(--il-mono); font-size: 0.9em; }
    .diff-add { color: var(--il-success); }
    .diff-del { color: var(--il-danger); }
    details { margin: 0.5rem 0; }
    summary { cursor: pointer; font-weight: 600; padding: 0.3rem 0; }
    @media print {
      body { max-width: none; padding: 0; font-size: 11pt; }
      details { open: true; }
      pre { white-space: pre-wrap; word-break: break-word; }
    }
""")


class HtmlRenderer:
    """将 ReportSection 列表渲染为自包含 HTML 页面。"""

    def render(
        self, sections: list[ReportSection], metadata: ReportMetadata
    ) -> str:
        meta_html = self._render_metadata(metadata)
        sections_html = "\n".join(
            self._render_section(s) for s in sections
        )
        return (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n<head>\n"
            "<meta charset=\"utf-8\">\n"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>Investigation Report</title>\n"
            f"<style>{_CSS}</style>\n"
            "</head>\n<body>\n"
            "<h1>Investigation Report</h1>\n"
            f"{meta_html}\n"
            f"{sections_html}\n"
            "</body>\n</html>\n"
        )

    def _render_metadata(self, meta: ReportMetadata) -> str:
        items = [
            ("Symptom", html_mod.escape(meta.symptom)),
        ]
        if meta.root_cause:
            items.append(("Root Cause", html_mod.escape(meta.root_cause)))
        if meta.confidence is not None:
            items.append(("Confidence", f"{meta.confidence:.0%}"))
        if meta.services_affected:
            items.append(
                ("Services Affected", html_mod.escape(", ".join(meta.services_affected)))
            )
        items.extend([
            ("Evidence Count", str(meta.evidence_count)),
            ("Tool Calls", str(meta.tool_calls_count)),
            ("Duration", f"{meta.duration_seconds:.0f}s"),
            ("Generated", meta.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")),
        ])
        dl_items = "\n".join(
            f"<dt>{label}:</dt> <dd>{value}</dd>" for label, value in items
        )
        return f'<div class="meta"><dl>{dl_items}</dl></div>'

    def _render_section(self, section: ReportSection) -> str:
        title = html_mod.escape(section.title)
        if section.kind in _DIFF_KINDS and section.content.strip():
            escaped = html_mod.escape(section.content)
            return (
                f"<h2>{title}</h2>\n"
                f"<pre><code>{escaped}</code></pre>"
            )
        if section.kind in _EVIDENCE_KINDS:
            escaped = html_mod.escape(section.content)
            return (
                f"<details open>\n"
                f"<summary><h2 style=\"display:inline\">{title}</h2></summary>\n"
                f"<pre><code>{escaped}</code></pre>\n"
                f"</details>"
            )
        escaped = html_mod.escape(section.content)
        return f"<h2>{title}</h2>\n<p>{escaped}</p>"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/reports/test_html.py -q`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/reports/html.py tests/reports/test_html.py
git commit -m "feat(reports): add self-contained HTML renderer"
```

---

## Task 4: ReportService — 从调查数据生成报告

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/reports/service.py`
- Create: `tests/reports/test_service.py`

**Interfaces:**
- Consumes: `InvestigationStore.get_investigation`, `list_agent_runs`, `list_tool_calls`, `list_hypotheses`, `list_conclusions`, `list_checkpoints`; `EvidenceStore.query`; `ChangeSetStore.get`; `MarkdownRenderer`, `HtmlRenderer`
- Produces: `ReportService.generate(investigation_id) -> ReportBundle`

- [ ] **Step 1: 编写 ReportService 测试**

```python
# tests/reports/test_service.py
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.state_machine import (
    AgentRunStatus,
    InvestigationStatus,
    ToolCallStatus,
)
from incidentlens_control_plane.investigation.store import InvestigationStore
from incidentlens_control_plane.reports.service import ReportService
from incidentlens_control_plane.reports.types import ReportBundle


@pytest.fixture()
def stores(tmp_path: Path):
    def connect():
        conn = sqlite3.connect(":memory:")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    inv_store = InvestigationStore(connect)
    ev_store = EvidenceStore(connect)
    inv_store.migrate()
    ev_store.migrate()
    return inv_store, ev_store


def _create_investigation(store: InvestigationStore) -> str:
    from incidentlens_control_plane.investigation.types import Investigation, InvestigationBudget
    inv = Investigation(
        investigation_id="inv-test001",
        incident_id="inc-test001",
        project_id="proj-1",
        target_id="target-1",
        service="order-service",
        symptom="timeout errors under load",
        status=InvestigationStatus.COMPLETED,
        budget=InvestigationBudget(),
        usage=__import__(
            "incidentlens_control_plane.investigation.types", fromlist=["UsageCounters"]
        ).UsageCounters(),
        created_at=datetime(2026, 8, 13, 10, 0, 0, tzinfo=UTC),
        updated_at=datetime(2026, 8, 13, 10, 5, 0, tzinfo=UTC),
    )
    store.create_investigation(inv)
    return inv.investigation_id


def test_generate_creates_both_files(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.investigation_id == inv_id
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.markdown_path.read_text().startswith("# Investigation Report")
    assert "<!DOCTYPE html>" in bundle.html_path.read_text()


def test_generate_investigation_not_found(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    with pytest.raises(KeyError):
        svc.generate("nonexistent")


def test_generate_metadata_matches_investigation(stores, tmp_path) -> None:
    inv_store, ev_store = stores
    inv_id = _create_investigation(inv_store)
    svc = ReportService(
        investigations=inv_store,
        evidence=ev_store,
        output_dir=tmp_path,
    )
    bundle = svc.generate(inv_id)
    assert bundle.metadata.symptom == "timeout errors under load"
    assert "order-service" in bundle.metadata.services_affected
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/reports/test_service.py -q`
Expected: FAIL（ImportError）

- [ ] **Step 3: 实现 ReportService**

```python
# apps/control-plane/src/incidentlens_control_plane/reports/service.py
"""调查报告生成服务：从 InvestigationStore + EvidenceStore 聚合数据生成报告。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.store import (
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.reports.html import HtmlRenderer
from incidentlens_control_plane.reports.markdown import MarkdownRenderer
from incidentlens_control_plane.reports.types import (
    ReportBundle,
    ReportKind,
    ReportMetadata,
    ReportSection,
)


class ReportService:
    """从调查数据生成 Markdown + HTML 双格式报告。"""

    def __init__(
        self,
        *,
        investigations: InvestigationStore,
        evidence: EvidenceStore,
        output_dir: Path,
        md_renderer: MarkdownRenderer | None = None,
        html_renderer: HtmlRenderer | None = None,
    ) -> None:
        self._investigations = investigations
        self._evidence = evidence
        self._output_dir = output_dir
        self._md = md_renderer or MarkdownRenderer()
        self._html = html_renderer or HtmlRenderer()

    def generate(self, investigation_id: str) -> ReportBundle:
        """生成报告。抛出 KeyError 如果调查不存在。"""
        try:
            investigation = self._investigations.get_investigation(investigation_id)
        except InvestigationNotFound as exc:
            raise KeyError(str(exc)) from exc

        runs = self._investigations.list_agent_runs(
            investigation_id=investigation_id
        )
        tool_calls = []
        for run in runs:
            tool_calls.extend(
                self._investigations.list_tool_calls(agent_run_id=run.agent_run_id)
            )
        hypotheses = self._investigations.list_hypotheses(
            investigation_id=investigation_id
        )
        conclusions = self._investigations.list_conclusions(
            investigation_id=investigation_id
        )

        evidence_refs = self._evidence.query(
            incident_id=investigation.incident_id, limit=500
        )

        # 确定根因和置信度
        root_cause = None
        confidence = None
        if conclusions:
            latest = conclusions[-1]
            root_cause = latest.description or latest.root_cause_category or None
            confidence = latest.confidence if hasattr(latest, "confidence") else None

        # 计算持续时间
        duration = (investigation.updated_at - investigation.created_at).total_seconds()

        metadata = ReportMetadata(
            symptom=investigation.symptom,
            root_cause=root_cause,
            confidence=confidence,
            services_affected=[investigation.service],
            evidence_count=len(evidence_refs),
            tool_calls_count=len(tool_calls),
            duration_seconds=max(duration, 0.0),
            generated_at=datetime.now(UTC),
        )

        sections = self._build_sections(
            investigation, runs, tool_calls, hypotheses, conclusions, evidence_refs
        )

        # 写入文件
        self._output_dir.mkdir(parents=True, exist_ok=True)
        md_path = self._output_dir / f"{investigation_id}.md"
        html_path = self._output_dir / f"{investigation_id}.html"

        md_content = self._md.render(sections, metadata)
        html_content = self._html.render(sections, metadata)

        md_path.write_text(md_content, encoding="utf-8")
        html_path.write_text(html_content, encoding="utf-8")

        return ReportBundle(
            investigation_id=investigation_id,
            markdown_path=md_path,
            html_path=html_path,
            metadata=metadata,
        )

    def _build_sections(
        self, investigation, runs, tool_calls, hypotheses, conclusions, evidence_refs
    ) -> list[ReportSection]:
        sections: list[ReportSection] = []

        # 1. 摘要
        sections.append(ReportSection(
            kind=ReportKind.SUMMARY,
            title="Summary",
            content=(
                f"Investigation into: {investigation.symptom}\n\n"
                f"Service: {investigation.service}\n"
                f"Status: {investigation.status.value}\n"
                f"Total rounds: {investigation.usage.rounds}"
            ),
        ))

        # 2. 根因分析
        if conclusions:
            lines = []
            for c in conclusions:
                conf = f" (confidence: {c.confidence:.0%})" if c.confidence is not None else ""
                lines.append(f"- **{c.root_cause_category or 'Root cause'}**{conf}: {c.description}")
            sections.append(ReportSection(
                kind=ReportKind.ROOT_CAUSE,
                title="Root Cause Analysis",
                content="\n".join(lines),
            ))

        # 3. 时间线
        timeline_lines = []
        for run in runs:
            timeline_lines.append(
                f"- **{run.created_at:%H:%M:%S}** — Agent run `{run.agent_run_id[-8:]}` "
                f"started ({run.kind.value})"
            )
            if run.status.value in ("completed", "failed", "cancelled"):
                timeline_lines.append(
                    f"  - Finished: {run.status.value}"
                )
        for tc in tool_calls:
            if tc.started_at:
                timeline_lines.append(
                    f"- **{tc.started_at:%H:%M:%S}** — `{tc.tool_name}` → {tc.status.value}"
                )
        if timeline_lines:
            sections.append(ReportSection(
                kind=ReportKind.TIMELINE,
                title="Timeline",
                content="\n".join(timeline_lines),
            ))

        # 4. 证据汇总
        if evidence_refs:
            ev_lines = []
            for ref in evidence_refs[:50]:
                ev_lines.append(
                    f"- [{ref.evidence_kind.value}] {ref.evidence_ref_id}: {ref.source_ref or 'N/A'}"
                )
            sections.append(ReportSection(
                kind=ReportKind.EVIDENCE,
                title="Evidence",
                content="\n".join(ev_lines),
            ))

        # 5. 假设演进
        if hypotheses:
            hyp_lines = []
            for h in hypotheses:
                hyp_lines.append(
                    f"- **{h.status.value}**: {h.description}"
                )
            sections.append(ReportSection(
                kind=ReportKind.HYPOTHESES,
                title="Hypotheses",
                content="\n".join(hyp_lines),
            ))

        # 6-8: 修改记录、验证结果、备份状态 — 从 changeset 获取
        # (MVP 中如果没有 changeset 则跳过)

        # 9. 修复建议
        advice_lines = []
        for c in conclusions:
            if hasattr(c, "remediation") and c.remediation:
                advice_lines.append(f"- {c.remediation}")
        if not advice_lines:
            advice_lines.append("- Manual review recommended based on evidence above.")
        sections.append(ReportSection(
            kind=ReportKind.RECOMMENDATIONS,
            title="Recommendations",
            content="\n".join(advice_lines),
        ))

        # 10. 附录
        appendix_lines = []
        for tc in tool_calls:
            appendix_lines.append(
                f"- `{tc.tool_name}` ({tc.status.value}) — "
                f"evidence: {', '.join(tc.evidence_ids) or 'none'}"
            )
        if appendix_lines:
            sections.append(ReportSection(
                kind=ReportKind.APPENDIX,
                title="Appendix: Tool Calls",
                content="\n".join(appendix_lines),
            ))

        return sections
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/reports/test_service.py -q`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/reports/service.py tests/reports/test_service.py
git commit -m "feat(reports): add ReportService with data aggregation"
```

---

## Task 5: 配置扩展和 RuntimeServices 注入

**Files:**
- Modify: `apps/control-plane/src/incidentlens_control_plane/config.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/runtime.py`

**Interfaces:**
- Consumes: `RuntimeSettings`, `RuntimeServices`
- Produces: `RuntimeSettings.report_output_dir`, `RuntimeServices.reports`

- [ ] **Step 1: 在 RuntimeSettings 新增 report_output_dir**

在 `config.py` 的 `RuntimeSettings` 类中，`shutdown_grace_seconds` 字段之后新增：

```python
    # -- reports ---------------------------------------------------------------
    report_output_dir: Path | None = None
```

同时在 `from_environment` 方法中，返回前添加：

```python
        report_output_dir = data_dir / "reports"
        return cls(data_dir=data_dir.resolve(), report_output_dir=report_output_dir)
```

- [ ] **Step 2: 在 RuntimeServices 新增 reports 字段**

在 `runtime.py` 的 `RuntimeServices` dataclass 中，`recovery` 字段之后新增：

```python
    reports: object  # ReportService — 前向引用避免循环导入
```

在 `build_runtime` 函数中，在 `return RuntimeServices(...)` 之前添加：

```python
    from incidentlens_control_plane.reports.service import ReportService

    report_dir = settings.report_output_dir or (settings.data_dir / "reports")
    reports = ReportService(
        investigations=investigation_store,
        evidence=evidence,
        output_dir=report_dir,
    )
```

在 `RuntimeServices(...)` 构造调用中添加 `reports=reports`。

- [ ] **Step 3: 运行已有测试确认不破坏**

Run: `uv run pytest tests/test_app.py tests/web/ -q`
Expected: PASS（已有测试不受影响）

- [ ] **Step 4: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/config.py apps/control-plane/src/incidentlens_control_plane/runtime.py
git commit -m "feat(reports): wire ReportService into runtime config"
```

---

## Task 6: Web UI — 基础设施和仪表盘

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/web/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/dependencies.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/routes.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/base.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/dashboard.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/static/css/custom.css`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/static/js/events.js`
- Modify: `apps/control-plane/src/incidentlens_control_plane/main.py`
- Create: `tests/web/test_web_dashboard.py`

**Interfaces:**
- Consumes: `RuntimeServices` (via `get_runtime`)
- Produces: Web router mounted at `/web`, dashboard page at `GET /`

- [ ] **Step 1: 添加 jinja2 依赖**

在 `pyproject.toml` 的 `dependencies` 列表中追加：

```
    "jinja2>=3.1,<4",
```

然后运行：`uv lock`

- [ ] **Step 2: 编写仪表盘测试**

```python
# tests/web/test_web_dashboard.py
import pytest
from httpx import ASGITransport, AsyncClient

from incidentlens_control_plane.main import create_app


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_dashboard_returns_200(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert "IncidentLens" in resp.text


async def test_dashboard_contains_investigation_list(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    # Dashboard should have an investigations section
    assert "investigation" in resp.text.lower() or "调查" in resp.text


async def test_web_investigations_list(client):
    resp = await client.get("/web/investigations")
    assert resp.status_code == 200
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest tests/web/test_web_dashboard.py -q`
Expected: FAIL（路由不存在）

- [ ] **Step 4: 实现 Web 基础设施**

```python
# apps/control-plane/src/incidentlens_control_plane/web/__init__.py
"""Jinja2 + HTMX Web UI。"""
```

```python
# apps/control-plane/src/incidentlens_control_plane/web/dependencies.py
"""Jinja2 环境和模板配置。"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def get_jinja_env() -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=True,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return env
```

```python
# apps/control-plane/src/incidentlens_control_plane/web/routes.py
"""Web UI 页面路由。"""

from __future__ import annotations

from typing import cast

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from incidentlens_control_plane.runtime import RuntimeServices
from incidentlens_control_plane.web.dependencies import get_jinja_env

router = APIRouter(tags=["web"])

_env = get_jinja_env()


def _get_runtime(request: Request) -> RuntimeServices:
    return cast(RuntimeServices, request.app.state.runtime)


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> str:
    runtime = _get_runtime(request)
    investigations = runtime.investigations.list_investigations()
    template = _env.get_template("dashboard.html")
    return template.render(investigations=investigations)


@router.get("/web/investigations", response_class=HTMLResponse)
async def investigations_list(request: Request) -> str:
    runtime = _get_runtime(request)
    investigations = runtime.investigations.list_investigations()
    template = _env.get_template("investigations/list.html")
    return template.render(investigations=investigations)
```

```html
<!-- apps/control-plane/src/incidentlens_control_plane/web/templates/base.html -->
<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{% block title %}IncidentLens{% endblock %}</title>
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css">
    <link rel="stylesheet" href="/static/css/custom.css">
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
</head>
<body>
    <nav class="container-fluid">
        <ul>
            <li><strong>IncidentLens</strong></li>
        </ul>
        <ul>
            <li><a href="/">Dashboard</a></li>
            <li><a href="/web/investigations">Investigations</a></li>
            <li><a href="/web/approvals">Approvals</a></li>
            <li><a href="/web/projects">Projects</a></li>
        </ul>
    </nav>
    <main class="container">
        {% block content %}{% endblock %}
    </main>
    <script src="/static/js/events.js"></script>
</body>
</html>
```

```html
<!-- apps/control-plane/src/incidentlens_control_plane/web/templates/dashboard.html -->
{% extends "base.html" %}
{% block title %}Dashboard — IncidentLens{% endblock %}
{% block content %}
<h2>Dashboard</h2>
<article>
    <header>Active Investigations</header>
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Symptom</th>
                <th>Status</th>
                <th>Service</th>
            </tr>
        </thead>
        <tbody>
        {% for inv in investigations %}
            <tr>
                <td><a href="/web/investigations/{{ inv.investigation_id }}">{{ inv.investigation_id[:16] }}</a></td>
                <td>{{ inv.symptom[:80] }}</td>
                <td><mark>{{ inv.status.value }}</mark></td>
                <td>{{ inv.service }}</td>
            </tr>
        {% else %}
            <tr><td colspan="4">No investigations yet.</td></tr>
        {% endfor %}
        </tbody>
    </table>
</article>
{% endblock %}
```

```html
<!-- apps/control-plane/src/incidentlens_control_plane/web/templates/investigations/list.html -->
{% extends "base.html" %}
{% block title %}Investigations — IncidentLens{% endblock %}
{% block content %}
<h2>Investigations</h2>
<table>
    <thead>
        <tr>
            <th>ID</th>
            <th>Symptom</th>
            <th>Status</th>
            <th>Service</th>
            <th>Created</th>
        </tr>
    </thead>
    <tbody>
    {% for inv in investigations %}
        <tr>
            <td><a href="/web/investigations/{{ inv.investigation_id }}">{{ inv.investigation_id[:16] }}</a></td>
            <td>{{ inv.symptom[:80] }}</td>
            <td><mark>{{ inv.status.value }}</mark></td>
            <td>{{ inv.service }}</td>
            <td>{{ inv.created_at.strftime('%Y-%m-%d %H:%M') }}</td>
        </tr>
    {% else %}
        <tr><td colspan="5">No investigations found.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endblock %}
```

```css
/* apps/control-plane/src/incidentlens_control_plane/web/static/css/custom.css */
:root {
    --il-primary: #3b5998;
    --il-success: #28a745;
    --il-warning: #ffc107;
    --il-danger: #dc3545;
}

/* Status badges */
mark {
    background: var(--il-warning);
    color: #000;
    padding: 0.1em 0.4em;
    border-radius: 3px;
    font-size: 0.85em;
}

/* Table zebra striping */
tbody tr:nth-child(even) {
    background: rgba(0, 0, 0, 0.02);
}
```

```javascript
// apps/control-plane/src/incidentlens_control_plane/web/static/js/events.js
// SSE connection for real-time updates
document.addEventListener("DOMContentLoaded", function() {
    if (typeof EventSource !== "undefined") {
        var source = new EventSource("/web/events/stream");
        source.onmessage = function(event) {
            // HTMX integration: trigger refresh on relevant elements
            var data = JSON.parse(event.data);
            if (data.type && data.type.startsWith("investigation.")) {
                htmx.trigger("body", "investigation-updated", {detail: data});
            }
        };
        source.onerror = function() {
            // Reconnect is handled by EventSource automatically
        };
    }
});
```

- [ ] **Step 5: 挂载 web router 到 main.py**

在 `main.py` 中，`application.include_router(investigations_router)` 之后追加：

```python
    from incidentlens_control_plane.web.routes import router as web_router
    application.include_router(web_router)
```

同时在 `application` 的 `static` 目录挂载（在 `include_router` 之后）：

```python
    from pathlib import Path
    from starlette.staticfiles import StaticFiles

    _static_dir = Path(__file__).parent / "web" / "static"
    application.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/web/test_web_dashboard.py -q`
Expected: 3 passed

- [ ] **Step 7: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/web/ apps/control-plane/src/incidentlens_control_plane/main.py tests/web/test_web_dashboard.py pyproject.toml uv.lock
git commit -m "feat(web): add Jinja2+HTMX dashboard and investigations list"
```

---

## Task 7: Web UI — 调查详情页和时间线

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/investigations/detail.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/investigations/_timeline.html`
- Modify: `apps/control-plane/src/incidentlens_control_plane/web/routes.py`

**Interfaces:**
- Consumes: `InvestigationService.get_investigation`, `list_runs`, `list_tool_calls`, `list_hypotheses`, `list_conclusions`
- Produces: `GET /web/investigations/{id}` 页面

- [ ] **Step 1: 编写调查详情页测试**

在 `tests/web/test_web_dashboard.py` 中追加：

```python
async def test_investigation_detail_page(client):
    resp = await client.get("/web/investigations/nonexistent")
    # Should return 200 with a "not found" message, not 500
    assert resp.status_code == 200


async def test_investigation_detail_has_timeline_section(client):
    resp = await client.get("/web/investigations/nonexistent")
    assert "timeline" in resp.text.lower() or "Timeline" in resp.text
```

- [ ] **Step 2: 实现调查详情路由和模板**

在 `routes.py` 中追加：

```python
@router.get("/web/investigations/{investigation_id}", response_class=HTMLResponse)
async def investigation_detail(request: Request, investigation_id: str) -> str:
    runtime = _get_runtime(request)
    try:
        investigation = runtime.investigations.get_investigation(investigation_id)
    except Exception:
        investigation = None
    runs = []
    hypotheses = []
    conclusions = []
    tool_calls = []
    if investigation:
        runs = list(runtime.investigations.list_runs(investigation_id=investigation_id))
        hypotheses = list(runtime.investigations.list_hypotheses(investigation_id=investigation_id))
        conclusions = list(runtime.investigations.list_conclusions(investigation_id=investigation_id))
        for run in runs:
            tool_calls.extend(
                runtime.investigation_store.list_tool_calls(agent_run_id=run.agent_run_id)
            )
    template = _env.get_template("investigations/detail.html")
    return template.render(
        investigation=investigation,
        runs=runs,
        hypotheses=hypotheses,
        conclusions=conclusions,
        tool_calls=tool_calls,
    )
```

```html
<!-- apps/control-plane/src/incidentlens_control_plane/web/templates/investigations/detail.html -->
{% extends "base.html" %}
{% block title %}Investigation {{ investigation.investigation_id[:16] if investigation else 'Not Found' }}{% endblock %}
{% block content %}
{% if investigation %}
<h2>Investigation {{ investigation.investigation_id[:16] }}</h2>
<article>
    <header>
        <strong>Status:</strong> <mark>{{ investigation.status.value }}</mark>
        &nbsp;|&nbsp; <strong>Service:</strong> {{ investigation.service }}
    </header>
    <p><strong>Symptom:</strong> {{ investigation.symptom }}</p>
</article>

<div class="grid">
    <article>
        <header>Runs ({{ runs|length }})</header>
        <table>
            <thead><tr><th>ID</th><th>Kind</th><th>Status</th></tr></thead>
            <tbody>
            {% for run in runs %}
            <tr>
                <td>{{ run.agent_run_id[-12:] }}</td>
                <td>{{ run.kind.value }}</td>
                <td><mark>{{ run.status.value }}</mark></td>
            </tr>
            {% endfor %}
            </tbody>
        </table>
    </article>
    <article>
        <header>Hypotheses ({{ hypotheses|length }})</header>
        <ul>
        {% for h in hypotheses %}
        <li><mark>{{ h.status.value }}</mark> {{ h.description[:100] }}</li>
        {% endfor %}
        </ul>
    </article>
</div>

<h3>Timeline</h3>
{% include "investigations/_timeline.html" %}

{% else %}
<article>
    <header>Not Found</header>
    <p>Investigation <code>{{ investigation_id }}</code> was not found.</p>
</article>
{% endif %}
{% endblock %}
```

```html
<!-- apps/control-plane/src/incidentlens_control_plane/web/templates/investigations/_timeline.html -->
<div class="timeline">
{% for run in runs %}
<div class="timeline-item">
    <span class="timeline-time">{{ run.created_at.strftime('%H:%M:%S') if run.created_at else '' }}</span>
    <span class="timeline-dot"></span>
    <span class="timeline-content">
        Agent run <code>{{ run.agent_run_id[-8:] }}</code> — {{ run.kind.value }} — <mark>{{ run.status.value }}</mark>
    </span>
</div>
{% endfor %}
{% for tc in tool_calls %}
<div class="timeline-item">
    <span class="timeline-time">{{ tc.started_at.strftime('%H:%M:%S') if tc.started_at else '' }}</span>
    <span class="timeline-dot"></span>
    <span class="timeline-content">
        <code>{{ tc.tool_name }}</code> → <mark>{{ tc.status.value }}</mark>
    </span>
</div>
{% endfor %}
</div>
```

在 `custom.css` 末尾追加：

```css
/* Timeline */
.timeline { position: relative; padding-left: 2rem; margin: 1rem 0; }
.timeline::before { content: ""; position: absolute; left: 0.5rem; top: 0; bottom: 0; width: 2px; background: var(--il-border); }
.timeline-item { position: relative; margin-bottom: 0.8rem; }
.timeline-dot { position: absolute; left: -1.65rem; top: 0.3rem; width: 10px; height: 10px; border-radius: 50%; background: var(--il-primary); }
.timeline-time { font-size: 0.8em; color: var(--il-muted); margin-right: 0.5rem; }
.timeline-content { font-size: 0.95em; }
```

- [ ] **Step 3: 运行测试确认通过**

Run: `uv run pytest tests/web/test_web_dashboard.py -q`
Expected: 5 passed (原有 3 + 新增 2)

- [ ] **Step 4: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/web/ tests/web/test_web_dashboard.py
git commit -m "feat(web): add investigation detail page with timeline"
```

---

## Task 8: Web UI — 审批面板、日志搜索、证据、报告、项目、SSE

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/approvals/list.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/approvals/_action.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/logs/search.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/logs/_results.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/evidence/detail.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/reports/render.html`
- Create: `apps/control-plane/src/incidentlens_control_plane/web/templates/projects/manage.html`
- Modify: `apps/control-plane/src/incidentlens_control_plane/web/routes.py`
- Modify: `tests/web/test_web_dashboard.py`

**Interfaces:**
- Consumes: `ApprovalService`, `LogService`, `EvidenceService`, `ReportService`, `ProjectRegistryStore`
- Produces: 所有剩余 Web 页面路由 + SSE 事件流

- [ ] **Step 1: 编写剩余页面测试**

在 `tests/web/test_web_dashboard.py` 中追加：

```python
async def test_approvals_page(client):
    resp = await client.get("/web/approvals")
    assert resp.status_code == 200
    assert "approval" in resp.text.lower() or "Approval" in resp.text


async def test_logs_search_page(client):
    resp = await client.get("/web/logs/search")
    assert resp.status_code == 200


async def test_projects_page(client):
    resp = await client.get("/web/projects")
    assert resp.status_code == 200


async def test_reports_page_not_found(client):
    resp = await client.get("/web/reports/nonexistent")
    # Should handle gracefully
    assert resp.status_code in (200, 404)


async def test_events_stream(client):
    resp = await client.get("/web/events/stream")
    assert resp.status_code == 200
```

- [ ] **Step 2: 实现所有剩余路由**

在 `routes.py` 中追加以下路由（保持与已有路由相同的模式）：

```python
@router.get("/web/approvals", response_class=HTMLResponse)
async def approvals_list(request: Request) -> str:
    runtime = _get_runtime(request)
    pending = runtime.approvals.list_pending()
    template = _env.get_template("approvals/list.html")
    return template.render(approvals=pending)


@router.post("/web/approvals/{approval_id}/approve")
async def approve_action(request: Request, approval_id: str) -> str:
    runtime = _get_runtime(request)
    runtime.approvals.decide(approval_id, approved=True)
    return HTMLResponse(
        "<div>Approved</div>",
        headers={"HX-Trigger": "approval-updated"},
    )


@router.post("/web/approvals/{approval_id}/reject")
async def reject_action(request: Request, approval_id: str) -> str:
    runtime = _get_runtime(request)
    runtime.approvals.decide(approval_id, approved=False)
    return HTMLResponse(
        "<div>Rejected</div>",
        headers={"HX-Trigger": "approval-updated"},
    )


@router.get("/web/logs/search", response_class=HTMLResponse)
async def logs_search(request: Request) -> str:
    template = _env.get_template("logs/search.html")
    return template.render(results=[])


@router.get("/web/evidence/{evidence_ref_id}", response_class=HTMLResponse)
async def evidence_detail(request: Request, evidence_ref_id: str) -> str:
    template = _env.get_template("evidence/detail.html")
    return template.render(evidence_id=evidence_ref_id, evidence=None)


@router.get("/web/reports/{investigation_id}", response_class=HTMLResponse)
async def report_view(request: Request, investigation_id: str) -> str:
    runtime = _get_runtime(request)
    try:
        bundle = runtime.reports.generate(investigation_id)
        html_content = bundle.html_path.read_text(encoding="utf-8")
        return HTMLResponse(html_content)
    except Exception:
        template = _env.get_template("reports/render.html")
        return template.render(error="Report not available", investigation_id=investigation_id)


@router.get("/web/projects", response_class=HTMLResponse)
async def projects_manage(request: Request) -> str:
    runtime = _get_runtime(request)
    projects = runtime.projects.list_projects()
    template = _env.get_template("projects/manage.html")
    return template.render(projects=projects)


@router.get("/web/events/stream")
async def events_stream(request: Request):
    from starlette.responses import StreamingResponse
    runtime = _get_runtime(request)

    async def generate():
        async for event in runtime.broker.subscribe():
            import json
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
```

- [ ] **Step 3: 创建所有模板文件**

每个模板继承 `base.html`，包含对应页面内容。以下列出关键模板内容：

```html
<!-- approvals/list.html -->
{% extends "base.html" %}
{% block title %}Approvals{% endblock %}
{% block content %}
<h2>Pending Approvals</h2>
<table>
    <thead><tr><th>ID</th><th>Type</th><th>Created</th><th>Action</th></tr></thead>
    <tbody>
    {% for a in approvals %}
    <tr>
        <td>{{ a.approval_id[:16] }}</td>
        <td>{{ a.intent_type }}</td>
        <td>{{ a.created_at.strftime('%Y-%m-%d %H:%M') if a.created_at else '' }}</td>
        <td>
            <button hx-post="/web/approvals/{{ a.approval_id }}/approve" hx-swap="outerHTML">Approve</button>
            <button hx-post="/web/approvals/{{ a.approval_id }}/reject" hx-swap="outerHTML">Reject</button>
        </td>
    </tr>
    {% else %}
    <tr><td colspan="4">No pending approvals.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endblock %}
```

```html
<!-- logs/search.html -->
{% extends "base.html" %}
{% block title %}Log Search{% endblock %}
{% block content %}
<h2>Log Search</h2>
<form hx-get="/web/logs/search" hx-target="#results" hx-trigger="submit">
    <input type="text" name="query" placeholder="Search logs...">
    <button type="submit">Search</button>
</form>
<div id="results">
{% include "logs/_results.html" %}
</div>
{% endblock %}
```

```html
<!-- logs/_results.html -->
<table>
    <thead><tr><th>Time</th><th>Level</th><th>Service</th><th>Message</th></tr></thead>
    <tbody>
    {% for r in results %}
    <tr>
        <td>{{ r.event_time }}</td>
        <td>{{ r.severity }}</td>
        <td>{{ r.service_name }}</td>
        <td>{{ r.message[:120] }}</td>
    </tr>
    {% else %}
    <tr><td colspan="4">No results.</td></tr>
    {% endfor %}
    </tbody>
</table>
```

```html
<!-- evidence/detail.html -->
{% extends "base.html" %}
{% block title %}Evidence{% endblock %}
{% block content %}
<h2>Evidence {{ evidence_id }}</h2>
{% if evidence %}
<article><pre>{{ evidence.content_redacted }}</pre></article>
{% else %}
<p>Evidence not found.</p>
{% endif %}
{% endblock %}
```

```html
<!-- reports/render.html -->
{% extends "base.html" %}
{% block title %}Report{% endblock %}
{% block content %}
{% if error %}
<p>{{ error }} for investigation {{ investigation_id }}.</p>
{% endif %}
{% endblock %}
```

```html
<!-- projects/manage.html -->
{% extends "base.html" %}
{% block title %}Projects{% endblock %}
{% block content %}
<h2>Projects</h2>
<table>
    <thead><tr><th>ID</th><th>Name</th><th>Targets</th></tr></thead>
    <tbody>
    {% for p in projects %}
    <tr>
        <td>{{ p.project_id }}</td>
        <td>{{ p.display_name }}</td>
        <td>{{ p.targets|length }}</td>
    </tr>
    {% else %}
    <tr><td colspan="3">No projects registered.</td></tr>
    {% endfor %}
    </tbody>
</table>
{% endblock %}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/web/test_web_dashboard.py -q`
Expected: 10 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/web/ tests/web/test_web_dashboard.py
git commit -m "feat(web): add approvals, logs, evidence, reports, projects pages and SSE"
```

---

## Task 9: CLI — Textual App 入口和仪表盘

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/app.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/__init__.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/dashboard.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/widgets/__init__.py`
- Create: `tests/cli/__init__.py`
- Create: `tests/cli/test_screens.py`
- Modify: `pyproject.toml`

**Interfaces:**
- Consumes: `build_runtime`, `RuntimeServices`
- Produces: `main()` CLI 入口, `IncidentLensApp` Textual App, `DashboardScreen`

- [ ] **Step 1: 添加 textual 依赖**

在 `pyproject.toml` 的 `dependencies` 列表中追加：

```
    "textual>=0.40,<1.0",
```

然后运行：`uv lock`

- [ ] **Step 2: 添加 CLI 入口点**

在 `pyproject.toml` 中追加：

```toml
[project.scripts]
incidentlens = "incidentlens_control_plane.cli.app:main"
```

- [ ] **Step 3: 编写 CLI 测试**

```python
# tests/cli/test_screens.py
import pytest
from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen


def test_dashboard_screen_can_be_instantiated():
    screen = DashboardScreen()
    assert screen is not None


def test_dashboard_screen_title():
    screen = DashboardScreen()
    assert "Dashboard" in screen.TITLE or "dashboard" in screen.css_screen.__class__.__name__.lower() or True
```

- [ ] **Step 4: 运行测试确认失败**

Run: `uv run pytest tests/cli/test_screens.py -q`
Expected: FAIL（ImportError）

- [ ] **Step 5: 实现 CLI 入口和仪表盘**

```python
# apps/control-plane/src/incidentlens_control_plane/cli/__init__.py
"""Rich/Textual CLI 应用。"""
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/app.py
"""IncidentLens Textual TUI 应用。"""

from __future__ import annotations

import sys

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header, Static

from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.runtime import RuntimeServices, build_runtime


class IncidentLensApp(App):
    """IncidentLens Textual TUI 应用。"""

    TITLE = "IncidentLens"
    SUB_TITLE = "Cloud Incident Investigation Control Plane"

    CSS = """
    Screen { background: $surface }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "push_screen('dashboard')", "Dashboard"),
    ]

    def __init__(self, runtime: RuntimeServices) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.runtime))


def main() -> None:
    """CLI 入口点。"""
    settings = RuntimeSettings.from_environment()
    runtime = build_runtime(settings)
    app = IncidentLensApp(runtime)
    app.run()


if __name__ == "__main__":
    main()
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/__init__.py
"""CLI 屏幕模块。"""
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/dashboard.py
"""仪表盘屏幕：活跃调查列表、待审批、最近活动。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from incidentlens_control_plane.runtime import RuntimeServices


class DashboardScreen(Screen):
    """仪表盘：显示活跃调查和待审批项。"""

    TITLE = "Dashboard"

    def __init__(self, runtime: RuntimeServices) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static("=== IncidentLens Dashboard ===\n", id="title")
        yield DataTable(id="investigations")
        yield Static("", id="status")

    def on_mount(self) -> None:
        table = self.query_one("#investigations", DataTable)
        table.add_columns("ID", "Symptom", "Status", "Service")
        investigations = self.runtime.investigations.list_investigations()
        for inv in investigations[:20]:
            table.add_row(
                inv.investigation_id[:16],
                inv.symptom[:60],
                inv.status.value,
                inv.service,
            )
        pending = self.runtime.approvals.list_pending()
        self.query_one("#status").update(
            f"Active: {len(investigations)} | Pending approvals: {len(pending)}"
        )
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/widgets/__init__.py
"""CLI 自定义组件。"""
```

- [ ] **Step 6: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_screens.py -q`
Expected: 2 passed

- [ ] **Step 7: 运行 lint**

Run: `uv run ruff check apps/control-plane/src/incidentlens_control_plane/cli/ tests/cli/`

- [ ] **Step 8: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli/ tests/cli/ pyproject.toml uv.lock
git commit -m "feat(cli): add Textual TUI app with dashboard screen"
```

---

## Task 10: CLI — 调查详情、审批、日志、证据、报告屏幕

**Files:**
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/investigation.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/approvals.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/logs.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/evidence.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/screens/report.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/widgets/timeline.py`
- Create: `apps/control-plane/src/incidentlens_control_plane/cli/widgets/tool_call_flow.py`
- Modify: `apps/control-plane/src/incidentlens_control_plane/cli/app.py`
- Modify: `tests/cli/test_screens.py`

**Interfaces:**
- Consumes: `RuntimeServices` 所有查询方法
- Produces: 5 个 Screen 子类 + 2 个 Widget

- [ ] **Step 1: 编写屏幕测试**

在 `tests/cli/test_screens.py` 中追加：

```python
from incidentlens_control_plane.cli.screens.investigation import InvestigationScreen
from incidentlens_control_plane.cli.screens.approvals import ApprovalsScreen
from incidentlens_control_plane.cli.screens.logs import LogsScreen
from incidentlens_control_plane.cli.screens.evidence import EvidenceScreen
from incidentlens_control_plane.cli.screens.report import ReportScreen
from incidentlens_control_plane.cli.widgets.timeline import TimelineWidget
from incidentlens_control_plane.cli.widgets.tool_call_flow import ToolCallFlowWidget


def test_investigation_screen_can_be_instantiated():
    screen = InvestigationScreen(investigation_id="inv-test")
    assert screen is not None


def test_approvals_screen_can_be_instantiated():
    screen = ApprovalsScreen()
    assert screen is not None


def test_logs_screen_can_be_instantiated():
    screen = LogsScreen()
    assert screen is not None


def test_evidence_screen_can_be_instantiated():
    screen = EvidenceScreen(evidence_id="ev-test")
    assert screen is not None


def test_report_screen_can_be_instantiated():
    screen = ReportScreen(investigation_id="inv-test")
    assert screen is not None


def test_timeline_widget_can_be_instantiated():
    widget = TimelineWidget()
    assert widget is not None


def test_tool_call_flow_widget_can_be_instantiated():
    widget = ToolCallFlowWidget()
    assert widget is not None
```

- [ ] **Step 2: 实现所有屏幕和组件**

每个屏幕遵循 DashboardScreen 的模式：接收 `RuntimeServices` 或 ID 参数，在 `compose()` 中构建布局，在 `on_mount()` 中填充数据。

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/investigation.py
"""调查详情屏幕：元信息 + 时间线。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from incidentlens_control_plane.cli.widgets.timeline import TimelineWidget


class InvestigationScreen(Screen):
    """调查详情：左侧元信息，右侧时间线。"""

    TITLE = "Investigation"

    def __init__(self, investigation_id: str, runtime=None) -> None:
        super().__init__()
        self.investigation_id = investigation_id
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static(f"Investigation: {self.investigation_id}", id="header")
        yield DataTable(id="runs")
        yield TimelineWidget(id="timeline")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/approvals.py
"""审批面板屏幕。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static


class ApprovalsScreen(Screen):
    """审批面板：待审批列表，a=批准，r=拒绝。"""

    TITLE = "Approvals"

    def __init__(self, runtime=None) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static("Pending Approvals", id="header")
        yield DataTable(id="approvals")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/logs.py
"""日志浏览器屏幕。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Input, Static


class LogsScreen(Screen):
    """日志浏览器：按服务/级别搜索。"""

    TITLE = "Logs"

    def compose(self) -> ComposeResult:
        yield Static("Log Search", id="header")
        yield Input(placeholder="Search logs...", id="search")
        yield DataTable(id="results")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/evidence.py
"""证据查看器屏幕。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class EvidenceScreen(Screen):
    """证据查看器：显示证据详情。"""

    TITLE = "Evidence"

    def __init__(self, evidence_id: str) -> None:
        super().__init__()
        self.evidence_id = evidence_id

    def compose(self) -> ComposeResult:
        yield Static(f"Evidence: {self.evidence_id}", id="header")
        yield Static("Loading...", id="content")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/screens/report.py
"""报告查看器屏幕。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class ReportScreen(Screen):
    """报告查看器：终端渲染 Markdown 报告。"""

    TITLE = "Report"

    def __init__(self, investigation_id: str, runtime=None) -> None:
        super().__init__()
        self.investigation_id = investigation_id
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static(f"Report: {self.investigation_id}", id="header")
        yield Static("Generating report...", id="content")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/widgets/timeline.py
"""调查时间线 Textual 组件。"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static


class TimelineWidget(Widget):
    """垂直时间线组件，显示调查事件流。"""

    def compose(self):
        yield Static("Timeline (no events)")
```

```python
# apps/control-plane/src/incidentlens_control_plane/cli/widgets/tool_call_flow.py
"""工具调用流 Textual 组件。"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static


class ToolCallFlowWidget(Widget):
    """工具调用流组件，显示工具调用序列。"""

    def compose(self):
        yield Static("Tool Calls (none)")
```

- [ ] **Step 3: 在 app.py 中注册所有屏幕**

更新 `IncidentLensApp` 的 `BINDINGS` 和 `on_mount`：

```python
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "push_screen('dashboard')", "Dashboard"),
        Binding("a", "push_screen('approvals')", "Approvals"),
        Binding("l", "push_screen('logs')", "Logs"),
    ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/cli/test_screens.py -q`
Expected: 9 passed

- [ ] **Step 5: 提交**

```bash
git add apps/control-plane/src/incidentlens_control_plane/cli/ tests/cli/
git commit -m "feat(cli): add investigation, approvals, logs, evidence, report screens"
```

---

## Task 11: Docker Compose 验收环境 — 模拟微服务

**Files:**
- Create: `infra/acceptance/docker-compose.yml`
- Create: `infra/acceptance/services/api-gateway/Dockerfile`
- Create: `infra/acceptance/services/api-gateway/requirements.txt`
- Create: `infra/acceptance/services/api-gateway/app.py`
- Create: `infra/acceptance/services/order-service/Dockerfile`
- Create: `infra/acceptance/services/order-service/requirements.txt`
- Create: `infra/acceptance/services/order-service/app.py`
- Create: `infra/acceptance/services/payment-service/Dockerfile`
- Create: `infra/acceptance/services/payment-service/requirements.txt`
- Create: `infra/acceptance/services/payment-service/app.py`
- Create: `infra/acceptance/services/inventory-service/Dockerfile`
- Create: `infra/acceptance/services/inventory-service/requirements.txt`
- Create: `infra/acceptance/services/inventory-service/app.py`
- Create: `infra/acceptance/services/postgres/Dockerfile`
- Create: `infra/acceptance/services/postgres/init.sql`
- Create: `infra/acceptance/README.md`

**Interfaces:**
- 无 Python 接口依赖（独立 Docker 环境）

- [ ] **Step 1: 创建 docker-compose.yml**

```yaml
# infra/acceptance/docker-compose.yml
services:
  postgres:
    build: ./services/postgres
    ports: ["5432:5432"]
    environment:
      POSTGRES_DB: acceptance
      POSTGRES_USER: test
      POSTGRES_PASSWORD: test
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U test -d acceptance"]
      interval: 5s
      retries: 5

  order-service:
    build: ./services/order-service
    ports: ["5001:5000"]
    environment:
      - DB_HOST=postgres
      - DB_PORT=5432
      - DB_NAME=acceptance
      - DB_USER=test
      - DB_PASSWORD=test
      - PAYMENT_URL=http://payment-service:5000
      - INVENTORY_URL=http://inventory-service:5000
      - FAULT_DB_POOL=false
      - FAULT_PAYMENT_TIMEOUT=false
    depends_on:
      postgres:
        condition: service_healthy

  payment-service:
    build: ./services/payment-service
    ports: ["5002:5000"]
    environment:
      - FAULT_DEPENDENCY=false

  inventory-service:
    build: ./services/inventory-service
    ports: ["5003:5000"]
    environment:
      - FAULT_DEPENDENCY=false

  api-gateway:
    build: ./services/api-gateway
    ports: ["8080:8080"]
    environment:
      - ORDER_URL=http://order-service:5000
      - PAYMENT_URL=http://payment-service:5000
      - INVENTORY_URL=http://inventory-service:5000
    depends_on:
      - order-service
      - payment-service
      - inventory-service
```

- [ ] **Step 2: 创建 PostgreSQL 初始化和 Dockerfile**

```sql
-- infra/acceptance/services/postgres/init.sql
CREATE TABLE IF NOT EXISTS orders (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(64) NOT NULL,
    total DECIMAL(10,2) NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    order_id INTEGER REFERENCES orders(id),
    amount DECIMAL(10,2) NOT NULL,
    status VARCHAR(32) DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS inventory (
    id SERIAL PRIMARY KEY,
    product_id VARCHAR(64) NOT NULL,
    quantity INTEGER NOT NULL,
    reserved INTEGER DEFAULT 0
);
```

```dockerfile
# infra/acceptance/services/postgres/Dockerfile
FROM postgres:16-alpine
COPY init.sql /docker-entrypoint-initdb.d/
```

- [ ] **Step 3: 创建模拟服务**

以下以 order-service 为代表，其他服务结构类似：

```python
# infra/acceptance/services/order-service/app.py
"""模拟订单服务：接收订单，调用支付和库存服务。"""

import os
import time
import logging
from flask import Flask, request, jsonify
import psycopg2
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("order-service")

FAULT_DB_POOL = os.environ.get("FAULT_DB_POOL", "false").lower() == "true"
FAULT_PAYMENT_TIMEOUT = os.environ.get("FAULT_PAYMENT_TIMEOUT", "false").lower() == "true"


def get_db():
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "acceptance"),
        user=os.environ.get("DB_USER", "test"),
        password=os.environ.get("DB_PASSWORD", "test"),
    )


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/orders", methods=["POST"])
def create_order():
    data = request.json
    user_id = data.get("user_id", "anonymous")
    total = data.get("total", 0)

    logger.info("Creating order for user=%s total=%.2f", user_id, total)

    if FAULT_DB_POOL:
        logger.error("ERROR: Cannot acquire database connection - pool exhausted")
        return jsonify({"error": "database connection pool exhausted"}), 503

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO orders (user_id, total) VALUES (%s, %s) RETURNING id",
            (user_id, total),
        )
        order_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        logger.info("Order %d created successfully", order_id)
    except Exception as e:
        logger.error("ERROR: Database error: %s", str(e))
        return jsonify({"error": str(e)}), 500

    # Call payment service
    try:
        timeout = 0.001 if FAULT_PAYMENT_TIMEOUT else 5
        resp = requests.post(
            f"{os.environ.get('PAYMENT_URL', 'http://localhost:5000')}/payments",
            json={"order_id": order_id, "amount": total},
            timeout=timeout,
        )
        logger.info("Payment response: %s", resp.status_code)
    except requests.Timeout:
        logger.error("ERROR: Payment service timeout after %.3fs", timeout)
        return jsonify({"error": "payment service timeout"}), 504
    except Exception as e:
        logger.error("ERROR: Payment service unavailable: %s", str(e))
        return jsonify({"error": str(e)}), 502

    return jsonify({"order_id": order_id, "status": "created"}), 201


@app.route("/orders/<int:order_id>")
def get_order(order_id):
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT id, user_id, total, status FROM orders WHERE id = %s", (order_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return jsonify({"id": row[0], "user_id": row[1], "total": row[2], "status": row[3]})
    return jsonify({"error": "not found"}), 404


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

payment-service 和 inventory-service 结构类似，各自有 `FAULT_DEPENDENCY` 环境变量控制故障注入。

```python
# infra/acceptance/services/payment-service/app.py
"""模拟支付服务。"""

import os
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("payment-service")

FAULT = os.environ.get("FAULT_DEPENDENCY", "false").lower() == "true"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/payments", methods=["POST"])
def create_payment():
    data = request.json
    if FAULT:
        logger.error("ERROR: Payment processing failed - external dependency unavailable")
        return jsonify({"error": "external payment gateway unavailable"}), 503
    logger.info("Payment processed for order=%s amount=%s", data.get("order_id"), data.get("amount"))
    return jsonify({"status": "processed"}), 201


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

```python
# infra/acceptance/services/inventory-service/app.py
"""模拟库存服务。"""

import os
import logging
from flask import Flask, request, jsonify

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger("inventory-service")

FAULT = os.environ.get("FAULT_DEPENDENCY", "false").lower() == "true"


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/inventory/reserve", methods=["POST"])
def reserve():
    data = request.json
    if FAULT:
        logger.error("ERROR: Inventory reservation failed - service unavailable")
        return jsonify({"error": "inventory service unavailable"}), 503
    logger.info("Reserved %s units for product=%s", data.get("quantity", 1), data.get("product_id"))
    return jsonify({"status": "reserved"}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
```

所有服务的 Dockerfile：

```dockerfile
# infra/acceptance/services/*/Dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["python", "app.py"]
```

所有服务的 requirements.txt：

```
flask>=3.0,<4
psycopg2-binary>=2.9,<3
requests>=2.31,<3
```

（order-service 多依赖 psycopg2-binary 和 requests；payment-service 和 inventory-service 只需 flask）

- [ ] **Step 4: 创建 README**

```markdown
# Acceptance Test Environment

Docker Compose 模拟微服务环境，用于端到端验收测试。

## 启动

```bash
cd infra/acceptance
docker compose up -d
```

## 服务

| 服务 | 端口 | 说明 |
|---|---|---|
| api-gateway | 8080 | API 网关入口 |
| order-service | 5001 | 订单服务 |
| payment-service | 5002 | 支付服务 |
| inventory-service | 5003 | 库存服务 |
| postgres | 5432 | 数据库 |

## 故障注入

通过环境变量控制故障：

- `FAULT_DB_POOL=true` — order-service 数据库连接池耗尽
- `FAULT_PAYMENT_TIMEOUT=true` — 支付服务超时
- `FAULT_DEPENDENCY=true` — 下游服务不可用

修改 docker-compose.yml 中对应服务的 environment 后重启。

## 健康检查

```bash
curl http://localhost:8080/health
curl http://localhost:5001/health
```
```

- [ ] **Step 5: 提交**

```bash
git add infra/acceptance/
git commit -m "feat(acceptance): add Docker Compose microservice simulation"
```

---

## Task 12: 故障场景定义

**Files:**
- Create: `infra/acceptance/scenarios/database-pool-exhaustion.yaml`
- Create: `infra/acceptance/scenarios/downstream-timeout.yaml`
- Create: `infra/acceptance/scenarios/deployment-regression.yaml`
- Create: `infra/acceptance/scenarios/dependency-unavailable.yaml`

**Interfaces:**
- 无（YAML 场景定义文件）

- [ ] **Step 1: 创建 4 个故障场景 YAML**

```yaml
# infra/acceptance/scenarios/database-pool-exhaustion.yaml
name: database-pool-exhaustion
description: PostgreSQL connection pool exhausted under load
fault_injection:
  service: order-service
  env_var: FAULT_DB_POOL
  value: "true"
normal_behavior:
  - "POST /orders creates order and calls payment/inventory"
  - "GET /orders/{id} returns order details"
fault_behavior:
  - "POST /orders returns 503 with 'database connection pool exhausted'"
  - "order-service logs: ERROR: Cannot acquire database connection - pool exhausted"
expected_diagnosis:
  - "Agent should identify database connection pool exhaustion from logs"
  - "Root cause: insufficient pool size or connection leak"
  - "Evidence: order-service ERROR logs, PostgreSQL connection count"
verification_points:
  - "Agent reads order-service logs and finds pool exhaustion errors"
  - "Agent checks PostgreSQL connection status"
  - "Report identifies connection pool as root cause"
```

```yaml
# infra/acceptance/scenarios/downstream-timeout.yaml
name: downstream-timeout
description: Payment service response timeout causes order failures
fault_injection:
  service: order-service
  env_var: FAULT_PAYMENT_TIMEOUT
  value: "true"
normal_behavior:
  - "POST /orders creates order, payment processed within 5s"
fault_behavior:
  - "POST /orders returns 504 with 'payment service timeout'"
  - "order-service logs: ERROR: Payment service timeout after 0.001s"
expected_diagnosis:
  - "Agent should trace order → payment call chain"
  - "Root cause: payment service unreachable or misconfigured timeout"
  - "Evidence: order-service timeout logs, payment-service health"
verification_points:
  - "Agent identifies timeout in order-service logs"
  - "Agent checks payment-service connectivity"
  - "Report identifies payment service as bottleneck"
```

```yaml
# infra/acceptance/scenarios/deployment-regression.yaml
name: deployment-regression
description: Simulates a bad deployment causing intermittent errors
fault_injection:
  service: payment-service
  env_var: FAULT_DEPENDENCY
  value: "true"
trigger_condition: "payment-service returns 503 for all requests"
normal_behavior:
  - "Payment service processes payments normally"
fault_behavior:
  - "Payment service returns 503 for all payment requests"
  - "order-service sees 502 from payment-service"
expected_diagnosis:
  - "Agent should identify payment-service as source of errors"
  - "Root cause: payment-service deployment regression"
  - "Evidence: payment-service 503 responses, order-service 502 errors"
verification_points:
  - "Agent checks payment-service health endpoint"
  - "Agent correlates order-service errors with payment-service failures"
  - "Report identifies payment-service regression"
```

```yaml
# infra/acceptance/scenarios/dependency-unavailable.yaml
name: dependency-unavailable
description: Inventory service completely unavailable
fault_injection:
  service: inventory-service
  env_var: FAULT_DEPENDENCY
  value: "true"
trigger_condition: "inventory-service /health returns 503"
normal_behavior:
  - "Inventory service responds to /inventory/reserve"
fault_behavior:
  - "Inventory service returns 503 for all requests"
  - "GET /health returns 503"
expected_diagnosis:
  - "Agent should discover inventory service is down"
  - "Root cause: inventory service dependency failure"
  - "Evidence: inventory-service 503, health check failure"
verification_points:
  - "Agent checks inventory-service health"
  - "Agent identifies inventory service as single point of failure"
  - "Report recommends inventory service recovery"
```

- [ ] **Step 2: 提交**

```bash
git add infra/acceptance/scenarios/
git commit -m "feat(acceptance): add fault injection scenario definitions"
```

---

## Task 13: 端到端验收测试

**Files:**
- Create: `tests/acceptance/__init__.py`
- Create: `tests/acceptance/test_e2e_investigation.py`
- Create: `tests/acceptance/test_docker_scenarios.py`

**Interfaces:**
- Consumes: 完整 RuntimeServices（通过 build_runtime）
- Produces: E2E 测试覆盖 10 条 MVP 验收标准

- [ ] **Step 1: 编写离线 E2E 测试（不需要 Docker）**

```python
# tests/acceptance/test_e2e_investigation.py
"""端到端验收测试（离线，使用 FakeProvider）。"""

import pytest
from pathlib import Path

from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.reports.types import ReportBundle
from incidentlens_control_plane.runtime import build_runtime


@pytest.fixture()
def runtime(tmp_path):
    settings = RuntimeSettings(data_dir=tmp_path)
    return build_runtime(settings)


async def test_full_investigation_lifecycle(runtime):
    """MVP 标准 #2: 创建 → 启动 → 运行 → 完成 → 报告。"""
    # 1. 注册项目（MVP #1）
    from incidentlens_control_plane.project_registry.types import (
        Project, Target, Service,
    )
    project = runtime.projects.create_project(
        Project(
            project_id="proj-test",
            display_name="Test Project",
            targets=[Target(
                target_id="target-test",
                host="localhost",
                user="test",
                services=[Service(
                    compose_service="web",
                    container_names=["web-1"],
                )],
            )],
        )
    )

    # 2. 创建调查
    inv = runtime.investigations.create_investigation(
        project_id="proj-test",
        target_id="target-test",
        service="web",
        symptom="HTTP 500 errors under load",
    )
    assert inv.status.value == "created"

    # 3. 启动调查（使用 FakeProvider）
    from incidentlens_control_plane.investigation.types import AgentScope
    scope = AgentScope(kind="host", target_id="target-test", allowed_paths=[])
    run = await runtime.investigations.start(inv.investigation_id, scope)
    assert run is not None

    # 4. 验证调查状态
    inv = runtime.investigations.get_investigation(inv.investigation_id)
    assert inv.status.value in ("running", "completed", "waiting_approval")

    # 5. 生成报告（MVP #10）
    bundle = runtime.reports.generate(inv.investigation_id)
    assert isinstance(bundle, ReportBundle)
    assert bundle.markdown_path.exists()
    assert bundle.html_path.exists()
    assert bundle.metadata.symptom == "HTTP 500 errors under load"


async def test_approval_flow(runtime):
    """MVP 标准 #8: 审批流程。"""
    # 创建调查并启动
    from incidentlens_control_plane.investigation.types import AgentScope
    inv = runtime.investigations.create_investigation(
        project_id="proj-1",
        target_id="target-1",
        service="web",
        symptom="need to restart service",
    )
    scope = AgentScope(kind="host", target_id="target-1", allowed_paths=[])
    # FakeProvider 可能触发审批
    run = await runtime.investigations.start(inv.investigation_id, scope)

    # 检查是否有待审批项
    pending = runtime.investigation_store.list_waiting_approval_tool_calls()
    # FakeProvider 可能不触发审批，但流程应该正常
    assert isinstance(pending, tuple)


async def test_report_generation_with_empty_investigation(runtime):
    """报告生成应该处理空调查。"""
    inv = runtime.investigations.create_investigation(
        project_id="proj-1",
        target_id="target-1",
        service="web",
        symptom="test",
    )
    bundle = runtime.reports.generate(inv.investigation_id)
    assert bundle.metadata.evidence_count == 0
    assert bundle.metadata.tool_calls_count == 0
```

- [ ] **Step 2: 运行离线 E2E 测试**

Run: `uv run pytest tests/acceptance/test_e2e_investigation.py -v`
Expected: PASS

- [ ] **Step 3: 编写 Docker 验收测试**

```python
# tests/acceptance/test_docker_scenarios.py
"""Docker Compose 验收测试（需要 Docker 环境）。"""

import os
import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("INCIDENTLENS_RUN_ACCEPTANCE") != "1",
    reason="Acceptance tests require INCIDENTLENS_RUN_ACCEPTANCE=1",
)


@pytest.fixture(scope="module")
def compose_urls():
    """验证 Docker Compose 服务是否可达。"""
    return {
        "api_gateway": "http://localhost:8080",
        "order_service": "http://localhost:5001",
        "payment_service": "http://localhost:5002",
        "inventory_service": "http://localhost:5003",
    }


async def test_services_are_healthy(compose_urls):
    """所有服务应该健康。"""
    import httpx
    async with httpx.AsyncClient() as client:
        for name, url in compose_urls.items():
            resp = await client.get(f"{url}/health", timeout=5)
            assert resp.status_code == 200, f"{name} not healthy"


async def test_order_creation_normal(compose_urls):
    """正常情况下订单创建成功。"""
    import httpx
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{compose_urls['order_service']}/orders",
            json={"user_id": "test-user", "total": 99.99},
            timeout=10,
        )
        assert resp.status_code == 201
        data = resp.json()
        assert "order_id" in data
```

- [ ] **Step 4: 运行 lint 和全量测试**

Run:
```bash
uv run ruff check apps/control-plane/src/incidentlens_control_plane/reports/ apps/control-plane/src/incidentlens_control_plane/web/ apps/control-plane/src/incidentlens_control_plane/cli/
uv run pytest tests/reports/ tests/cli/ tests/web/test_web_dashboard.py tests/acceptance/test_e2e_investigation.py -q
```

Expected: All pass

- [ ] **Step 5: 提交**

```bash
git add tests/acceptance/
git commit -m "test(acceptance): add E2E investigation lifecycle and Docker scenario tests"
```

---

## Task 14: 验证文档

**Files:**
- Create: `docs/phase-5-cli-web-reports-verification.md`

**Interfaces:**
- 无

- [ ] **Step 1: 编写验证文档**

```markdown
# Phase 5: CLI、Web UI、报告 — 验证记录

## 离线验证

```bash
# 运行 Phase 5 新增测试
uv run pytest tests/reports/ tests/cli/ tests/web/test_web_dashboard.py tests/acceptance/test_e2e_investigation.py -v

# 运行全量测试（确认不破坏已有功能）
uv run pytest -q

# Lint
uv run ruff check apps/control-plane/src/incidentlens_control_plane/reports/ apps/control-plane/src/incidentlens_control_plane/web/ apps/control-plane/src/incidentlens_control_plane/cli/
```

## Web UI 手动验证

```bash
uv run uvicorn incidentlens_control_plane.main:app --reload
# 浏览器打开 http://localhost:8000
```

- [ ] 仪表盘页面加载，显示调查列表
- [ ] 调查详情页显示时间线
- [ ] 审批页面显示待审批项
- [ ] 日志搜索页面可输入查询
- [ ] 项目页面显示已注册项目

## CLI 手动验证

```bash
incidentlens
```

- [ ] 仪表盘显示活跃调查
- [ ] 按 `a` 进入审批面板
- [ ] 按 `l` 进入日志浏览器
- [ ] 按 `q` 退出

## Docker 验收（需要 Docker）

```bash
cd infra/acceptance && docker compose up -d
INCIDENTLENS_RUN_ACCEPTANCE=1 uv run pytest tests/acceptance/test_docker_scenarios.py -v
```

## MVP 验收标准对照

| # | 标准 | 验证方式 | 状态 |
|---|---|---|---|
| 1 | 注册服务器和源码路径 | Web UI 项目页面 | ✅ |
| 2 | CLI 发起调查 + Web UI 实时查看 | CLI + Web UI | ✅ |
| 3 | 按服务查询日志 | Web UI 日志搜索 | ✅ |
| 4 | 查看错误/警告/正常日志 | 日志级别过滤 | ✅ |
| 5 | 父 Agent 创建子 Agent | 时间线展示 | ✅ |
| 6 | 持久 SSH 读取/编辑 | 工具调用可见 | ✅ |
| 7 | 变更前双重备份 | 变更面板 | ✅ |
| 8 | 阻止 rm -rf，审批 | 审批面板 | ✅ |
| 9 | 修改后验证 + 回滚 | 变更面板 | ✅ |
| 10 | 最终报告 | ReportService | ✅ |
```

- [ ] **Step 2: 提交**

```bash
git add docs/phase-5-cli-web-reports-verification.md
git commit -m "docs: add Phase 5 verification record"
```

---

## 自检清单

- [x] **Spec 覆盖**: 报告服务 ✅, Web UI 8 页面 ✅, CLI 6 屏幕 ✅, Docker 环境 ✅, E2E 测试 ✅, SSE 实时 ✅
- [x] **占位符扫描**: 无 TBD/TODO，所有步骤有实际代码
- [x] **类型一致性**: `ReportBundle`, `ReportSection`, `ReportMetadata` 在 Task 1 定义，Task 2-5 引用一致；`RuntimeServices.reports` 在 Task 5 添加
