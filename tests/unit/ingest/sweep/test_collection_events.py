"""The collection phase's pure halves: the three transitions one `collection_id` field change
resolves into (EF-5), the round-trip marker, and burst grouping (D-7)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from upmovies.ingest.sweep.collection_events import (
    COLLECTION_ADDED,
    COLLECTION_REMOVED,
    CollectionChangeRow,
    collection_field_events,
    group_collection_changes,
    mark_window_reverts,
)

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
FILM = uuid4()
OTHER_FILM = uuid4()


def _row(collection_id: int, name: str, **overrides) -> CollectionChangeRow:
    kwargs = {
        "film_id": FILM,
        "collection_id": collection_id,
        "name": name,
        "change": COLLECTION_ADDED,
        "changed_at": NOW,
    }
    return CollectionChangeRow(**{**kwargs, **overrides})


# --- the three transitions -----------------------------------------------------------------


def test_a_first_collection_is_one_attachment():
    assert collection_field_events(None, 726871) == ((COLLECTION_ADDED, 726871),)


def test_a_cleared_collection_is_one_removal():
    assert collection_field_events(726871, None) == ((COLLECTION_REMOVED, 726871),)


def test_a_move_is_one_removal_of_the_old_and_one_attachment_of_the_new():
    """The one transition in the sweep that cards twice from a single history row, and the
    departure reads first so a pair rendered together reads as a move."""
    assert collection_field_events(726871, 999) == (
        (COLLECTION_REMOVED, 726871),
        (COLLECTION_ADDED, 999),
    )


def test_an_unchanged_value_is_no_beat():
    assert collection_field_events(726871, 726871) == ()
    assert collection_field_events(None, None) == ()


def test_a_non_integer_side_is_dropped_rather_than_guessed_at():
    """`classify_field_change`'s rule: data this was not written against raises no event.
    `True` is the case worth pinning — it is an `int` subclass and would card as collection 1.
    """
    assert collection_field_events("726871", None) == ()
    assert collection_field_events(None, True) == ()
    assert collection_field_events(True, 5) == ((COLLECTION_ADDED, 5),)


# --- the round-trip marker -----------------------------------------------------------------


def test_a_removal_following_an_attachment_in_the_window_is_marked():
    """A revert writes both rows, and only the history says the departure is the far side of
    an arrival rather than a film genuinely leaving a franchise."""
    marked = mark_window_reverts(
        [
            _row(1, "Dune Collection", changed_at=NOW - timedelta(days=2)),
            _row(1, "Dune Collection", change=COLLECTION_REMOVED),
        ]
    )

    assert [c.reverted_in_window for c in marked] == [False, True]


def test_an_attachment_following_a_removal_in_the_window_is_marked_too():
    """Where the studio half pairs one direction, a scalar column flaps both ways: a cleared
    `collection_id` put back is an arrival at a franchise the film was never seen to leave."""
    marked = mark_window_reverts(
        [
            _row(1, "Dune Collection", change=COLLECTION_REMOVED, changed_at=NOW - timedelta(2)),
            _row(1, "Dune Collection"),
        ]
    )

    assert [c.reverted_in_window for c in marked] == [False, True]


def test_a_change_with_no_opposite_in_the_window_is_not_marked():
    """A film filed under a franchise since its baseline has no `added` row at all, and its
    departure is a complete beat."""
    marked = mark_window_reverts([_row(1, "Dune Collection", change=COLLECTION_REMOVED)])

    assert marked[0].reverted_in_window is False


def test_the_marker_is_per_collection():
    marked = mark_window_reverts(
        [
            _row(1, "Dune Collection", changed_at=NOW - timedelta(days=2)),
            _row(2, "Alien Collection", change=COLLECTION_REMOVED),
        ]
    )

    assert marked[1].reverted_in_window is False


# --- grouping ------------------------------------------------------------------------------


def test_a_move_released_together_is_two_groups_on_one_film():
    groups = group_collection_changes(
        [_row(1, "Dune Collection", change=COLLECTION_REMOVED), _row(2, "Alien Collection")]
    )

    assert {g.event_type for g in groups} == {"collection_attached", "collection_removed"}


def test_two_arrivals_clearing_quarantine_in_one_pass_are_one_group():
    """D-7, reached here only across observations: a film cannot hold two franchises at once,
    but it can have visited two inside one window."""
    groups = group_collection_changes(
        [
            _row(1, "Dune Collection", changed_at=NOW - timedelta(days=2)),
            _row(2, "Alien Collection"),
        ]
    )

    assert len(groups) == 1
    assert [c.collection_id for c in groups[0].changes] == [1, 2]  # oldest first


def test_the_group_is_dated_by_its_latest_change():
    """The latest rather than the earliest, so a pass collapsing a larger burst lands on a
    later timestamp and never collides under `uq_event_catalog_change`."""
    groups = group_collection_changes(
        [
            _row(1, "Dune Collection", changed_at=NOW - timedelta(days=2)),
            _row(2, "Alien Collection", changed_at=NOW),
        ]
    )

    assert groups[0].changed_at == NOW


def test_two_films_are_two_groups():
    groups = group_collection_changes(
        [_row(1, "Dune Collection"), _row(1, "Dune Collection", film_id=OTHER_FILM)]
    )

    assert {g.film_id for g in groups} == {FILM, OTHER_FILM}


def test_one_collection_appears_once_per_observation():
    groups = group_collection_changes([_row(1, "Dune Collection"), _row(1, "Dune Collection")])

    assert len(groups[0].changes) == 1


def test_a_re_arrival_at_another_instant_survives_the_dedupe():
    """Join, leave, re-join puts two `added` rows in one backlog, and they are two beats —
    whether the second shares a body with the first is the suppression check's question."""
    groups = group_collection_changes(
        [
            _row(1, "Dune Collection", changed_at=NOW - timedelta(days=3)),
            _row(1, "Dune Collection"),
        ]
    )

    assert len(groups[0].changes) == 2
