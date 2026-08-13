"""IncidentLens Textual TUI 应用。"""

from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer, Header

from incidentlens_control_plane.cli.screens.approvals import ApprovalsScreen
from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen
from incidentlens_control_plane.cli.screens.logs import LogsScreen
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.runtime import RuntimeServices, build_runtime


class IncidentLensApp(App):
    """IncidentLens Textual TUI 应用。"""

    TITLE = "IncidentLens"
    SUB_TITLE = "Cloud Incident Investigation Control Plane"

    CSS = """
    Screen { background: $surface }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "show_dashboard", "Dashboard"),
        Binding("a", "show_approvals", "Approvals"),
        Binding("l", "show_logs", "Logs"),
    ]

    def __init__(self, runtime: RuntimeServices) -> None:
        super().__init__()
        self.runtime = runtime

    def compose(self) -> ComposeResult:
        yield Header()
        yield Footer()

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.runtime))

    def action_show_dashboard(self) -> None:
        self.push_screen(DashboardScreen(self.runtime))

    def action_show_approvals(self) -> None:
        self.push_screen(ApprovalsScreen(self.runtime))

    def action_show_logs(self) -> None:
        self.push_screen(LogsScreen())


def main() -> None:
    """CLI 入口点。"""
    settings = RuntimeSettings.from_environment()
    runtime = build_runtime(settings)
    app = IncidentLensApp(runtime)
    app.run()


if __name__ == "__main__":
    main()
