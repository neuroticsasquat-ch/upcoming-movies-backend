"""Assembling a re-pinned, enlarged validation set out of old labels and new proposals.

Two things this has to get right, and both are about the pin. Carried-forward `about` rows
whose film has released since are no longer labelable as `about` — the active-film filter
excludes the film, so the link path cannot reach it and the row would score as a miss for the
path under test. And the `none` class has to be subsampled deterministically, or the fixture
stops being reproducible from its recipe.
"""

from scripts.assemble_validation_set import (
    demote_unlabelable,
    select_none_rows,
    summarize,
)


def _about(tmdb_id: int, **kw) -> dict:
    return {
        "url": f"https://e/{tmdb_id}",
        "source": "Deadline",
        "title": "t",
        "summary": "s",
        "relation": "about",
        "expected_film_tmdb_id": tmdb_id,
        "event_type": "trailer",
        "event_group": f"{tmdb_id}-beat",
        "is_production_news": None,
        "exclusion_category": None,
        **kw,
    }


def test_an_about_row_whose_film_is_still_labelable_is_untouched():
    row = _about(111)

    assert demote_unlabelable(row, {111}) == row


def test_an_about_row_whose_film_has_released_becomes_an_untracked_none():
    """Not a relabel of convenience: at the new pin the film genuinely is not tracked, and
    the fixture's own definition of `none` is relative to that set. The row keeps its text and
    becomes a hard negative that exercises scope filtering — which the 2026-07-01 pin could
    not, since at that date `active_film_clause` excluded nothing (spec §5.1a)."""
    row = demote_unlabelable(_about(222), {111})

    assert row["relation"] == "none"
    assert row["expected_film_tmdb_id"] is None
    assert row["untracked_film"] is True
    assert row["title"] == "t"


def test_demotion_clears_every_about_only_field():
    """The schema refuses a non-`about` row carrying them, so a partial demotion fails to
    load rather than mislabeling quietly — but failing to load a 1,000-row fixture is a bad
    way to find out."""
    row = demote_unlabelable(
        _about(222, is_production_news=False, exclusion_category="reaction"), set()
    )

    assert row["event_type"] is None
    assert row["event_group"] is None
    assert row["is_production_news"] is None
    assert row["exclusion_category"] is None


def test_a_row_that_is_already_none_is_left_alone():
    row = {"url": "https://e/x", "relation": "none", "expected_film_tmdb_id": None}

    assert demote_unlabelable(row, set()) == row


def test_none_rows_are_subsampled_to_the_target_count():
    rows = [{"url": f"https://e/{i}", "relation": "none"} for i in range(100)]

    assert len(select_none_rows(rows, target=30, seed="s")) == 30


def test_the_subsample_is_deterministic_in_the_seed():
    rows = [{"url": f"https://e/{i}", "relation": "none"} for i in range(100)]

    first = select_none_rows(rows, target=30, seed="s")
    again = select_none_rows(rows, target=30, seed="s")
    other = select_none_rows(rows, target=30, seed="different")

    assert [r["url"] for r in first] == [r["url"] for r in again]
    assert [r["url"] for r in first] != [r["url"] for r in other]


def test_a_target_above_the_supply_takes_every_row():
    rows = [{"url": f"https://e/{i}", "relation": "none"} for i in range(5)]

    assert len(select_none_rows(rows, target=99, seed="s")) == 5


def test_the_summary_separates_the_link_and_news_value_populations():
    """`about` is the link axis's true-positive ceiling — `compute_link_metrics` credits a
    true positive for any `about` row linked to its labeled film, production news or not.
    `linkable_about` is the narrower population the news-value axis scores recall against.
    Reported separately because they move independently, and because conflating them would
    understate the resolution an enlarged fixture actually buys."""
    rows = [
        _about(1),
        _about(2, is_production_news=False, exclusion_category="reaction"),
        {"relation": "mention", "expected_film_tmdb_id": None},
        {"relation": "none", "expected_film_tmdb_id": None},
    ]

    counts = summarize(rows)

    assert counts["about"] == 2
    assert counts["linkable_about"] == 1
    assert counts["mention"] == 1
    assert counts["none"] == 1
    assert counts["total"] == 4
