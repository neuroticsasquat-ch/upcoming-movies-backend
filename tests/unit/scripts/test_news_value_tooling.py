from scripts.propose_validation_labels import _proposal_to_row
from scripts.validate_linking import news_value_rows
from upmovies.link.validation import ValidationItem


def _about(**kw) -> ValidationItem:
    base = dict(
        url="u", source="s", title="t", summary="", relation="about", expected_film_tmdb_id=1
    )
    base.update(kw)
    return ValidationItem.model_validate(base)


def test_news_value_rows_keeps_about_and_maps_linked():
    items_with_linked = [
        (_about(is_production_news=False, exclusion_category="reaction"), True),
        (_about(), False),
        (
            ValidationItem.model_validate(
                dict(url="u2", source="s", title="t", summary="", relation="none")
            ),
            True,
        ),  # dropped (not about)
    ]
    rows = news_value_rows(items_with_linked)
    assert rows == [(True, False, "reaction"), (False, None, None)]


def _draft_row():
    return {"url": "u", "source": "s", "title": "t", "summary": ""}


def test_proposal_to_row_about_carries_news_axis():
    p = {
        "relation": "about",
        "tmdb_id": 7,
        "event_type": "casting",
        "is_production_news": False,
        "exclusion_category": "roundup",
    }
    row = _proposal_to_row(_draft_row(), p, {7})
    assert row["relation"] == "about" and row["expected_film_tmdb_id"] == 7
    assert row["is_production_news"] is False and row["exclusion_category"] == "roundup"


def test_proposal_to_row_real_news_has_no_exclusion_category():
    p = {
        "relation": "about",
        "tmdb_id": 7,
        "event_type": "trailer",
        "is_production_news": True,
        "exclusion_category": "roundup",
    }
    row = _proposal_to_row(_draft_row(), p, {7})
    assert row["is_production_news"] is True and row["exclusion_category"] is None


def test_proposal_to_row_non_about_blanks_news_axis():
    p = {
        "relation": "none",
        "tmdb_id": None,
        "event_type": None,
        "is_production_news": False,
        "exclusion_category": "reaction",
    }
    row = _proposal_to_row(_draft_row(), p, set())
    assert row["relation"] == "none"
    assert row["is_production_news"] is None and row["exclusion_category"] is None
