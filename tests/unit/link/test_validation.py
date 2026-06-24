import json
from pathlib import Path

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


def test_about_can_carry_production_news_axis():
    item = ValidationItem.model_validate(
        _item(is_production_news=False, exclusion_category="reaction", event_type=None)
    )
    assert item.is_production_news is False
    assert item.exclusion_category == "reaction"


def test_about_defaults_news_axis_to_none():
    item = ValidationItem.model_validate(_item())
    assert item.is_production_news is None
    assert item.exclusion_category is None


def test_news_axis_only_on_about_items():
    with pytest.raises(ValidationError):
        ValidationItem.model_validate(
            _item(
                relation="none",
                expected_film_tmdb_id=None,
                event_type=None,
                is_production_news=False,
            )
        )


def test_exclusion_category_requires_is_production_news_false():
    with pytest.raises(ValidationError):
        ValidationItem.model_validate(
            _item(exclusion_category="reaction")
        )  # is_production_news None


_FIXTURE = Path(__file__).parents[2] / "fixtures" / "link" / "validation_set.json"
_EXCLUSION_CATEGORIES = {"reaction", "roundup", "streaming-move", "interview-quote", "downstream"}


def test_fixture_has_curated_not_news_rows():
    items = load_validation_set(_FIXTURE)
    excluded = [it for it in items if it.relation == "about" and it.is_production_news is False]
    assert len(excluded) >= 6
    assert _EXCLUSION_CATEGORIES <= {it.exclusion_category for it in excluded}


def test_curated_excluded_rows_score_clean_when_dropped():
    from upmovies.link.metrics import compute_news_value_metrics

    items = load_validation_set(_FIXTURE)
    excluded = [it for it in items if it.relation == "about" and it.is_production_news is False]
    rows = [(False, it.is_production_news, it.exclusion_category) for it in excluded]
    m = compute_news_value_metrics(rows)
    assert m.true_negatives == len(excluded)
    assert m.false_positives == 0 and m.leaks_by_category == {}


def test_fixture_has_neu367_interview_reaction_rows():
    items = load_validation_set(_FIXTURE)
    by_url = {it.url: it for it in items}
    neu367_urls = [
        "https://example.test/neu367/starfighter-amy-adams-teases-excitement",
        "https://example.test/neu367/starfighter-matt-smith-opens-up-role",
        "https://example.test/neu367/anya-taylor-joy-reacts-to-casting",
        "https://example.test/neu367/lewis-pullman-working-with-dad",
    ]
    for url in neu367_urls:
        assert url in by_url, f"missing curated row {url}"
        it = by_url[url]
        assert it.relation == "about"
        assert it.is_production_news is False
        assert it.exclusion_category in {"interview-quote", "reaction"}
