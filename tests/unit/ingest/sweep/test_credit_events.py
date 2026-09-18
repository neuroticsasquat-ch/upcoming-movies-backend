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


def _attached(person_id, name, role, *, film_id=FILM, changed_at=AT, credit_order=None):
    return AttachedCredit(
        film_id=film_id,
        person_id=person_id,
        name=name,
        role=role,
        changed_at=changed_at,
        credit_order=credit_order,
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
            _attached(1, "Timothée Chalamet", "cast", credit_order=0),
            _attached(2, "Zendaya", "cast", credit_order=1),
            _attached(3, "Rebecca Ferguson", "cast", credit_order=2),
        ]
    )

    assert len(groups) == 1
    group = groups[0]
    assert group.event_type == "casting"
    assert group.changed_at == AT
    assert group.credits == (
        CreditAttached(role="cast", name="Timothée Chalamet", credit_order=0),
        CreditAttached(role="cast", name="Zendaya", credit_order=1),
        CreditAttached(role="cast", name="Rebecca Ferguson", credit_order=2),
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


def test_observations_clearing_in_one_pass_collapse_into_one_card():
    """D-7 reverses per-observation grouping. Quarantine releases a film's credits together
    however many observations they arrived over, so the pass — not the timestamp — is the
    key, and the card is dated at the latest change it names."""
    groups = group_attachments(
        [_attached(1, "Zendaya", "cast"), _attached(2, "Josh Brolin", "cast", changed_at=LATER)]
    )

    assert len(groups) == 1
    assert groups[0].changed_at == LATER
    assert [c.name for c in groups[0].credits] == ["Zendaya", "Josh Brolin"]


def test_a_burst_over_four_days_is_one_card_naming_everyone():
    """The done-when case: six credits spread over four days, all clearing together."""
    days = [datetime(2026, 8, day, 2, 0, tzinfo=UTC) for day in (6, 6, 7, 8, 9, 9)]
    groups = group_attachments(
        [
            _attached(i, f"Performer {i}", "cast", changed_at=day, credit_order=i)
            for i, day in enumerate(days)
        ]
    )

    assert len(groups) == 1
    assert len(groups[0].credits) == 6
    assert groups[0].changed_at == datetime(2026, 8, 9, 2, 0, tzinfo=UTC)


def test_a_collapsed_group_names_its_cast_in_billing_order():
    """The body's only ranking, so it comes from `credit_order` rather than from whichever
    order the history diff emitted."""
    groups = group_attachments(
        [
            _attached(1, "Third Billed", "cast", changed_at=LATER, credit_order=2),
            _attached(2, "Top Billed", "cast", credit_order=0),
            _attached(3, "Second Billed", "cast", credit_order=1),
        ]
    )

    assert [c.name for c in groups[0].credits] == ["Top Billed", "Second Billed", "Third Billed"]


def test_an_unbilled_credit_sorts_after_every_billed_one():
    """A cast credit the quarantine gate never stamped (the gate disabled) must not displace
    one that carries a real billing position."""
    groups = group_attachments(
        [
            _attached(1, "Unbilled", "cast"),
            _attached(2, "Top Billed", "cast", credit_order=0),
        ]
    )

    assert [c.name for c in groups[0].credits] == ["Top Billed", "Unbilled"]


def test_crew_credits_keep_seed_grade_role_order():
    """`ROLE_ORDER` is the only ranking crew has — no `credit_order` — so a writer read
    first by the diff still renders behind the director."""
    groups = group_attachments(
        [
            _attached(1, "Jon Spaihts", "writer"),
            _attached(2, "Denis Villeneuve", "director", changed_at=LATER),
        ]
    )

    assert [c.role for c in groups[0].credits] == ["director", "writer"]


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
