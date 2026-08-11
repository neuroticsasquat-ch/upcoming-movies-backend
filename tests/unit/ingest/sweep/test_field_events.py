"""Classifying one `catalog.film_field_change` row into an event, with no DB in sight.

The rules this pins are the ones a reader of the ticket would check first: which fields card
at all, that a date *removal* is not a date move, and that a status TMDB may add tomorrow is
ignored rather than carded under a body nobody wrote.
"""

from datetime import date

from upmovies.ingest.sweep.field_events import classify_field_change
from upmovies.synthesize.deterministic import (
    ReleaseDateMoved,
    ReleaseDateSet,
    StatusChanged,
)


def test_first_release_date_is_a_release_date_event():
    carded = classify_field_change("release_date", None, "2026-08-14")

    assert carded is not None
    assert carded.event_type == "release_date"
    assert carded.change == ReleaseDateSet(new_date=date(2026, 8, 14))


def test_moved_release_date_carries_both_dates():
    carded = classify_field_change("release_date", "2026-08-14", "2026-11-20")

    assert carded is not None
    assert carded.change == ReleaseDateMoved(
        previous_date=date(2026, 8, 14), new_date=date(2026, 11, 20)
    )


def test_removed_release_date_is_not_an_event():
    # "assigned or moved" — a date TMDB withdrew is neither, and there is no date to render.
    assert classify_field_change("release_date", "2026-08-14", None) is None


def test_unparseable_release_date_is_not_an_event():
    assert classify_field_change("release_date", None, "soon") is None


def test_unparseable_previous_date_still_cards_as_a_first_date():
    # The new date is what the body renders; a previous value we cannot read costs the
    # "moved from" clause, not the event.
    carded = classify_field_change("release_date", "TBA", "2026-11-20")

    assert carded is not None
    assert carded.change == ReleaseDateSet(new_date=date(2026, 11, 20))


def test_status_into_production_is_a_production_start():
    carded = classify_field_change("status", "Planned", "In Production")

    assert carded is not None
    assert carded.event_type == "production_start"
    assert carded.change == StatusChanged(new_status="In Production")


def test_status_into_post_production_is_a_production_wrap():
    carded = classify_field_change("status", "In Production", "Post Production")

    assert carded is not None
    assert carded.event_type == "production_wrap"


def test_other_status_transitions_do_not_card():
    # Released and Canceled are real transitions with no event type in scope; an unknown one
    # is new data from TMDB, and both are dropped rather than guessed at.
    assert classify_field_change("status", "Post Production", "Released") is None
    assert classify_field_change("status", "Planned", "Canceled") is None
    assert classify_field_change("status", "Planned", "In Limbo") is None


def test_untracked_fields_do_not_card():
    assert classify_field_change("title", "Old", "New") is None
    assert classify_field_change("runtime", 90, 120) is None
