import json
from datetime import date

from upmovies.link.cluster import _INSTRUCTIONS, assemble_cluster_payload, parse_cluster_groups


def test_assemble_cluster_payload_shape():
    system, messages = assemble_cluster_payload(
        film_title="The Odyssey",
        film_year=2026,
        film_release_date=date(2026, 7, 17),
        existing_payload=[],
        new_payload=[{"n": 1, "title": "T", "summary": "S"}],
        run_date=date(2026, 6, 25),
    )
    assert system == [{"type": "text", "text": _INSTRUCTIONS}]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    user = json.loads(messages[0]["content"])
    assert user["as_of_date"] == "2026-06-25"
    assert user["film"] == {"title": "The Odyssey", "year": 2026, "release_date": "2026-07-17"}
    assert user["existing_events"] == []
    assert user["new_stories"] == [{"n": 1, "title": "T", "summary": "S"}]


def test_assemble_cluster_payload_release_date_null_when_absent():
    _, messages = assemble_cluster_payload(
        film_title="Untitled",
        film_year=None,
        film_release_date=None,
        existing_payload=[],
        new_payload=[],
        run_date=date(2026, 6, 25),
    )
    user = json.loads(messages[0]["content"])
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
