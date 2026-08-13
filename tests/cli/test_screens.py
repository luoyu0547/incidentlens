"""Dashboard 屏幕测试：仅实例化，不启动 App/driver。"""

from incidentlens_control_plane.cli.screens.dashboard import DashboardScreen


def test_dashboard_screen_can_be_instantiated():
    screen = DashboardScreen()
    assert screen is not None


def test_dashboard_screen_title():
    screen = DashboardScreen()
    assert screen.TITLE == "Dashboard"
