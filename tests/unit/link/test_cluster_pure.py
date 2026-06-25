import json

from upmovies.link.cluster import _INSTRUCTIONS, assemble_cluster_payload


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
