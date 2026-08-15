from upmovies.catalog.ref import film_ref, parse_film_ref


def test_ref_is_id_then_title_slug():
    assert film_ref(1061474, "Shang-Chi 2") == "1061474-shang-chi-2"


def test_ref_carries_no_release_year():
    """Unlike `base_slug`. Release years move constantly for upcoming films, and each move would
    churn the canonical URL and mint another redirect."""
    assert film_ref(603692, "John Wick: Chapter 4") == "603692-john-wick-chapter-4"


def test_ref_follows_the_current_title():
    assert film_ref(1, "Untitled Sequel") == "1-untitled-sequel"
    assert film_ref(1, "The Real Title") == "1-the-real-title"


def test_ref_falls_back_to_the_bare_id_for_an_unslugifiable_title():
    assert film_ref(42, "???") == "42"
    assert film_ref(42, "") == "42"


def test_parse_reads_the_leading_id():
    assert parse_film_ref("1061474-shang-chi-2") == 1061474
    assert parse_film_ref("1061474") == 1061474


def test_parse_ignores_the_decorative_half_entirely():
    assert parse_film_ref("1061474-anything-at-all") == 1061474


def test_parse_returns_none_for_a_legacy_slug():
    assert parse_film_ref("the-odyssey-2026") is None
    assert parse_film_ref("film-12345") is None


def test_parse_needs_a_whole_leading_id_not_a_numeric_prefix():
    """`2001abc` is not id 2001 with decoration — a ref's id is followed by a hyphen or nothing."""
    assert parse_film_ref("2001abc-space") is None


def test_a_numeric_title_slug_is_ambiguous_by_construction():
    """The film "1917" is slugged `1917-2019`, which parses as id 1917 — a different, real film.
    Nothing in the string distinguishes them, so the resolver has to try both and prefer the
    exact slug match."""
    assert parse_film_ref("1917-2019") == 1917
