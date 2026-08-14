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
        yield Static("[bold]incidentlens[/] / 本地调查", id="workspace-title")
        yield DataTable(id="investigations")
        yield Static(
            "选择调查后按 Enter 进入会话；也可使用 incidentlens investigate <ID>。",
            id="status",
        )

    def on_mount(self) -> None:
        if self.runtime is None:
            return
        table = self.query_one("#investigations", DataTable)
        table.cursor_type = "row"
        table.add_columns("调查", "症状", "状态", "服务", "更新")
        investigations = self.runtime.investigations.list_investigations()
        for inv in investigations[:20]:
            table.add_row(
                inv.investigation_id,
                inv.symptom[:60],
                inv.status.value,
                inv.service,
                inv.updated_at.strftime("%m-%d %H:%M"),
                key=inv.investigation_id,
            )
        pending = self.runtime.approvals.list(ApprovalStatus.PENDING)
        self.query_one("#status").update(
            f"共 {len(investigations)} 个调查 · {len(pending)} 个待确认操作 · Enter 进入会话"
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        from incidentlens_control_plane.cli.screens.investigation import InvestigationScreen

        self.app.push_screen(InvestigationScreen(str(event.row_key.value), self.runtime))
