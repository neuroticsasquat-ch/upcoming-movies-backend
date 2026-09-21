"""The shared catalog-event vocabulary — specifically the trailer half's subject-key tokens,
which are written by the video poll and read back by the public read models (D-35)."""

from upmovies.news.catalog_events import (
    CATALOG_EVENT_TYPES,
    COLLECTION_ATTACHED_EVENT_TYPE,
    COLLECTION_EVENT_TYPES,
    COLLECTION_REMOVED_EVENT_TYPE,
    TRAILER_EVENT_TYPE,
    video_key_of,
    video_subject_key,
)


def test_a_trailer_card_carries_one_prefixed_token():
    """`subject_key` is one column shared by every event type — a bare id in it could be read
    as a person's name on a casting card, so the namespace is part of the token."""
    assert video_subject_key("abc123") == ["youtube:abc123"]


def test_the_key_round_trips_through_the_token():
    assert video_key_of(TRAILER_EVENT_TYPE, video_subject_key("abc123")) == "abc123"


def test_a_key_containing_a_colon_survives_the_round_trip():
    """Only the first prefix is stripped; whatever follows is the key verbatim."""
    assert video_key_of(TRAILER_EVENT_TYPE, video_subject_key("a:b")) == "a:b"


def test_an_event_of_another_type_has_no_video_key():
    """A `now_available` card's `US:rent` tokens must never be read as a video id."""
    assert video_key_of("now_available", ["US:rent"]) is None


def test_a_story_born_trailer_card_has_no_video_key():
    """The outlets reported a trailer; we hold no video, so the page has nothing to embed and
    falls back to the card's sources."""
    assert video_key_of(TRAILER_EVENT_TYPE, None) is None
    assert video_key_of(TRAILER_EVENT_TYPE, []) is None


def test_a_trailer_card_with_only_foreign_tokens_has_no_video_key():
    assert video_key_of(TRAILER_EVENT_TYPE, ["Gal Gadot"]) is None


def test_trailer_is_not_a_catalog_dedup_type():
    """`trailer` has been in the LLM's own vocabulary since the story path shipped and is one
    of `link.cluster._SINGULAR_BEAT_TYPES`, so a trade story about the same trailer joins the
    poll's card through that rule. Adding it here would give it a second, overlapping one."""
    assert TRAILER_EVENT_TYPE not in CATALOG_EVENT_TYPES


# --- the collection types' registrations (EF-5, NEU-1434) -----------------------


def test_the_collection_types_are_registered_everywhere_the_vocabulary_is_enumerated():
    """EF-5 lists the sites by name, and every one of them fails silently when missed: an
    unregistered type ranks below everything on the arc, is admitted by no CHECK constraint,
    or reads as "Update" in the digest. `ck_event_type` itself is covered by the migration
    parity test (`tests/integration/test_migrations.py`)."""
    from upmovies.app.services.digest_sender import DIGEST_BEAT_LABELS
    from upmovies.link.cluster import _STALE_EVENT_TYPES
    from upmovies.news.visibility import HIDDEN_EVENT_TYPES
    from upmovies.public.arc import event_stage_rank

    for event_type in COLLECTION_EVENT_TYPES:
        assert event_type in DIGEST_BEAT_LABELS
        assert event_type not in HIDDEN_EVENT_TYPES

    # The arrival ranks on the arc; the departure deliberately does not, like `company_removed`
    # and `credit_removed` — a detachment is a correction to an arc, not a stage of one.
    assert event_stage_rank(COLLECTION_ATTACHED_EVENT_TYPE) == event_stage_rank("company_attached")
    assert event_stage_rank(COLLECTION_REMOVED_EVENT_TYPE) == -1
    assert COLLECTION_ATTACHED_EVENT_TYPE in _STALE_EVENT_TYPES


def test_the_collection_types_are_not_catalog_dedup_targets():
    """The cluster vocabulary has no organisation types at all until EF-12 gives it one, so
    there is no story-borne card for these to dedup against."""
    for event_type in COLLECTION_EVENT_TYPES:
        assert event_type not in CATALOG_EVENT_TYPES
