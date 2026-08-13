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
