import json
from datetime import date

from upmovies.link.cluster import (
    _INSTRUCTIONS,
    assemble_cluster_payload,
    parse_cluster_groups,
    parse_cluster_mentions,
)


def test_assemble_cluster_payload_shape():
    prompt = assemble_cluster_payload(
        film_title="The Odyssey",
        film_year=2026,
        film_release_date=date(2026, 7, 17),
        existing_payload=[],
        new_payload=[{"n": 1, "title": "T", "summary": "S"}],
        run_date=date(2026, 6, 25),
    )
    assert prompt.stable_prefix == _INSTRUCTIONS
    assert prompt.prefill is None
    user = json.loads(prompt.user)
    assert user["as_of_date"] == "2026-06-25"
    assert user["film"] == {"title": "The Odyssey", "year": 2026, "release_date": "2026-07-17"}
    assert user["existing_events"] == []
    assert user["new_stories"] == [{"n": 1, "title": "T", "summary": "S"}]


def test_assemble_cluster_payload_release_date_null_when_absent():
    prompt = assemble_cluster_payload(
        film_title="Untitled",
        film_year=None,
        film_release_date=None,
        existing_payload=[],
        new_payload=[],
        run_date=date(2026, 6, 25),
    )
    user = json.loads(prompt.user)
    assert user["film"] == {"title": "Untitled", "year": None, "release_date": None}


def test_instructions_flag_already_scheduled_restatement():
    """NEU-451: a release_date event requires a NEW/CHANGED date; a story restating the
    film's already-known release_date is dropped as off_topic, not recorded."""
    text = _INSTRUCTIONS.lower()
    assert "new or changed" in text
    assert "restat" in text  # matches "restate"/"restating"/"restatement"
    assert "off_topic" in text


def test_parse_cluster_groups_new_event():
    raw = (
        '{"events": [{"existing": null, "type": "casting", "confidence": "confirmed",'
        ' "stories": [1, 2]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=2)
    assert groups is not None and len(groups) == 1
    g = groups[0]
    assert g.existing is None
    assert g.event_type == "casting"
    assert g.confidence == "confirmed"
    assert g.story_indices == [1, 2]


def test_parse_cluster_groups_attach_to_existing():
    raw = '{"events": [{"existing": 3, "type": null, "confidence": null, "stories": [1]}]}'
    groups = parse_cluster_groups(raw, n_stories=2)
    assert groups is not None
    assert groups[0].existing == 3 and groups[0].story_indices == [1]


def test_parse_cluster_groups_drops_out_of_range_and_dupes():
    raw = (
        '{"events": [{"existing": null, "type": "trailer", "confidence": "confirmed",'
        ' "stories": [1, 1, 5, "x"]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=2)
    assert groups is not None
    assert groups[0].story_indices == [1]


def test_parse_cluster_groups_unparseable_returns_none():
    assert parse_cluster_groups('{"events": [trunc', n_stories=3) is None


def test_instructions_state_primary_beat_precedence():
    """NEU-445: the cluster prompt must tell the model to classify by the dominant
    (headline) beat and ignore incidental mentions, so a trailer/first-look that names
    cast is not mislabeled 'casting'."""
    text = _INSTRUCTIONS.lower()
    assert "dominant" in text
    assert "incidental" in text
    # The worked direction the rule exists to fix is spelled out.
    assert "trailer" in text and "casting" in text


def test_parse_cluster_groups_extracts_region_uppercased():
    raw = (
        '{"events": [{"existing": null, "type": "release_date", "confidence": "confirmed",'
        ' "region": "in", "stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None
    assert groups[0].region == "IN"


def test_parse_cluster_groups_region_missing_or_invalid_is_none():
    raw = (
        '{"events": ['
        '{"existing": null, "type": "casting", "confidence": "confirmed", "stories": [1]},'
        '{"existing": null, "type": "release_date", "confidence": "confirmed",'
        ' "region": "India", "stories": [2]}'
        "]}"
    )
    groups = parse_cluster_groups(raw, n_stories=2)
    assert groups is not None
    assert groups[0].region is None  # key absent
    assert groups[1].region is None  # full country name rejected (not alpha-2)


def test_instructions_describe_region_alpha2():
    text = _INSTRUCTIONS.lower()
    assert "region" in text
    assert "alpha-2" in text or "iso 3166" in text


def test_first_look_is_a_valid_type():
    from upmovies.link.cluster import _VALID_TYPES

    assert "first_look" in _VALID_TYPES


def test_parse_cluster_groups_first_look_type():
    raw = (
        '{"events": [{"existing": null, "type": "first_look", "confidence": "confirmed",'
        ' "stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None
    assert groups[0].event_type == "first_look"


def test_instructions_distinguish_released_video_from_first_look():
    """NEU-447: a trailer is a RELEASED video the public can watch; footage merely
    screened/described, concept art, character designs, and first-look photos are
    'first_look', not 'trailer'."""
    text = _INSTRUCTIONS.lower()
    assert "first_look" in text
    assert "released" in text  # the trailer boundary hinges on a publicly released video


def test_instructions_state_casting_consolidation_rule():
    """NEU-483 #7, #9: a new casting story naming a performer already in an existing
    casting event must attach there instead of opening a duplicate event."""
    text = _INSTRUCTIONS.lower()
    assert "already appears in an existing casting event" in text
    assert "continuation of that casting beat" in text


def test_instructions_require_named_performer_for_casting():
    """NEU-483 #5, #8: no performer named (or casting only 'forthcoming') is not a
    casting beat."""
    text = _INSTRUCTIONS.lower()
    assert "requires an actual performer's name" in text
    assert "forthcoming" in text


def test_instructions_exclude_tie_in_products_from_release_date():
    """NEU-483 #2: a companion video game/merchandise launch is never the film's own
    release_date beat, even when the story says "release"."""
    text = _INSTRUCTIONS.lower()
    assert "companion product" in text
    assert "tie-in" in text


def test_instructions_flag_fake_date_move_matching_known_release_date():
    """NEU-483 #3, #4: a story framing a date as "moved" when it matches
    film.release_date is a restatement, not a new beat, regardless of headline framing."""
    text = _INSTRUCTIONS.lower()
    assert "regardless of how the headline frames it" in text
    assert "already-known release_date" in text


def test_parse_reads_claimed_date_for_release_date_events():
    from datetime import date

    from upmovies.link.cluster import parse_cluster_groups

    raw = (
        '{"events": [{"existing": null, "type": "release_date", "confidence": "confirmed", '
        '"region": "US", "claimed_date": "2027-06-30", "stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None and groups[0].claimed_date == date(2027, 6, 30)


def test_parse_bad_claimed_date_is_none():
    from upmovies.link.cluster import parse_cluster_groups

    raw = (
        '{"events": [{"existing": null, "type": "release_date", "confidence": "confirmed", '
        '"claimed_date": "sometime 2027", "stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None and groups[0].claimed_date is None


def test_normalize_name_folds_case_and_whitespace():
    # Shared with the credit phase (`news.subject_key`): both sides of the "have we already
    # carded this person" question must fold a name the same way (ADR-0014).
    from upmovies.news.subject_key import normalize_name

    assert normalize_name("Alain  Chabat") == "alain chabat"
    assert normalize_name("  Monica Barbaro ") == "monica barbaro"
    assert normalize_name("JOHN DOE") == "john doe"
    assert normalize_name("") == ""


def test_parse_reads_cast_for_casting_events():
    from upmovies.link.cluster import parse_cluster_groups

    raw = (
        '{"events": [{"existing": null, "type": "casting", "confidence": "confirmed", '
        '"cast": ["Monica Barbaro", "John Doe"], "stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None and len(groups) == 1
    assert groups[0].cast == ["Monica Barbaro", "John Doe"]


def test_parse_cast_absent_is_none():
    from upmovies.link.cluster import parse_cluster_groups

    raw = (
        '{"events": [{"existing": null, "type": "trailer", "confidence": "confirmed", '
        '"stories": [1]}]}'
    )
    groups = parse_cluster_groups(raw, n_stories=1)
    assert groups is not None and groups[0].cast is None


def test_instructions_ask_for_mention_tuples():
    """NEU-1360 (D-20): the extraction half of the contract — names as written, never ids."""
    text = _INSTRUCTIONS.lower()
    assert "mentions" in text
    assert "name_as_written" in text
    assert "evidence_span" in text
    assert "title_mentioned" in text
    assert "never emit an id" in text


def test_parse_cluster_mentions_extracts_the_tuple():
    raw = json.dumps(
        {
            "events": [],
            "mentions": [
                {
                    "n": 2,
                    "name_as_written": "Chris Evans",
                    "role": "Batman",
                    "department": "Acting",
                    "title_mentioned": "The Gray Man",
                    "event_type": "casting",
                    "evidence_span": "Chris Evans is set to star.",
                }
            ],
        }
    )
    mentions = parse_cluster_mentions(raw, n_stories=2)
    assert len(mentions) == 1
    m = mentions[0]
    assert m.story_index == 2
    assert m.name_as_written == "Chris Evans"
    assert m.role == "Batman"
    assert m.department == "Acting"
    assert m.title_mentioned == "The Gray Man"
    assert m.event_type == "casting"
    assert m.evidence_span == "Chris Evans is set to star."


def test_parse_cluster_mentions_optional_fields_default_to_none():
    raw = '{"mentions": [{"n": 1, "name_as_written": "Ana de Armas"}]}'
    mentions = parse_cluster_mentions(raw, n_stories=1)
    assert len(mentions) == 1
    m = mentions[0]
    assert (m.role, m.department, m.title_mentioned, m.event_type, m.evidence_span) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_parse_cluster_mentions_drops_out_of_range_and_nameless():
    raw = json.dumps(
        {
            "mentions": [
                {"n": 3, "name_as_written": "Out Of Range"},
                {"n": 0, "name_as_written": "Also Out"},
                {"n": "1", "name_as_written": "Index Not An Int"},
                {"n": 1, "name_as_written": "   "},
                {"n": 1},
                "not an object",
                {"n": 1, "name_as_written": "Kept Name"},
            ]
        }
    )
    mentions = parse_cluster_mentions(raw, n_stories=2)
    assert [m.name_as_written for m in mentions] == ["Kept Name"]


def test_parse_cluster_mentions_ignores_non_string_free_text():
    """A number or object where a quote belongs is the model answering a different question:
    dropped, never coerced into something that would read like evidence."""
    raw = json.dumps(
        {
            "mentions": [
                {
                    "n": 1,
                    "name_as_written": "Ryan Gosling",
                    "role": 7,
                    "evidence_span": {"quote": "nope"},
                }
            ]
        }
    )
    mentions = parse_cluster_mentions(raw, n_stories=1)
    assert (mentions[0].role, mentions[0].evidence_span) == (None, None)


def test_parse_cluster_mentions_dedupes_within_a_story_only():
    raw = json.dumps(
        {
            "mentions": [
                {"n": 1, "name_as_written": "Chris Evans", "role": "first wins"},
                {"n": 1, "name_as_written": "chris  evans", "role": "dropped"},
                {"n": 2, "name_as_written": "Chris Evans", "role": "other story"},
            ]
        }
    )
    mentions = parse_cluster_mentions(raw, n_stories=2)
    assert [(m.story_index, m.role) for m in mentions] == [(1, "first wins"), (2, "other story")]


def test_parse_cluster_mentions_truncates_evidence_span():
    raw = json.dumps({"mentions": [{"n": 1, "name_as_written": "A", "evidence_span": "x" * 900}]})
    assert parse_cluster_mentions(raw, n_stories=1)[0].evidence_span == "x" * 500


def test_parse_cluster_mentions_absent_or_unparseable_is_empty_not_none():
    assert parse_cluster_mentions('{"events": []}', n_stories=1) == []
    assert parse_cluster_mentions('{"mentions": null}', n_stories=1) == []
    assert parse_cluster_mentions('{"mentions": [trunc', n_stories=1) == []
    assert parse_cluster_mentions("[]", n_stories=1) == []


def test_parse_cluster_mentions_drops_an_invented_event_type():
    """The beat is validated against the same vocabulary a group's "type" is: a word the model
    invented would never match anything downstream, so it lands as None rather than as noise."""
    raw = json.dumps(
        {
            "mentions": [
                {"n": 1, "name_as_written": "A", "event_type": "casting"},
                {"n": 2, "name_as_written": "B", "event_type": "gossip"},
            ]
        }
    )
    mentions = parse_cluster_mentions(raw, n_stories=2)
    assert [m.event_type for m in mentions] == ["casting", None]
