"""Classifying one `catalog.film_field_change` row into an event, with no DB in sight.

The rules this pins are the ones a reader of the ticket would check first: which fields card
at all, and that a status TMDB may add tomorrow is ignored rather than carded under a body
nobody wrote.
"""

from upmovies.ingest.sweep.field_events import TRACKED_FIELDS, classify_field_change
from upmovies.synthesize.deterministic import StatusChanged


def test_the_primary_release_date_no_longer_cards_here():
    # NEU-1121: `film.release_date` is TMDB's earliest release in any country of any type,
    # while the page lists US-or-origin theatrical dates — so carding it named dates the page
    # never showed. Displayable dates card from `film_release_date_change` instead.
    assert classify_field_change("release_date", None, "2026-08-14") is None
    assert classify_field_change("release_date", "2026-08-14", "2026-11-20") is None
    assert "release_date" not in TRACKED_FIELDS


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
