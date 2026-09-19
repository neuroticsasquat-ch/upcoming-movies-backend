"""Typed accessors over the `icalendar` parser, shared by the writer's unit tests and the
route's integration tests (D-34).

`icalendar` types a component's `__getitem__` as the union of every property class it knows, so
`vevent["dtstart"].dt` does not typecheck even though it is the library's documented usage.
These three helpers narrow it in one place, and the `isinstance` that does the narrowing is a
real assertion rather than a cast: it is what catches a DTSTART emitted as TEXT instead of a
date value, which is exactly the class of bug a hand-rolled writer produces.
"""

from datetime import date, datetime

from icalendar import Calendar
from icalendar.cal import Component
from icalendar.prop import vDDDTypes


def events(body: str) -> list[Component]:
    """The VEVENTs in a rendered calendar, in document order."""
    return Calendar.from_ical(body).walk("VEVENT")


def prop_dt(event: Component, name: str) -> date | datetime:
    """A date or date-time property's value. A `date` (not a `datetime`) is how the parser
    reports `VALUE=DATE`, i.e. an all-day event."""
    value = event[name]
    assert isinstance(value, vDDDTypes), f"{name} is {type(value).__name__}, not a date value"
    # `vDDDTypes` also carries times and durations. `date` admits `datetime` (its subclass), so
    # this passes a DTSTART emitted as VALUE=DATE and a DTSTAMP alike, and fails anything that
    # is neither.
    parsed = value.dt
    assert isinstance(parsed, date), f"{name} parsed as {type(parsed).__name__}, not a date"
    return parsed


def prop_text(event: Component, name: str) -> str:
    """A TEXT property's value, unescaped by the parser."""
    return str(event[name])
