"""在终端内阅读实际生成的调查报告。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import RichLog, Static


class ReportScreen(Screen):
    """报告查看器：直接调用 ReportService，渲染生成的 Markdown。"""

    TITLE = "Report"
    BINDINGS = [Binding("escape", "back", "返回")]

    def __init__(self, investigation_id: str, runtime=None) -> None:
        super().__init__()
        self.investigation_id = investigation_id
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static("[bold]incidentlens[/] / 调查报告", id="workspace-title")
        yield RichLog(id="report-content", wrap=True, markup=False, highlight=False)
        yield Static(
            "[dim]Esc 返回调查会话 · 报告同时写入本地 Markdown 和 HTML 文件[/]",
            id="command-hint",
        )

    def on_mount(self) -> None:
        log = self.query_one("#report-content", RichLog)
        if self.runtime is None:
            log.write("报告运行时不可用。")
            return
        try:
            bundle = self.runtime.reports.generate(self.investigation_id)
            log.write(bundle.markdown_path.read_text(encoding="utf-8"))
            log.write(
                "\n报告已写入运行数据目录 reports/\n"
                f"Markdown: {bundle.markdown_path.name}\n"
                f"HTML: {bundle.html_path.name}"
            )
        except Exception as exc:
            log.write(f"无法生成报告：{exc}")

    def action_back(self) -> None:
        self.app.pop_screen()
