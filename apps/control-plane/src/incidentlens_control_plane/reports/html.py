"""自包含的中文 HTML 调查报告渲染器。"""
# ruff: noqa: E501

from __future__ import annotations

import html as html_mod
from textwrap import dedent

from incidentlens_control_plane.reports.types import ReportKind, ReportMetadata, ReportSection

_EVIDENCE_KINDS: frozenset[ReportKind] = frozenset({ReportKind.EVIDENCE, ReportKind.APPENDIX})
_DIFF_KINDS: frozenset[ReportKind] = frozenset({ReportKind.MODIFICATIONS})

_CSS = dedent("""\
    :root { --il-primary:#315dcd; --il-bg:#f4f7fb; --il-surface:#fff; --il-text:#172033; --il-muted:#657187; --il-success:#167a58; --il-border:#dce3ef; --il-font:"PingFang SC","Noto Sans SC",-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; --il-mono:"SFMono-Regular",Consolas,"Liberation Mono",Menlo,monospace; }
    * { box-sizing:border-box; margin:0; padding:0; }
    body { max-width:1120px; margin:0 auto; padding:48px 28px 72px; background:var(--il-bg); color:var(--il-text); font:15px/1.7 var(--il-font); }
    .report-hero { padding:34px 38px; border-radius:16px; background:linear-gradient(135deg,#14264e,#1f49a6); color:#fff; box-shadow:0 18px 45px rgba(35,72,152,.18); }
    .eyebrow { color:#aebfff; font-size:.78rem; font-weight:700; letter-spacing:.14em; text-transform:uppercase; }
    h1 { margin:7px 0 8px; font-size:2rem; line-height:1.25; letter-spacing:-.035em; }
    .hero-subtitle { color:#d6e1ff; font-size:1rem; } .hero-safety { display:inline-block; margin-top:18px; padding:5px 10px; border:1px solid rgba(255,255,255,.28); border-radius:99px; color:#e4fff4; font-size:.8rem; }
    .meta-grid { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:1px; margin:22px 0 34px; overflow:hidden; border:1px solid var(--il-border); border-radius:12px; background:var(--il-border); }
    .meta-item { min-height:94px; padding:17px 19px; background:var(--il-surface); } .meta-label { display:block; margin-bottom:6px; color:var(--il-muted); font-size:.78rem; } .meta-value { color:var(--il-text); font-size:.95rem; font-weight:650; overflow-wrap:anywhere; } .meta-value.mono { font-family:var(--il-mono); font-size:.84rem; }
    .report-section { margin-top:30px; padding:28px 30px; border:1px solid var(--il-border); border-radius:12px; background:var(--il-surface); } .section-heading { display:flex; align-items:baseline; gap:10px; margin-bottom:17px; } h2 { color:var(--il-text); font-size:1.18rem; letter-spacing:-.015em; } .section-source { color:var(--il-muted); font-family:var(--il-mono); font-size:.72rem; } .section-content { color:#364259; white-space:pre-wrap; overflow-wrap:anywhere; }
    .report-section[data-kind="root_cause"] { border-left:4px solid var(--il-primary); } .report-section[data-kind="recommendations"] { border-left:4px solid var(--il-success); } .report-section[data-kind="timeline"] .section-content { padding-left:16px; border-left:2px solid #c9d7fb; }
    details { border:1px solid var(--il-border); border-radius:9px; background:#fbfcff; } summary { cursor:pointer; padding:14px 16px; color:var(--il-text); font-weight:650; } details .section-content { padding:0 16px 16px; } pre { margin-top:12px; overflow-x:auto; border-radius:9px; background:#101725; color:#dce8ff; padding:18px; font:.82rem/1.6 var(--il-mono); } code { font-family:var(--il-mono); font-size:.9em; } .report-footer { margin-top:28px; color:var(--il-muted); font-size:.8rem; text-align:center; }
    @media (max-width:720px) { body { padding:20px 14px 42px; } .report-hero,.report-section { padding:24px 20px; } .meta-grid { grid-template-columns:repeat(2,minmax(0,1fr)); } h1 { font-size:1.6rem; } }
    @media print { body { max-width:none; padding:0; background:#fff; font-size:10.5pt; } .report-hero { color:#000; background:#fff; border:1px solid #000; box-shadow:none; } .hero-subtitle,.eyebrow,.hero-safety { color:#000; border-color:#000; } .report-section,.meta-grid { break-inside:avoid; } pre { white-space:pre-wrap; word-break:break-word; } }
""")


class HtmlRenderer:
    """将报告数据渲染为适合阅读与打印的中文 HTML 文档。"""

    def render(self, sections: list[ReportSection], metadata: ReportMetadata) -> str:
        return (
            "<!DOCTYPE html>\n<html lang=\"zh-CN\">\n<head>\n"
            "<meta charset=\"utf-8\">\n<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
            "<title>事故调查报告 · Investigation Report</title>\n"
            f"<style>{_CSS}</style>\n</head>\n<body>\n"
            "<header class=\"report-hero\"><div class=\"eyebrow\">IncidentLens · 安全优先事故调查</div>\n"
            "<h1>事故调查报告 <span class=\"section-source\">Investigation Report</span></h1>\n"
            f"<p class=\"hero-subtitle\">{html_mod.escape(metadata.symptom)}</p>\n"
            "<span class=\"hero-safety\">已脱敏证据 · 可审计调查记录</span></header>\n"
            f"{self._render_metadata(metadata)}\n"
            f"{' '.join(self._render_section(section) for section in sections)}\n"
            "<footer class=\"report-footer\">所有日志与证据均在归档前脱敏；报告仅包含调查运行时已拥有的证据引用。</footer>\n"
            "</body>\n</html>\n"
        )

    def _render_metadata(self, meta: ReportMetadata) -> str:
        items: list[tuple[str, str, bool]] = [("调查症状", html_mod.escape(meta.symptom), False)]
        if meta.root_cause:
            items.append(("根因", html_mod.escape(meta.root_cause), False))
        if meta.confidence is not None:
            items.append(("置信度", f"{meta.confidence:.0%}", False))
        if meta.services_affected:
            items.append(("受影响服务", html_mod.escape(", ".join(meta.services_affected)), True))
        items.extend([("证据数量", str(meta.evidence_count), False), ("工具调用", str(meta.tool_calls_count), False), ("调查时长", f"{meta.duration_seconds:.0f}s", False), ("生成时间", meta.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC"), True)])
        cards = "".join("<div class=\"meta-item\">" f"<span class=\"meta-label\">{label}</span>" f"<span class=\"meta-value{' mono' if mono else ''}\">{value}</span></div>" for label, value, mono in items)
        return f'<section class="meta-grid">{cards}</section>'

    @staticmethod
    def _display_title(kind: ReportKind) -> str:
        return {ReportKind.SUMMARY:"摘要", ReportKind.ROOT_CAUSE:"根因分析", ReportKind.TIMELINE:"调查时间线", ReportKind.EVIDENCE:"证据汇总", ReportKind.HYPOTHESES:"假设演进", ReportKind.MODIFICATIONS:"变更记录", ReportKind.VERIFICATION:"验证结果", ReportKind.BACKUPS:"备份状态", ReportKind.RECOMMENDATIONS:"修复建议", ReportKind.APPENDIX:"附录：工具调用"}[kind]

    def _render_section(self, section: ReportSection) -> str:
        title = html_mod.escape(self._display_title(section.kind))
        source_title = html_mod.escape(section.title)
        escaped = html_mod.escape(section.content)
        heading = f"<div class=\"section-heading\"><h2>{title}</h2><span class=\"section-source\">{source_title}</span></div>"
        if section.kind in _DIFF_KINDS and section.content.strip():
            content = f"<pre><code class=\"diff\">{escaped}</code></pre>"
        elif section.kind in _EVIDENCE_KINDS:
            content = "<details open><summary>展开已脱敏证据详情</summary>" f"<div class=\"section-content\">{escaped}</div></details>"
        else:
            content = f"<div class=\"section-content\">{escaped}</div>"
        return f'<section class="report-section" data-kind="{section.kind.value}">{heading}{content}</section>'
