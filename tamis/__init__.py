"""edt-tamis: one iCalendar feed with only your university courses, from ADE exports."""

__version__ = "0.3.0"
APP = "edt-tamis"


class EdtError(Exception):
    """A problem the user can fix (bad config, unreachable source, ...)."""
