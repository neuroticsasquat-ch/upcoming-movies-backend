import datetime as _dt

import pytest

from upmovies.link.cluster import is_stale_stage

# A fixed run date used across the stale-stage tests.
AS_OF = _dt.date(2026, 7, 1)
PAST = _dt.date(2026, 1, 1)  # released before the run date
FUTURE = _dt.date(2026, 12, 1)  # not yet released


@pytest.mark.parametrize(
    "event_type", ["announced", "casting", "production_start", "production_wrap"]
)
@pytest.mark.parametrize("status", ["Post Production", "Released"])
def test_early_stage_on_wrapped_film_is_stale(event_type, status):
    # Existing status-based rule still fires regardless of release date.
    assert is_stale_stage(event_type, status, None, AS_OF) is True


@pytest.mark.parametrize(
    "event_type", ["announced", "casting", "production_start", "production_wrap"]
)
def test_early_stage_on_already_released_film_is_stale(event_type):
    # New rule: release_date already past the run date, even if status lags.
    assert is_stale_stage(event_type, "In Production", PAST, AS_OF) is True


@pytest.mark.parametrize(
    "event_type", ["announced", "casting", "production_start", "production_wrap"]
)
@pytest.mark.parametrize("status", ["In Production", "Planned", "Rumored", None])
def test_early_stage_on_unwrapped_upcoming_film_is_not_stale(event_type, status):
    # Future release date + non-wrapped status → not stale.
    assert is_stale_stage(event_type, status, FUTURE, AS_OF) is False
    # Unknown release date + non-wrapped status → not stale.
    assert is_stale_stage(event_type, status, None, AS_OF) is False


@pytest.mark.parametrize("event_type", ["trailer", "release_date", "first_look", "other"])
def test_non_early_stage_types_are_never_stale(event_type):
    assert is_stale_stage(event_type, "Released", PAST, AS_OF) is False
    assert is_stale_stage(event_type, "In Production", FUTURE, AS_OF) is False


def test_instructions_name_sibling_spinoff_as_off_topic():
    from upmovies.link.cluster import _INSTRUCTIONS

    lowered = _INSTRUCTIONS.lower()
    assert "spin-off" in lowered
    assert "off_topic" in lowered  # the sentinel is already documented; keep it asserted
