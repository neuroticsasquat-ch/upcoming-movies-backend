"""The organisation half of the cluster reply (EF-12): the widened event vocabulary, and the
pure parse of the `organisations` block."""

import json

from upmovies.link.cluster import (
    _STALE_EVENT_TYPES,
    _VALID_TYPES,
    CLUSTER_PROMPT_VERSION,
    parse_cluster_mentions,
    parse_cluster_organisations,
)
from upmovies.news.catalog_events import COLLECTION_EVENT_TYPES, COMPANY_EVENT_TYPES
from upmovies.news.models import ORGANISATION_KINDS

ORGANISATION_EVENT_TYPES = (*COMPANY_EVENT_TYPES, *COLLECTION_EVENT_TYPES)


def reply(*organisations, **extra) -> str:
    return json.dumps({"events": [], "mentions": [], "organisations": list(organisations), **extra})


def org(n: object = 1, **overrides) -> dict:
    row: dict = {
        "n": n,
        "name_as_written": "Blumhouse",
        "kind": "company",
        "title_mentioned": None,
        "event_type": "company_attached",
        "evidence_span": "Blumhouse is boarding the thriller",
    }
    row.update(overrides)
    return row


# --- the vocabulary ------------------------------------------------------------------------


def test_the_four_organisation_beats_are_in_the_story_vocabulary():
    """Without these a studio boarding a film cards as `announced` or `other`, and `other` is
    hidden on every surface (NEU-1446's constraint 1)."""
    assert set(ORGANISATION_EVENT_TYPES) <= _VALID_TYPES


def test_the_two_attachment_beats_are_stale_stage():
    """A studio boarding a film that has already released is re-circulated old news, exactly
    as a casting is."""
    assert {"company_attached", "collection_attached"} <= _STALE_EVENT_TYPES


def test_the_two_detachment_beats_are_not_stale_stage():
    """A studio leaving a released film is news about a film that has released."""
    assert not {"company_removed", "collection_removed"} & _STALE_EVENT_TYPES


def test_an_organisation_mention_uses_the_event_vocabulary():
    """The same values as an event's `type`, exactly as a person mention does."""
    (parsed,) = parse_cluster_organisations(
        reply(org(event_type="collection_removed")), n_stories=1
    )
    assert parsed.event_type == "collection_removed"


def test_an_invented_event_type_is_dropped_rather_than_stored():
    (parsed,) = parse_cluster_organisations(reply(org(event_type="studio_boarded")), n_stories=1)
    assert parsed.event_type is None


def test_the_prompt_version_was_bumped_for_the_new_contract():
    """A `story_person` row stamped 2 sits beside stories whose studios were never
    extracted — a fact about the prompt, not about the article."""
    assert CLUSTER_PROMPT_VERSION == "3"


# --- the parse -----------------------------------------------------------------------------


def test_reads_the_tuple_the_model_reported():
    (parsed,) = parse_cluster_organisations(reply(org()), n_stories=1)
    assert parsed.story_index == 1
    assert parsed.name_as_written == "Blumhouse"
    assert parsed.kind == "company"
    assert parsed.evidence_span == "Blumhouse is boarding the thriller"


def test_a_reply_with_no_organisations_block_yields_none():
    """The ordinary case — a story naming no studio — and not distinguishable from one."""
    assert (
        parse_cluster_organisations(json.dumps({"events": [], "mentions": []}), n_stories=1) == []
    )


def test_unparseable_json_yields_none_rather_than_raising():
    assert parse_cluster_organisations("not json at all", n_stories=1) == []


def test_a_null_organisations_key_yields_none():
    assert parse_cluster_organisations(reply(organisations=None), n_stories=1) == []


def test_both_kinds_are_accepted():
    parsed = parse_cluster_organisations(
        reply(
            org(name_as_written="Blumhouse"), org(name_as_written="John Wick", kind="collection")
        ),
        n_stories=1,
    )
    assert [p.kind for p in parsed] == list(ORGANISATION_KINDS)


def test_an_unknown_kind_is_dropped():
    """`kind` decides which TMDB endpoint the name is searched against, so a word the model
    invented is a mention nothing could ever resolve."""
    assert parse_cluster_organisations(reply(org(kind="broadcaster")), n_stories=1) == []


def test_a_missing_kind_is_dropped():
    assert parse_cluster_organisations(reply(org(kind=None)), n_stories=1) == []


def test_a_story_index_outside_the_batch_is_dropped():
    assert parse_cluster_organisations(reply(org(n=4)), n_stories=2) == []


def test_a_non_integer_story_index_is_dropped():
    assert parse_cluster_organisations(reply(org(n="1")), n_stories=2) == []


def test_a_nameless_entry_is_dropped():
    assert parse_cluster_organisations(reply(org(name_as_written="   ")), n_stories=1) == []


def test_an_id_where_a_name_belongs_is_dropped():
    """INV-5: the model never emits ids, and a number here is not a name to resolve."""
    assert parse_cluster_organisations(reply(org(name_as_written=12345)), n_stories=1) == []


def test_the_same_organisation_named_twice_in_one_story_is_one_row():
    parsed = parse_cluster_organisations(
        reply(org(name_as_written="Blumhouse"), org(name_as_written="blumhouse  ")), n_stories=1
    )
    assert len(parsed) == 1


def test_one_name_under_two_kinds_is_two_rows():
    """A studio and the franchise it launched are two questions with two answers."""
    parsed = parse_cluster_organisations(
        reply(
            org(name_as_written="Blumhouse"), org(name_as_written="Blumhouse", kind="collection")
        ),
        n_stories=1,
    )
    assert [p.kind for p in parsed] == ["company", "collection"]


def test_two_stories_naming_one_organisation_stay_two_rows():
    """Each story is its own piece of evidence."""
    parsed = parse_cluster_organisations(reply(org(n=1), org(n=2)), n_stories=2)
    assert [p.story_index for p in parsed] == [1, 2]


def test_a_long_evidence_span_is_bounded():
    (parsed,) = parse_cluster_organisations(reply(org(evidence_span="x" * 900)), n_stories=1)
    assert parsed.evidence_span is not None
    assert len(parsed.evidence_span) == 500


def test_the_person_block_is_unaffected_by_the_organisation_one():
    """Two independent lists in one reply — a story names people and studios both."""
    raw = json.dumps(
        {
            "events": [],
            "mentions": [{"n": 1, "name_as_written": "Chris Evans", "evidence_span": "q"}],
            "organisations": [org()],
        }
    )
    assert [m.name_as_written for m in parse_cluster_mentions(raw, n_stories=1)] == ["Chris Evans"]
    assert [o.name_as_written for o in parse_cluster_organisations(raw, n_stories=1)] == [
        "Blumhouse"
    ]
