"""调查报告生成服务：从 InvestigationStore + EvidenceStore 聚合数据生成报告。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from incidentlens_control_plane.evidence.store import EvidenceStore
from incidentlens_control_plane.investigation.store import (
    InvestigationNotFound,
    InvestigationStore,
)
from incidentlens_control_plane.investigation.types import ToolCall
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
        tool_calls: list[ToolCall] = []
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

        # 确定根因：以最新一条结论的摘要作为根因。Conclusion 没有
        # root_cause_category / confidence 字段，因此置信度保持为 None。
        root_cause = conclusions[-1].summary if conclusions else None
        confidence = None

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
        self,
        investigation,
        runs,
        tool_calls,
        hypotheses,
        conclusions,
        evidence_refs,
    ) -> list[ReportSection]:
        sections: list[ReportSection] = []

        # 1. 摘要
        sections.append(
            ReportSection(
                kind=ReportKind.SUMMARY,
                title="Summary",
                content=(
                    f"Investigation into: {investigation.symptom}\n\n"
                    f"Service: {investigation.service}\n"
                    f"Status: {investigation.status.value}\n"
                    f"Total rounds: {investigation.usage.rounds}"
                ),
            )
        )

        # 2. 根因分析：结论没有分类/置信度，逐条渲染 summary。
        if conclusions:
            lines = [f"- {c.summary}" for c in conclusions]
            sections.append(
                ReportSection(
                    kind=ReportKind.ROOT_CAUSE,
                    title="Root Cause Analysis",
                    content="\n".join(lines),
                )
            )

        # 3. 时间线
        timeline_lines: list[str] = []
        for run in runs:
            timeline_lines.append(
                f"- **{run.created_at:%H:%M:%S}** — Agent run "
                f"`{run.agent_run_id[-8:]}` started ({run.kind.value})"
            )
            if run.status.value in ("completed", "failed", "cancelled"):
                timeline_lines.append(f"  - Finished: {run.status.value}")
        for tc in tool_calls:
            if tc.started_at:
                timeline_lines.append(
                    f"- **{tc.started_at:%H:%M:%S}** — "
                    f"`{tc.tool_name}` → {tc.status.value}"
                )
        if timeline_lines:
            sections.append(
                ReportSection(
                    kind=ReportKind.TIMELINE,
                    title="Timeline",
                    content="\n".join(timeline_lines),
                )
            )

        # 4. 证据汇总
        if evidence_refs:
            ev_lines = [
                f"- [{ref.evidence_kind.value}] {ref.evidence_ref_id}: "
                f"{ref.source_ref or 'N/A'}"
                for ref in evidence_refs[:50]
            ]
            sections.append(
                ReportSection(
                    kind=ReportKind.EVIDENCE,
                    title="Evidence",
                    content="\n".join(ev_lines),
                )
            )

        # 5. 假设演进
        if hypotheses:
            hyp_lines = [f"- **{h.status.value}**: {h.summary}" for h in hypotheses]
            sections.append(
                ReportSection(
                    kind=ReportKind.HYPOTHESES,
                    title="Hypotheses",
                    content="\n".join(hyp_lines),
                )
            )

        # 6-8: 修改记录、验证结果、备份状态 — 依赖 ChangeSetStore，MVP 阶段不生成。

        # 9. 修复建议：Conclusion 没有 remediation 字段，统一给出基于证据的人工复核建议。
        sections.append(
            ReportSection(
                kind=ReportKind.RECOMMENDATIONS,
                title="Recommendations",
                content="- Manual review recommended based on evidence above.",
            )
        )

        # 10. 附录
        appendix_lines: list[str] = []
        for tc in tool_calls:
            appendix_lines.append(
                f"- `{tc.tool_name}` ({tc.status.value}) — "
                f"evidence: {', '.join(tc.evidence_ids) or 'none'}"
            )
        if appendix_lines:
            sections.append(
                ReportSection(
                    kind=ReportKind.APPENDIX,
                    title="Appendix: Tool Calls",
                    content="\n".join(appendix_lines),
                )
            )

        return sections
