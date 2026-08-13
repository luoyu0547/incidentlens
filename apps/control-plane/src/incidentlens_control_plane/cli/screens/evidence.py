"""证据查看器屏幕。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static


class EvidenceScreen(Screen):
    """证据查看器：显示证据详情。"""

    TITLE = "Evidence"

    def __init__(self, evidence_id: str) -> None:
        super().__init__()
        self.evidence_id = evidence_id

    def compose(self) -> ComposeResult:
        yield Static(f"Evidence: {self.evidence_id}", id="header")
        yield Static("Loading...", id="content")
