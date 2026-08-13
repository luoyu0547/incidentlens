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
