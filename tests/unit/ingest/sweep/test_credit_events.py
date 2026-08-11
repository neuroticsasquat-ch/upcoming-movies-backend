"""Turning `catalog.film_credit_change` rows into event-shaped groups, with no DB in sight.

The rule with teeth here is the grouping: TMDB gains a whole top-billed cast between two
ingests, and the difference between one card and five is entirely in this function.
"""

from datetime import UTC, datetime
from uuid import uuid4

from upmovies.ingest.sweep.credit_events import (
    AttachedCredit,
    credit_role,
    group_attachments,
)
from upmovies.synthesize.deterministic import CreditAttached

FILM = uuid4()
OTHER_FILM = uuid4()
AT = datetime(2026, 8, 9, 2, 0, tzinfo=UTC)
LATER = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)


def _attached(person_id, name, role, *, film_id=FILM, changed_at=AT):
    return AttachedCredit(
        film_id=film_id, person_id=person_id, name=name, role=role, changed_at=changed_at
    )


def test_cast_and_crew_carry_the_roles_the_seed_grade_defines():
    assert credit_role("crew", "Director") == "director"
    assert credit_role("crew", "Screenplay") == "writer"
    assert credit_role("cast", None) == "cast"


def test_a_credit_outside_the_seed_grade_has_no_role():
    # The history only records seed-grade credits, so this is a defensive drop rather than a
    # live case — but a role the renderer has no template for must never reach it.
    assert credit_role("crew", "Executive Producer") is None
    assert credit_role("sound", None) is None


def test_cast_added_in_one_observation_become_one_casting_group():
    groups = group_attachments(
        [
            _attached(1, "Timothée Chalamet", "cast"),
            _attached(2, "Zendaya", "cast"),
            _attached(3, "Rebecca Ferguson", "cast"),
        ]
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.event_type == "casting"
    assert group.changed_at == AT
    assert group.credits == (
        CreditAttached(role="cast", name="Timothée Chalamet"),
        CreditAttached(role="cast", name="Zendaya"),
        CreditAttached(role="cast", name="Rebecca Ferguson"),
    )


def test_a_director_and_a_writer_in_one_observation_are_one_crew_group():
    groups = group_attachments(
        [_attached(1, "Denis Villeneuve", "director"), _attached(2, "Jon Spaihts", "writer")]
    )

    assert len(groups) == 1
    assert groups[0].event_type == "crew_attached"
    assert [c.role for c in groups[0].credits] == ["director", "writer"]


def test_cast_and_crew_in_one_observation_are_two_groups():
    """One beat each: `casting` is an existing type with its own meaning, and a card naming
    the director and the third-billed performer in one body is neither beat."""
    groups = group_attachments(
        [_attached(1, "Denis Villeneuve", "director"), _attached(2, "Zendaya", "cast")]
    )

    assert [g.event_type for g in groups] == ["crew_attached", "casting"]


def test_observations_at_different_times_never_merge():
    """`occurred_at` is the change's own timestamp and `uq_event_catalog_change` keys on it,
    so two observations are two cards however close together they land."""
    groups = group_attachments(
        [_attached(1, "Zendaya", "cast"), _attached(2, "Josh Brolin", "cast", changed_at=LATER)]
    )

    assert [g.changed_at for g in groups] == [AT, LATER]


def test_films_never_merge():
    groups = group_attachments(
        [_attached(1, "Zendaya", "cast"), _attached(2, "Josh Brolin", "cast", film_id=OTHER_FILM)]
    )

    assert {g.film_id for g in groups} == {FILM, OTHER_FILM}


def test_the_same_person_twice_in_one_observation_is_named_once():
    """A person who both wrote and directed holds two seed-grade credits, and both are
    genuine history rows. The group keeps both; the renderer gives each role its own clause,
    so neither is dropped and neither reads as a repeat."""
    groups = group_attachments(
        [_attached(1, "Denis Villeneuve", "director"), _attached(1, "Denis Villeneuve", "writer")]
    )

    assert len(groups) == 1
    assert groups[0].credits == (
        CreditAttached(role="director", name="Denis Villeneuve"),
        CreditAttached(role="writer", name="Denis Villeneuve"),
    )


def test_nothing_read_is_nothing_grouped():
    assert group_attachments([]) == []
