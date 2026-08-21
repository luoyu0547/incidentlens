"""Updateable semantic event card."""

from textual.widgets import Static

from incidentlens_control_plane.cli.presentation import EventPresentation, render_markup


class EventCard(Static):
    """A stable card that may be updated by an internal tool-call identity."""

    def __init__(self, presentation: EventPresentation) -> None:
        super().__init__(render_markup(presentation))
        self.event_key = presentation.key

    def apply(self, presentation: EventPresentation) -> None:
        self.event_key = presentation.key
        self.update(render_markup(presentation))


__all__ = ["EventCard"]
