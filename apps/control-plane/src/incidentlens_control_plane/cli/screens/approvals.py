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
