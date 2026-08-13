"""调查时间线 Textual 组件。"""

from __future__ import annotations

from textual.widget import Widget
from textual.widgets import Static


class TimelineWidget(Widget):
    """垂直时间线组件，显示调查事件流。"""

    def compose(self):
        yield Static("Timeline (no events)")
