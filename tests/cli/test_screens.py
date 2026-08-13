"""Dashboard 屏幕测试：仅实例化，不启动 App/driver。"""

from incidentlens_control_plane.cli.screens.approvals import ApprovalsScreen
from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen
from incidentlens_control_plane.cli.screens.evidence import EvidenceScreen
from incidentlens_control_plane.cli.screens.investigation import InvestigationScreen
from incidentlens_control_plane.cli.screens.logs import LogsScreen
from incidentlens_control_plane.cli.screens.report import ReportScreen
from incidentlens_control_plane.cli.widgets.timeline import TimelineWidget
from incidentlens_control_plane.cli.widgets.tool_call_flow import ToolCallFlowWidget


def test_dashboard_screen_can_be_instantiated():
    screen = DashboardScreen()
    assert screen is not None


def test_dashboard_screen_title():
    screen = DashboardScreen()
    assert screen.TITLE == "Dashboard"


def test_investigation_screen_can_be_instantiated():
    screen = InvestigationScreen(investigation_id="inv-test")
    assert screen is not None


def test_approvals_screen_can_be_instantiated():
    screen = ApprovalsScreen()
    assert screen is not None


def test_logs_screen_can_be_instantiated():
    screen = LogsScreen()
    assert screen is not None


def test_evidence_screen_can_be_instantiated():
    screen = EvidenceScreen(evidence_id="ev-test")
    assert screen is not None


def test_report_screen_can_be_instantiated():
    screen = ReportScreen(investigation_id="inv-test")
    assert screen is not None


def test_timeline_widget_can_be_instantiated():
    widget = TimelineWidget()
    assert widget is not None


def test_tool_call_flow_widget_can_be_instantiated():
    widget = ToolCallFlowWidget()
    assert widget is not None
