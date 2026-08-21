"""Live screen contracts."""

from pathlib import Path

from incidentlens_control_plane.cli.screens.investigation import InvestigationScreen


def test_activity_stream_is_append_only() -> None:
    source = Path(
        "apps/control-plane/src/incidentlens_control_plane/cli/screens/investigation.py"
    ).read_text()
    assert "log.clear()" not in source
    assert "broker.subscribe()" in source
    assert "_backfill_events" in source


def test_screen_tracks_event_sequence() -> None:
    screen = InvestigationScreen("inv-1")
    assert screen._last_event_sequence == 0
    assert screen._rendered_event_sequences == set()
