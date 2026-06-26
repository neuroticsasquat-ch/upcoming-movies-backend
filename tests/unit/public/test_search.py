from upmovies.public.service import _escape_like


def test_escape_like_escapes_percent():
    assert _escape_like("100%") == "100\\%"


def test_escape_like_escapes_underscore():
    assert _escape_like("film_title") == "film\\_title"


def test_escape_like_escapes_backslash():
    assert _escape_like("back\\slash") == "back\\\\slash"


def test_escape_like_plain_term_unchanged():
    assert _escape_like("odyssey") == "odyssey"
