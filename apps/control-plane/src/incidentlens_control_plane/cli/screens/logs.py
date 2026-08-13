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
