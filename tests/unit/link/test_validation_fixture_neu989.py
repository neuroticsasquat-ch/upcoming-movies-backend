from pathlib import Path

from upmovies.link.validation import load_validation_set

_FIXTURE = Path(__file__).parents[2] / "fixtures" / "link" / "validation_set.json"

_STAR_WARS_STARFIGHTER = 1417668
_SPACEBALLS_THE_NEW_ONE = 1306322

# NEU-989: three synthetic not-news rows pointed at an arbitrary tracked film rather than
# the film they are actually about. They are rejected either way (is_production_news is
# False), so the end-to-end score never moved — but the fixture is also the retrieval
# oracle, and a wrong film id makes every recall measurement wrong.
_REPAIRED_FILM_BY_URL = {
    "https://example.test/neu367/starfighter-amy-adams-teases-excitement": _STAR_WARS_STARFIGHTER,
    "https://example.test/neu367/starfighter-matt-smith-opens-up-role": _STAR_WARS_STARFIGHTER,
    "https://example.test/neu367/lewis-pullman-working-with-dad": _SPACEBALLS_THE_NEW_ONE,
}


def test_mislabeled_rows_point_at_the_film_they_are_about():
    """NEU-989: each repaired row names the film its headline is actually about."""
    by_url = {it.url: it for it in load_validation_set(_FIXTURE).items}

    for url, expected_tmdb_id in _REPAIRED_FILM_BY_URL.items():
        it = by_url.get(url)
        assert it is not None, f"{url} must be in the gold set"
        assert it.expected_film_tmdb_id == expected_tmdb_id, (
            f"{url} is labeled {it.expected_film_tmdb_id}, expected {expected_tmdb_id}"
        )
        # Still an about/not-production-news row: the repair changes the film, not the verdict.
        assert it.relation == "about"
        assert it.is_production_news is False
        assert it.exclusion_category == "interview-quote"


def test_every_starfighter_story_is_labeled_starfighter():
    """Keyed by headline rather than url, so a *newly added* Starfighter row that reused
    another film's id is caught too — that is the defect class NEU-989 repaired."""
    items = load_validation_set(_FIXTURE).items
    rows = [it for it in items if "starfighter" in it.title.lower() and it.relation == "about"]

    assert rows, "the Starfighter stories must be in the gold set"
    assert all(it.expected_film_tmdb_id == _STAR_WARS_STARFIGHTER for it in rows)


def test_every_spaceballs_story_is_labeled_spaceballs():
    """Same guard for the Spaceballs sequel row, which was labeled Spider-Man.

    Stated as "at least one, all naming the same film" rather than "exactly one". The count
    was never the property under test — it was a fact about a 302-row corpus, and enlarging
    that corpus to 1,175 rows added a second Spaceballs row (NEU-1012). Pinning the count
    would have made a *bigger* fixture fail a test about label correctness."""
    items = load_validation_set(_FIXTURE).items
    rows = [it for it in items if "spaceballs" in it.title.lower() and it.relation == "about"]

    assert rows, "the Spaceballs story must be in the gold set"
    assert all(it.expected_film_tmdb_id == _SPACEBALLS_THE_NEW_ONE for it in rows)
