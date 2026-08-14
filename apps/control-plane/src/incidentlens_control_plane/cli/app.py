"""IncidentLens 的本地、会话驱动终端入口。"""

from __future__ import annotations

import argparse

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Footer

from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen
from incidentlens_control_plane.cli.screens.investigation import InvestigationScreen
from incidentlens_control_plane.cli.screens.report import ReportScreen
from incidentlens_control_plane.config import RuntimeSettings
from incidentlens_control_plane.runtime import RuntimeServices, build_runtime


class IncidentLensApp(App):
    """以一次调查会话为核心的 Textual CLI。"""

    TITLE = "IncidentLens"
    SUB_TITLE = "local incident investigation"

    CSS = """
    Screen { background: #0b0f14; color: #c9d1d9; }
    Footer { background: #0b0f14; color: #6e7681; border-top: solid #30363d; }
    Footer > .footer--key { background: #1f6feb; color: #ffffff; text-style: bold; }
    #workspace-title {
        height: auto; padding: 1 2; color: #e6edf3; background: #111820;
        border-left: thick #58a6ff; border-bottom: solid #30363d; text-style: bold;
    }
    #workspace-body { height: 1fr; }
    #session-context {
        width: 34; padding: 1 2; color: #a8b3c4; background: #0d1117;
        border-right: solid #30363d;
    }
    #activity, #report-content {
        height: 1fr; padding: 1 2; background: #0b0f14; color: #c9d1d9;
        scrollbar-background: #0b0f14; scrollbar-color: #30363d;
    }
    #command-bar {
        height: 3; margin: 0 1; border: tall #30363d; background: #0b0f14;
        color: #f0f6fc; padding-left: 1;
    }
    #command-bar:focus { border: tall #58a6ff; }
    #command-hint { height: 2; padding: 0 2; color: #6e7681; background: #0b0f14; }
    #title { padding: 1 2; color: #79c0ff; }
    #status { padding: 1 2; color: #8b949e; }
    DataTable { margin: 0 2; height: 1fr; background: #0d1117; border: tall #30363d; }
    """

    BINDINGS = [
        Binding("q", "quit", "退出"),
        Binding("d", "show_dashboard", "调查列表"),
    ]

    def __init__(
        self,
        runtime: RuntimeServices,
        *,
        investigation_id: str | None = None,
        show_report: bool = False,
    ) -> None:
        super().__init__()
        self.runtime = runtime
        self.investigation_id = investigation_id
        self.show_report = show_report

    def compose(self) -> ComposeResult:
        yield Footer()

    def on_mount(self) -> None:
        if self.investigation_id and self.show_report:
            self.push_screen(ReportScreen(self.investigation_id, self.runtime))
        elif self.investigation_id:
            self.push_screen(InvestigationScreen(self.investigation_id, self.runtime))
        else:
            self.push_screen(DashboardScreen(self.runtime))

    def action_show_dashboard(self) -> None:
        self.push_screen(DashboardScreen(self.runtime))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="incidentlens")
    subparsers = parser.add_subparsers(dest="command")
    for command in ("investigate", "report"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("investigation_id")
    return parser.parse_args()


def main() -> None:
    """CLI 入口：列表、调查会话或直接生成报告。"""
    args = _parse_args()
    settings = RuntimeSettings.from_environment()
    runtime = build_runtime(settings)
    app = IncidentLensApp(
        runtime,
        investigation_id=getattr(args, "investigation_id", None),
        show_report=args.command == "report",
    )
    app.run()


if __name__ == "__main__":
    main()
