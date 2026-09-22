"""The decision pass's rules that need no database: the `now_available` store match, the
vocabulary bridge it depends on, and the run's detail line.

The pass itself is covered in `tests/integration/app/test_notify_pass.py`; what is here is the
half that would otherwise only ever be exercised through a fixture that happened to pick the
right monetization type.
"""

from uuid import uuid4

from upmovies.app.models import ALERT_STORES, DEFAULT_ALERT_STORES
from upmovies.app.services.notify_service import (
    ALERT_STORE_BY_MONETIZATION,
    ATTACHMENT_PUSH_TYPES,
    ENTITY_PUSH_TYPES,
    PUSH_TYPES,
    SEED_GRADE_TITLE_TYPES,
    TITLE_PUSH_TYPES,
    NotifyResult,
    Recipient,
    alert_reaches,
    confirmed_enough_to_push,
    notify_detail,
    now_available_matches_stores,
)
from upmovies.catalog.models import MONETIZATION_TYPES


def test_every_monetization_type_maps_to_a_real_alert_store():
    """The bridge between the two vocabularies, pinned from both ends (D-44, D-28).

    A fourth offer kind added to the poll with no store beside it would not fail: it would
    card `now_available` events that quietly match nobody's watchlist. Likewise a store
    renamed on the user-facing side. Both are a failing assertion here instead."""
    assert set(ALERT_STORE_BY_MONETIZATION) == set(MONETIZATION_TYPES)
    assert set(ALERT_STORE_BY_MONETIZATION.values()) == set(ALERT_STORES)


def test_flatrate_is_the_stream_store():
    """The one word the two vocabularies disagree on, and the default every user holds until
    they change it — so the mapping being wrong would silently mute the common case."""
    assert ALERT_STORE_BY_MONETIZATION["flatrate"] == "stream"
    assert now_available_matches_stores(["US:flatrate"], list(DEFAULT_ALERT_STORES))


def test_a_type_outside_the_stores_does_not_match():
    assert not now_available_matches_stores(["US:rent"], ["stream"])
    assert not now_available_matches_stores(["US:buy"], ["stream", "rent"])


def test_one_wanted_type_among_several_is_enough():
    """One observation can card rent and buy together, so the question is whether *any* token
    is wanted, not all of them."""
    assert now_available_matches_stores(["US:rent", "US:buy"], ["buy"])


def test_a_card_with_no_subject_key_matches_nothing():
    assert not now_available_matches_stores(None, ["stream", "rent", "buy"])
    assert not now_available_matches_stores([], ["stream", "rent", "buy"])


def test_a_token_with_no_monetization_half_matches_nothing_rather_than_raising():
    """This runs over the ledger on a schedule; one bad key must not cost every user their
    notifications for the day."""
    assert not now_available_matches_stores(["nonsense", "US:"], ["stream"])


def test_a_token_with_no_region_still_matches_on_its_type():
    """The permissive direction, deliberately. The region half is not part of the question —
    the poll tracks one region (`catalog.PRIMARY_REGION`) — so a card that somehow lost it is
    still a card saying the film is streamable, and muting it would be the worse failure."""
    assert now_available_matches_stores([":flatrate"], ["stream"])


def test_an_empty_store_setting_wants_nothing():
    """`alert_stores` is user-editable and may legitimately be emptied — that is how a user
    turns availability alerts off without unfollowing anything (D-44)."""
    assert not now_available_matches_stores(["US:flatrate"], [])


def test_the_title_push_set_is_ef7s_list():
    """EF-7's title arm in full: D-32's three beats, the cancellation, and the three credit
    beats the seed-grade floor then narrows. No studio or franchise type — which company
    financed a film you follow is timeline news."""
    assert TITLE_PUSH_TYPES == {
        "release_date",
        "trailer",
        "now_available",
        "canceled",
        "casting",
        "crew_attached",
        "credit_removed",
    }


def test_the_entity_push_set_is_the_attachment_stream_and_the_cancellation():
    """EF-7's entity arm. It is exactly what an entity follow delivers, so a beat about the
    film's own life — a date, a trailer, a streaming debut — is absent by construction."""
    assert ENTITY_PUSH_TYPES == {
        "casting",
        "crew_attached",
        "credit_removed",
        "company_attached",
        "company_removed",
        "collection_attached",
        "collection_removed",
        "canceled",
    }


def test_the_two_sets_disagree_about_the_films_own_beats_and_the_studios():
    """The whole point of splitting the list: neither set contains the other, so which follow
    reached the card is a question the pass has to answer rather than a formality."""
    assert TITLE_PUSH_TYPES - ENTITY_PUSH_TYPES == {"release_date", "trailer", "now_available"}
    assert ENTITY_PUSH_TYPES - TITLE_PUSH_TYPES == {
        "company_attached",
        "company_removed",
        "collection_attached",
        "collection_removed",
    }


def test_the_queried_type_list_is_the_union_of_the_two_arms():
    """`PUSH_TYPES` only exists to cut the alert query down before Python decides, so a type
    in either arm and missing from it would be a push silently unreachable in SQL."""
    assert set(PUSH_TYPES) == TITLE_PUSH_TYPES | ENTITY_PUSH_TYPES
    assert list(PUSH_TYPES) == sorted(PUSH_TYPES), "rendered into an IN; the order must be stable"


def test_canceled_is_not_an_attachment_type():
    """It is in both push sets and in neither provenance rule: a cancellation is a film-status
    beat that reaches entity followers, it is `confirmed` when carded, and nothing upgrades one
    in place (EF-8)."""
    assert "canceled" not in ATTACHMENT_PUSH_TYPES
    assert set(ATTACHMENT_PUSH_TYPES) == ENTITY_PUSH_TYPES - {"canceled"}
    assert list(ATTACHMENT_PUSH_TYPES) == sorted(ATTACHMENT_PUSH_TYPES)


def test_the_seed_grade_floor_covers_the_three_credit_beats_and_only_those():
    """EF-9 is a cut on the *title* arm. A studio attaching has no billing to grade, and the
    entity arm has no such floor at all."""
    assert SEED_GRADE_TITLE_TYPES == {"casting", "crew_attached", "credit_removed"}
    assert SEED_GRADE_TITLE_TYPES <= TITLE_PUSH_TYPES


def test_a_recipient_holds_the_default_stores_until_the_pass_reads_a_row():
    """`load_recipients` COALESCEs over an outer join, and the dataclass default is the other
    half of that: a user with no settings row is alerted on `{stream}` (D-44), not on
    nothing."""
    assert Recipient(user_id=uuid4(), deliverable=True).alert_stores == DEFAULT_ALERT_STORES


def test_a_deliverable_recipient_queues_and_everyone_else_is_suppressed():
    assert Recipient(user_id=uuid4(), deliverable=True).status == "queued"
    assert Recipient(user_id=uuid4(), deliverable=False).status == "suppressed"


def test_the_detail_line_reports_suppression_beside_the_queued_kinds():
    line = notify_detail(
        NotifyResult(
            users_considered=4,
            events_considered=9,
            alerts_queued=2,
            push_alerts_queued=1,
            digests_queued=5,
            suppressed=3,
        )
    )
    assert line == "notify: 9 events, 4 users, 2 alerts, 1 push, 5 digests, 3 suppressed, 0 failed"


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


def _reaches(event_type: str, **kw) -> bool:
    """`alert_reaches` with the defaults most cases do not care about: reached by neither
    follow, naming no seed-grade role, and the column-default stores."""
    kw.setdefault("via_title", False)
    kw.setdefault("via_entity", False)
    kw.setdefault("names_seed_role", False)
    kw.setdefault("subject_key", None)
    kw.setdefault("alert_stores", DEFAULT_ALERT_STORES)
    return alert_reaches(event_type, **kw)


def test_a_card_no_follow_reached_pushes_on_neither_arm():
    """The scope is still the scope. Both arms false is not a case the query produces — it
    filters on the OR — but the predicate is total and says so."""
    assert not _reaches("release_date")
    assert not _reaches("casting", names_seed_role=True)


def test_a_title_follow_pushes_on_the_films_own_beats():
    assert _reaches("release_date", via_title=True)
    assert _reaches("trailer", via_title=True)
    assert _reaches("canceled", via_title=True)


def test_a_title_follow_does_not_push_on_a_studio_joining():
    """`company_attached` is in the entity arm only. The film's follower reads it on their
    timeline; it does not interrupt them."""
    assert not _reaches("company_attached", via_title=True)
    assert _reaches("company_attached", via_entity=True)


def test_a_title_follow_on_a_credit_beat_stops_at_seed_grade():
    """EF-9. The same card, the same reach, decided entirely by whether the person it names
    holds a seed-grade role — a 12th-billed addition is digest-only."""
    for event_type in ("casting", "crew_attached", "credit_removed"):
        assert _reaches(event_type, via_title=True, names_seed_role=True)
        assert not _reaches(event_type, via_title=True, names_seed_role=False)


def test_an_entity_follow_on_a_credit_beat_has_no_seed_grade_floor():
    """You followed that performer. A 12th-billed role is exactly what you asked about, so the
    floor that protects the film's follower from it must not apply here (EF-7, EF-9)."""
    assert _reaches("casting", via_entity=True, names_seed_role=False)


def test_a_card_both_follows_reach_pushes_on_whichever_arm_admits_it():
    """The case the two-boolean signature exists for. A non-seed casting card fails the title
    arm and passes the entity one; the user holding both follows is owed the push once."""
    assert _reaches("casting", via_title=True, via_entity=True, names_seed_role=False)
    assert _reaches("release_date", via_title=True, via_entity=True)


def test_now_available_still_answers_to_the_store_setting():
    """D-44 survives EF-7 unchanged — and only on the title arm, since `now_available` is not
    in the entity set at all."""
    assert _reaches("now_available", via_title=True, subject_key=["US:flatrate"])
    assert not _reaches("now_available", via_title=True, subject_key=["US:rent"])
    assert not _reaches("now_available", via_entity=True, subject_key=["US:flatrate"])


def test_a_type_in_neither_set_never_pushes():
    assert not _reaches("production_start", via_title=True, via_entity=True)


def test_an_ordinary_beat_waits_for_confirmed():
    """D-32's floor, unchanged outside the attachment types."""
    assert confirmed_enough_to_push("release_date", "confirmed", "catalog")
    assert not confirmed_enough_to_push("release_date", "rumored", "catalog")
    assert not confirmed_enough_to_push("canceled", "rumored", "catalog")


def test_a_catalog_attachment_is_confirmed_by_construction():
    """EF-8: it is written `rumored` because any TMDB editor can add a credit, and published
    only once D-3's quarantine has passed. Surviving quarantine is the confirmation, so
    waiting for a `confidence` flip nothing writes would silence the whole stream."""
    for event_type in ATTACHMENT_PUSH_TYPES:
        assert confirmed_enough_to_push(event_type, "rumored", "catalog")


def test_a_rumored_story_attachment_waits():
    """EF-10: "in talks" is the card that waits, and the push arrives on the upgrade."""
    for event_type in ATTACHMENT_PUSH_TYPES:
        assert not confirmed_enough_to_push(event_type, "rumored", "story")


def test_a_confirmed_story_attachment_pushes_immediately():
    """EF-11."""
    for event_type in ATTACHMENT_PUSH_TYPES:
        assert confirmed_enough_to_push(event_type, "confirmed", "story")
