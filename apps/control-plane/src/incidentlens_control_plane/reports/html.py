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
    h1 {
      font-size: 1.8rem;
      margin-bottom: 0.5rem;
      border-bottom: 2px solid var(--il-primary);
      padding-bottom: 0.5rem;
    }
    h2 { font-size: 1.3rem; margin: 1.5rem 0 0.5rem; color: var(--il-primary); }
    .meta {
      background: var(--il-surface);
      border: 1px solid var(--il-border);
      border-radius: 6px;
      padding: 1rem;
      margin: 1rem 0;
    }
    .meta dt { font-weight: 600; display: inline; }
    .meta dd { display: inline; margin: 0 1rem 0 0.3rem; }
    .meta dd::after { content: ""; display: block; }
    pre {
      background: var(--il-surface);
      border: 1px solid var(--il-border);
      border-radius: 4px;
      padding: 1rem;
      overflow-x: auto;
      font-family: var(--il-mono);
      font-size: 0.85rem;
    }
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
