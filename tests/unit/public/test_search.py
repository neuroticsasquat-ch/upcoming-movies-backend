from upmovies.public.service import _normalize_query


def test_normalize_lowercases_and_strips_separators():
    assert _normalize_query("Spider-Man") == "spiderman"
    assert _normalize_query("spider man") == "spiderman"
    assert _normalize_query("  Spider  Man!  ") == "spiderman"


def test_normalize_folds_diacritics():
    assert _normalize_query("Shōgun") == "shogun"
    assert _normalize_query("Résumé") == "resume"


def test_normalize_keeps_non_latin_letters():
    # Non-Latin scripts must survive the fold (only separators/punctuation are dropped).
    assert _normalize_query("기생충") == "기생충"


def test_normalize_punctuation_only_is_empty():
    assert _normalize_query("—%_ .") == ""
