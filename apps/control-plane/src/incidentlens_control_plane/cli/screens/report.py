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
