"""The company phase's pure halves: burst grouping (D-7) and the per-observation dedupe."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from upmovies.ingest.sweep.company_events import (
    CompanyChangeRow,
    group_company_changes,
    mark_window_attachments,
)
from upmovies.ingest.tmdb.company_history import COMPANY_ADDED, COMPANY_REMOVED

NOW = datetime(2026, 9, 20, 2, 0, tzinfo=UTC)
FILM = uuid4()
OTHER_FILM = uuid4()


def _row(company_id: int, name: str, **overrides) -> CompanyChangeRow:
    kwargs = {
        "film_id": FILM,
        "company_id": company_id,
        "name": name,
        "change": COMPANY_ADDED,
        "changed_at": NOW,
    }
    return CompanyChangeRow(**{**kwargs, **overrides})


def test_one_pass_collapses_a_film_s_attachments_into_one_group():
    """D-7: three studios clearing quarantine in the same pass are one beat and one card,
    whatever days they were each observed on."""
    groups = group_company_changes(
        [
            _row(1, "Legendary Pictures"),
            _row(2, "Warner Bros. Pictures", changed_at=NOW - timedelta(days=2)),
            _row(3, "Atlas Entertainment", changed_at=NOW - timedelta(days=1)),
        ]
    )

    assert len(groups) == 1
    assert [c.company_id for c in groups[0].changes] == [3, 1, 2]  # by name


def test_the_group_is_dated_by_its_latest_change():
    """The latest rather than the earliest, so a pass collapsing a larger burst lands on a
    later timestamp and never collides under `uq_event_catalog_change`."""
    groups = group_company_changes(
        [_row(1, "A", changed_at=NOW - timedelta(days=2)), _row(2, "B", changed_at=NOW)]
    )

    assert groups[0].changed_at == NOW


def test_attachments_and_detachments_are_separate_groups_on_one_film():
    groups = group_company_changes([_row(1, "A"), _row(2, "B", change=COMPANY_REMOVED)])

    assert {g.event_type for g in groups} == {"company_attached", "company_removed"}


def test_two_films_are_two_groups():
    groups = group_company_changes([_row(1, "A"), _row(1, "A", film_id=OTHER_FILM)])

    assert {g.film_id for g in groups} == {FILM, OTHER_FILM}


def test_one_company_appears_once_per_observation():
    """A duplicate row for one company at one instant is one attachment, not "A and A join
    the production."""
    groups = group_company_changes([_row(1, "A"), _row(1, "A")])

    assert len(groups[0].changes) == 1


def test_a_re_attachment_at_another_instant_survives_the_dedupe():
    """Attach, detach, re-attach puts two `added` rows in one backlog, and they are two beats
    — whether the second shares a body with the first is the suppression check's question,
    asked later and against what was actually carded."""
    groups = group_company_changes([_row(1, "A", changed_at=NOW - timedelta(days=3)), _row(1, "A")])

    assert len(groups[0].changes) == 2


def test_a_removal_following_an_attachment_in_the_window_is_marked():
    """The round-trip marker: a revert writes both rows, and only the history says the
    departure is the far side of an attachment rather than a studio leaving for good."""
    marked = mark_window_attachments(
        [
            _row(1, "A", changed_at=NOW - timedelta(days=2)),
            _row(1, "A", change=COMPANY_REMOVED),
        ]
    )

    assert [c.attached_in_window for c in marked] == [False, True]


def test_a_removal_with_no_attachment_in_the_window_is_not_marked():
    """A company attached since the film's baseline has no `added` row at all, and its
    departure is a complete beat."""
    marked = mark_window_attachments([_row(1, "A", change=COMPANY_REMOVED)])

    assert marked[0].attached_in_window is False


def test_the_marker_is_per_company():
    marked = mark_window_attachments(
        [
            _row(1, "A", changed_at=NOW - timedelta(days=2)),
            _row(2, "B", change=COMPANY_REMOVED),
        ]
    )

    assert marked[1].attached_in_window is False


def test_an_attachment_after_a_removal_does_not_mark_it():
    """Order matters: the attachment has to precede the departure for the two to be one round
    trip. A studio leaving and coming back is the opposite sequence."""
    marked = mark_window_attachments(
        [
            _row(1, "A", change=COMPANY_REMOVED, changed_at=NOW - timedelta(days=2)),
            _row(1, "A"),
        ]
    )

    assert marked[0].attached_in_window is False
