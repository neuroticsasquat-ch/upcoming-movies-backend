import json

import pytest
from pydantic import ValidationError

from upmovies.link.validation import ValidationItem, load_validation_set


def _item(**overrides) -> dict:
    base = {
        "url": "https://e/1",
        "source": "Deadline",
        "title": "Runner gets a trailer",
        "summary": "First look at the sci-fi thriller.",
        "relation": "about",
        "expected_film_tmdb_id": 123,
        "event_type": "trailer",
    }
    base.update(overrides)
    return base


def test_loads_valid_items(tmp_path):
    path = tmp_path / "set.json"
    path.write_text(
        json.dumps(
            [
                _item(),
                _item(
                    url="https://e/2", relation="none", expected_film_tmdb_id=None, event_type=None
                ),
            ]
        )
    )
    items = load_validation_set(path)
    assert len(items) == 2
    assert items[0].expected_film_tmdb_id == 123
    assert items[1].relation == "none"


def test_about_requires_a_film_id(tmp_path):
    path = tmp_path / "set.json"
    path.write_text(json.dumps([_item(expected_film_tmdb_id=None)]))  # about but no film
    with pytest.raises(ValidationError):
        load_validation_set(path)


def test_rejects_unknown_relation():
    with pytest.raises(ValidationError):
        ValidationItem.model_validate(
            {"url": "u", "source": "s", "title": "t", "summary": "", "relation": "maybe"}
        )
