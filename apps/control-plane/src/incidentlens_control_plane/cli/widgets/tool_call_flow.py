"""工具调用流 Textual 组件。"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static


class ToolCallFlowWidget(Widget):
    """工具调用流组件，显示工具调用序列。"""

    def compose(self):
        yield Static("Tool Calls (none)")
