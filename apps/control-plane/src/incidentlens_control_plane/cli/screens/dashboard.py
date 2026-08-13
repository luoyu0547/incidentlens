"""仪表盘屏幕：活跃调查列表、待审批、最近活动。"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Static

from incidentlens_control_plane.approvals.types import ApprovalStatus
from incidentlens_control_plane.runtime import RuntimeServices


class DashboardScreen(Screen):
    """仪表盘：显示活跃调查和待审批项。"""

    TITLE = "Dashboard"

    def __init__(self, runtime: RuntimeServices | None = None) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Static("=== IncidentLens Dashboard ===\n", id="title")
        yield DataTable(id="investigations")
        yield Static("", id="status")

    def on_mount(self) -> None:
        if self.runtime is None:
            return
        table = self.query_one("#investigations", DataTable)
        table.add_columns("ID", "Symptom", "Status", "Service")
        investigations = self.runtime.investigations.list_investigations()
        for inv in investigations[:20]:
            table.add_row(
                inv.investigation_id[:16],
                inv.symptom[:60],
                inv.status.value,
                inv.service,
            )
        pending = self.runtime.approvals.list(ApprovalStatus.PENDING)
        self.query_one("#status").update(
            f"Active: {len(investigations)} | Pending approvals: {len(pending)}"
        )
