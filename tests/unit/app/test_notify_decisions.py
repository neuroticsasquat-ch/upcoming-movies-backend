"""The decision pass's rules that need no database: the `now_available` preference match, the
vocabulary bridge it depends on, and the run's detail line.

The pass itself is covered in `tests/integration/app/test_notify_pass.py`; what is here is the
half that would otherwise only ever be exercised through a fixture that happened to pick the
right monetization type.
"""

from uuid import uuid4

from upmovies.app.models import ALERT_PREFS, DEFAULT_ALERT_PREFS
from upmovies.app.services.notify_service import (
    ALERT_PREF_BY_MONETIZATION,
    PUSH_WHITELIST,
    NotifyResult,
    Recipient,
    notify_detail,
    now_available_matches_prefs,
)
from upmovies.catalog.models import MONETIZATION_TYPES


def test_every_monetization_type_maps_to_a_real_alert_preference():
    """The bridge between the two vocabularies, pinned from both ends (D-14, D-28).

    A fourth offer kind added to the poll with no preference beside it would not fail: it would
    card `now_available` events that quietly match nobody's watchlist. Likewise a preference
    renamed on the user-facing side. Both are a failing assertion here instead."""
    assert set(ALERT_PREF_BY_MONETIZATION) == set(MONETIZATION_TYPES)
    assert set(ALERT_PREF_BY_MONETIZATION.values()) == set(ALERT_PREFS)


def test_flatrate_is_the_stream_preference():
    """The one word the two vocabularies disagree on, and the default every watchlist item is
    written with — so the mapping being wrong would silently mute the common case."""
    assert ALERT_PREF_BY_MONETIZATION["flatrate"] == "stream"
    assert now_available_matches_prefs(["US:flatrate"], list(DEFAULT_ALERT_PREFS))


def test_a_type_outside_the_prefs_does_not_match():
    assert not now_available_matches_prefs(["US:rent"], ["stream"])
    assert not now_available_matches_prefs(["US:buy"], ["stream", "rent"])


def test_one_wanted_type_among_several_is_enough():
    """One observation can card rent and buy together, so the question is whether *any* token
    is wanted, not all of them."""
    assert now_available_matches_prefs(["US:rent", "US:buy"], ["buy"])


def test_a_card_with_no_subject_key_matches_nothing():
    assert not now_available_matches_prefs(None, ["stream", "rent", "buy"])
    assert not now_available_matches_prefs([], ["stream", "rent", "buy"])


def test_a_token_with_no_monetization_half_matches_nothing_rather_than_raising():
    """This runs over the ledger on a schedule; one bad key must not cost every user their
    notifications for the day."""
    assert not now_available_matches_prefs(["nonsense", "US:"], ["stream"])


def test_a_token_with_no_region_still_matches_on_its_type():
    """The permissive direction, deliberately. The region half is not part of the question —
    the poll tracks one region (`catalog.PRIMARY_REGION`) — so a card that somehow lost it is
    still a card saying the film is streamable, and muting it would be the worse failure."""
    assert now_available_matches_prefs([":flatrate"], ["stream"])


def test_an_empty_preference_set_wants_nothing():
    """`alert_prefs` is user-editable and may legitimately be emptied — that is how a user
    turns availability alerts off without leaving the watchlist."""
    assert not now_available_matches_prefs(["US:flatrate"], [])


def test_the_push_whitelist_is_d32s_three_beats():
    assert set(PUSH_WHITELIST) == {"release_date", "now_available", "trailer"}


def test_a_deliverable_recipient_queues_and_everyone_else_is_suppressed():
    assert Recipient(user_id=uuid4(), deliverable=True).status == "queued"
    assert Recipient(user_id=uuid4(), deliverable=False).status == "suppressed"


def test_the_detail_line_reports_suppression_beside_the_queued_kinds():
    line = notify_detail(
        NotifyResult(
            users_considered=4,
            events_considered=9,
            alerts_queued=2,
            digests_queued=5,
            suppressed=3,
        )
    )
    assert line == "notify: 9 events, 4 users, 2 alerts, 5 digests, 3 suppressed, 0 failed"


def test_an_aborted_pass_says_so_on_the_same_line():
    line = notify_detail(
        NotifyResult(failures=10, aborted=True, abort_error="aborted after 10 consecutive failures")
    )
    assert line.endswith("; notify aborted: aborted after 10 consecutive failures")


def test_a_cold_start_line_does_not_read_as_a_quiet_day():
    """`0 alerts, 0 digests` is what a healthy quiet night looks like too, so the first run
    ever has to say what it actually did."""
    assert notify_detail(NotifyResult(cold_start=True)) == (
        "notify: cold start — watermark established, nothing queued"
    )
