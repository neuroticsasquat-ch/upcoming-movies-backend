import json
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from upmovies.link.validation import (
    ValidationItem,
    films_ingested_after,
    load_validation_set,
)


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
    items = load_validation_set(path).items
    assert len(items) == 2
    assert items[0].expected_film_tmdb_id == 123
    assert items[1].relation == "none"


def test_envelope_carries_the_as_of_date(tmp_path):
    """The date the set was labeled travels with the data, not with the caller — a harness
    that has to be told the date separately can be told the wrong one."""
    path = tmp_path / "set.json"
    path.write_text(json.dumps({"as_of_date": "2026-07-01", "items": [_item()]}))

    loaded = load_validation_set(path)

    assert loaded.as_of_date == date(2026, 7, 1)
    assert len(loaded.items) == 1
    assert loaded.items[0].expected_film_tmdb_id == 123


def test_bare_list_still_loads_with_no_as_of_date(tmp_path):
    """The pre-envelope shape stays readable; an unpinned set simply has no date."""
    path = tmp_path / "set.json"
    path.write_text(json.dumps([_item()]))

    loaded = load_validation_set(path)

    assert loaded.as_of_date is None
    assert [it.url for it in loaded.items] == ["https://e/1"]


def test_the_real_fixture_declares_an_as_of_date():
    """Guard the pin itself. Without a date the harness silently falls back to wall clock,
    which is the decay this fixture was repaired to escape."""
    assert load_validation_set(_FIXTURE).as_of_date is not None


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
# The exclusion categories the fixture actually carries. `streaming-move` is deliberately
# absent, and its absence is a scope fact rather than a labeling gap: a
# streaming/catalogue-move story is about a film you can already watch, and the fixture is
# pinned to the *active* catalog, which is upcoming films only. The one such row the 302-row
# set held named Enola Holmes 3, and it demoted to `none` when the pin moved past that film's
# release (NEU-1012); the 4,000-row draft that replaced it produced no candidate for the
# category at all. `other` is in the set because the enlarged corpus supplies it in volume.
_EXCLUSION_CATEGORIES = {"reaction", "roundup", "interview-quote", "downstream", "other"}


def test_fixture_has_curated_not_news_rows():
    items = load_validation_set(_FIXTURE).items
    excluded = [it for it in items if it.relation == "about" and it.is_production_news is False]
    assert len(excluded) >= 6
    assert _EXCLUSION_CATEGORIES <= {it.exclusion_category for it in excluded}


def test_curated_excluded_rows_score_clean_when_dropped():
    from upmovies.link.metrics import compute_news_value_metrics

    items = load_validation_set(_FIXTURE).items
    excluded = [it for it in items if it.relation == "about" and it.is_production_news is False]
    rows = [(False, it.is_production_news, it.exclusion_category) for it in excluded]
    m = compute_news_value_metrics(rows)
    assert m.true_negatives == len(excluded)
    assert m.false_positives == 0 and m.leaks_by_category == {}


def test_fixture_has_neu367_interview_reaction_rows():
    items = load_validation_set(_FIXTURE).items
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


def test_fixture_has_neu443_aspirational_casting_row():
    items = load_validation_set(_FIXTURE).items
    by_url = {it.url: it for it in items}
    url = "https://www.joblo.com/madeline-petsch-poison-ivy-the-batman-2/"
    assert url in by_url, f"missing curated row {url}"
    it = by_url[url]
    assert it.relation == "about"
    assert it.expected_film_tmdb_id == 806704
    assert it.is_production_news is False
    assert it.exclusion_category == "interview-quote"


def test_films_ingested_after_the_pin_are_outside_the_label_space():
    """A `none` row means "not about any film tracked *at labeling time*". A prediction
    naming a film the catalog only learned about later is something the labels have no
    opinion on, so it must not score as a false positive (NEU-1011)."""
    ingested = {1: date(2026, 6, 1), 2: date(2026, 7, 20), 3: date(2026, 7, 1)}

    after = films_ingested_after(date(2026, 7, 1), ingested)

    assert after == frozenset({2})  # 3 was ingested on the pin date, so it counts as known


def test_films_ingested_after_is_empty_without_a_pin():
    """An unpinned fixture has no 'after' — every prediction stays scoreable."""
    assert films_ingested_after(None, {1: date(2026, 6, 1)}) == frozenset()
