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
