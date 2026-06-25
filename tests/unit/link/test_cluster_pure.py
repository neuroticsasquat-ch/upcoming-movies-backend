import json

from upmovies.link.cluster import _INSTRUCTIONS, assemble_cluster_payload, parse_cluster_groups


def test_assemble_cluster_payload_shape():
    system, messages = assemble_cluster_payload(
        film_title="The Odyssey",
        film_year=2026,
        existing_payload=[],
        new_payload=[{"n": 1, "title": "T", "summary": "S"}],
    )
    assert system == [{"type": "text", "text": _INSTRUCTIONS}]
    assert len(messages) == 1 and messages[0]["role"] == "user"
    user = json.loads(messages[0]["content"])
    assert user["film"] == {"title": "The Odyssey", "year": 2026}
    assert user["existing_events"] == []
    assert user["new_stories"] == [{"n": 1, "title": "T", "summary": "S"}]


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
