"""The shared catalog-event vocabulary — specifically the trailer half's subject-key tokens,
which are written by the video poll and read back by the public read models (D-35)."""

from upmovies.news.catalog_events import (
    CATALOG_EVENT_TYPES,
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
