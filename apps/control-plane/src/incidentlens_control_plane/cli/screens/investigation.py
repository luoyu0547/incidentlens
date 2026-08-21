"""面向一次事故调查的终端工作区。"""

from __future__ import annotations

import asyncio

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Input, RichLog, Static

from incidentlens_control_plane.runtime import RuntimeServices


class InvestigationScreen(Screen):
    """以调查过程为中心，而不是以网页表格为中心的 CLI 工作区。"""

    TITLE = "Investigation"
    BINDINGS = [
        Binding("r", "report", "报告"),
        Binding("ctrl+r", "refresh", "刷新"),
        Binding("escape", "back", "返回"),
    ]

    def __init__(self, investigation_id: str, runtime: RuntimeServices | None = None) -> None:
        super().__init__()
        self.investigation_id = investigation_id
        self.runtime = runtime
        self.events_ready = asyncio.Event()

    def compose(self) -> ComposeResult:
        yield Static("", id="workspace-title")
        with Horizontal(id="workspace-body"):
            yield Static("", id="session-context")
            yield RichLog(id="activity", wrap=True, markup=True, highlight=False)
        yield Input(placeholder="输入 :help 查看调查命令", id="command-bar")
        yield Static(
            ":report 报告 · :cancel 取消 · :approve <ID> 批准 · "
            ":reject <ID> 拒绝 · Ctrl+R 刷新",
            id="command-hint",
        )

    def on_mount(self) -> None:
        self.events_ready.set()
        self.set_interval(1.0, self.refresh_workspace)
        self.refresh_workspace()
        self.query_one("#command-bar", Input).focus()

    def refresh_workspace(self) -> None:
        if self.runtime is None:
            self.query_one("#workspace-title", Static).update("IncidentLens / 调查工作区")
            return
        try:
            investigation = self.runtime.investigations.get_investigation(self.investigation_id)
        except Exception as exc:  # 屏幕应展示可恢复错误，而不是让 TUI 崩溃。
            self.query_one("#workspace-title", Static).update(f"调查不可用：{exc}")
            return

        runs = self.runtime.investigations.list_runs(investigation_id=self.investigation_id)
        hypotheses = self.runtime.investigations.list_hypotheses(
            investigation_id=self.investigation_id
        )
        conclusions = self.runtime.investigations.list_conclusions(
            investigation_id=self.investigation_id
        )
        evidence = self.runtime.evidence.list_for_incident(investigation.incident_id)

        self.query_one("#workspace-title", Static).update(
            "[bold #79c0ff]◆ incidentlens[/] [dim]/[/] 调查会话  "
            f"[dim]{investigation.investigation_id}[/]  "
            f"[bold {self._status_color(investigation.status.value)}]"
            f"{investigation.status.value.upper()}[/]"
        )
        self.query_one("#session-context", Static).update(
            "[bold #79c0ff]调查范围[/]\n\n"
            "[dim]症状[/]\n"
            f"[white]{investigation.symptom}[/]\n\n"
            f"[dim]项目[/]  [cyan]{investigation.project_id}[/]\n"
            f"[dim]目标[/]  [cyan]{investigation.target_id}[/]\n"
            f"[dim]服务[/]  [cyan]{investigation.service}[/]\n\n"
            "[bold #79c0ff]资源预算[/]\n\n"
            f"[dim]轮次[/]  {investigation.usage.rounds}/{investigation.budget.max_rounds}\n"
            f"[dim]工具[/]  {investigation.usage.tool_calls}/"
            f"{investigation.budget.max_tool_calls}\n"
            f"[dim]证据[/]  {len(evidence)}/{investigation.budget.max_evidence}\n\n"
            "[bold #d29922]安全边界[/]\n\n"
            "[dim]已脱敏证据 · 精确审批\n不暴露通用 Shell[/]"
        )
        self._render_activity(runs, hypotheses, conclusions)

    def _render_activity(self, runs, hypotheses, conclusions) -> None:
        log = self.query_one("#activity", RichLog)
        log.clear()
        log.write("[bold #79c0ff]调查活动[/]  [dim]仅展示结构化、可审计事件[/]")
        if not runs:
            log.write("[dim]尚未创建 Agent 运行。此调查处于等待启动状态。[/]")
            return
        for run in runs:
            scope = run.scope.container_name or run.scope.target_id
            log.write("")
            log.write(
                f"[bold #58a6ff]●  {run.status.value.upper()}[/]  "
                f"[bold]Agent {run.kind.value}[/]  [dim]{run.agent_run_id} · {scope}[/]"
            )
            for tool in self.runtime.investigation_store.list_tool_calls(
                agent_run_id=run.agent_run_id
            ):
                status = tool.status.value.upper()
                log.write(
                    f"    [dim]├─[/] {tool.tool_name:<22} "
                    f"[{self._status_color(tool.status.value)}]{status}[/]"
                )
        if hypotheses or conclusions:
            log.write("")
            log.write("[bold #79c0ff]分析结果[/]")
        for hypothesis in hypotheses:
            log.write(
                f"  [magenta]?[/] [dim]假设[/]  {hypothesis.summary}"
            )
        for conclusion in conclusions:
            log.write(f"  [green]✓[/] [dim]结论[/]  {conclusion.summary}")
        waiting = [
            tool
            for run in runs
            for tool in self.runtime.investigation_store.list_tool_calls(
                agent_run_id=run.agent_run_id
            )
            if tool.status.value == "waiting_approval"
        ]
        log.write("")
        if waiting:
            log.write(
                f"[bold #d29922]待处理[/]  等待 {len(waiting)} 个精确审批；"
                "使用 :approve <审批 ID> 或 :reject <审批 ID>。"
            )
        else:
            log.write("[bold #3fb950]下一步[/]  当前没有待确认操作；输入 :report 生成调查报告。")

    @staticmethod
    def _status_color(status: str) -> str:
        if status in {"completed", "approved", "succeeded", "confirmed"}:
            return "#3fb950"
        if status in {"failed", "cancelled", "rejected"}:
            return "#f85149"
        if "waiting" in status or "paused" in status or status == "pending":
            return "#d29922"
        return "#58a6ff"

    def on_input_submitted(self, event: Input.Submitted) -> None:
        command = event.value.strip()
        event.input.value = ""
        if not command:
            return
        self.run_worker(self._run_command(command), exclusive=True)

    async def _run_command(self, command: str) -> None:
        log = self.query_one("#activity", RichLog)
        parts = command.split(maxsplit=1)
        action = parts[0].lower()
        argument = parts[1] if len(parts) == 2 else ""
        if action == ":help":
            log.write(
                "[bold]可用命令[/]  :report · :cancel · :approve <审批 ID> · "
                ":reject <审批 ID> · :refresh"
            )
        elif action == ":report":
            self.action_report()
        elif action == ":refresh":
            self.refresh_workspace()
        elif action == ":cancel" and self.runtime is not None:
            await self.runtime.investigations.cancel(self.investigation_id)
            self.refresh_workspace()
        elif action in {":approve", ":reject"} and argument and self.runtime is not None:
            if action == ":approve":
                await self.runtime.approvals.approve(argument)
            else:
                await self.runtime.approvals.reject(argument)
            await self.runtime.investigations.handle_approval_decision(argument)
            self.refresh_workspace()
        else:
            log.write(f"[yellow]无法执行：{command}[/]  输入 :help 查看可用命令。")

    def action_report(self) -> None:
        from incidentlens_control_plane.cli.screens.report import ReportScreen

        self.app.push_screen(ReportScreen(self.investigation_id, self.runtime))

    def action_refresh(self) -> None:
        self.refresh_workspace()

    def action_back(self) -> None:
        self.app.pop_screen()
