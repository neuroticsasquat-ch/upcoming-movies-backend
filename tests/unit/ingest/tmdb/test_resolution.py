"""Placing a Letterboxd title-and-year on a TMDB search hit (D-15). Pure — no client."""

from datetime import date

import pytest

from upmovies.ingest.tmdb.resolution import resolve
from upmovies.ingest.tmdb.schemas import TMDBMovieSummary


def hit(tmdb_id: int, title: str, year: int | None, **overrides) -> TMDBMovieSummary:
    return TMDBMovieSummary(
        id=tmdb_id,
        title=title,
        release_date=None if year is None else date(year, 6, 1),
        **overrides,
    )


def test_matches_on_title_and_year():
    resolved = resolve([hit(1, "Dune", 2021)], name="Dune", year=2021)
    assert resolved == (1, "Dune")


def test_the_fold_absorbs_punctuation_spacing_and_case():
    # `squash_fold` is the same normalization `link.retrieval` uses; here it is an equality
    # test rather than a substring one.
    hits = [hit(1, "WALL·E", 2008)]
    assert resolve(hits, name="wall e", year=2008) == (1, "WALL·E")


def test_matches_the_original_title_too():
    # A user whose Letterboxd is set to original titles exports the French name.
    hits = [hit(1, "Anatomy of a Fall", 2023, original_title="Anatomie d'une chute")]
    assert resolve(hits, name="Anatomie d'une chute", year=2023) == (1, "Anatomy of a Fall")


def test_a_year_one_out_still_matches():
    # Letterboxd dates a film by its first public screening, so a festival premiere and a TMDB
    # primary release routinely sit a year apart.
    assert resolve([hit(1, "The Zone of Interest", 2023)], name="The Zone of Interest", year=2024)


def test_a_year_two_out_does_not():
    assert resolve([hit(1, "Dune", 2021)], name="Dune", year=2024) is None


def test_an_exact_year_beats_a_year_that_is_one_out():
    # Both passes run over the whole hit list, exact first, so TMDB's ordering cannot decide it.
    hits = [hit(1, "Dune", 2020, popularity=99.0), hit(2, "Dune", 2021, popularity=1.0)]
    assert resolve(hits, name="Dune", year=2021) == (2, "Dune")


def test_popularity_breaks_a_tie_between_exact_matches():
    hits = [hit(1, "Heat", 1995, popularity=3.0), hit(2, "Heat", 1995, popularity=40.0)]
    assert resolve(hits, name="Heat", year=1995) == (2, "Heat")


def test_a_tie_on_popularity_is_broken_deterministically():
    # Same score either way; the import must not depend on TMDB's ordering for which film a
    # user ends up following.
    hits = [hit(1, "Heat", 1995), hit(2, "Heat", 1995)]
    assert resolve(hits, name="Heat", year=1995) == resolve(
        list(reversed(hits)), name="Heat", year=1995
    )


def test_a_near_miss_on_the_title_is_never_guessed():
    # A wrong match is a follow and a watchlist item for a film the user has never heard of,
    # and they cannot tell it from a bug. Unmatched is the safe answer.
    hits = [hit(1, "Dune: Part Two", 2021), hit(2, "Dunes", 2021)]
    assert resolve(hits, name="Dune", year=2021) is None


def test_a_hit_with_no_release_date_cannot_match():
    assert resolve([hit(1, "Dune", None)], name="Dune", year=2021) is None


def test_a_row_with_no_year_cannot_match():
    # Both rules are year-equality rules; dropping the requirement for the rows that lack one
    # would match exactly the rows carrying the least information on title alone.
    assert resolve([hit(1, "Dune", 2021)], name="Dune", year=None) is None


@pytest.mark.parametrize("name", ["", "   ", "!!!"])
def test_a_name_that_folds_to_nothing_cannot_match(name):
    assert resolve([hit(1, "Dune", 2021)], name=name, year=2021) is None


def test_no_hits_is_unmatched():
    assert resolve([], name="Dune", year=2021) is None
