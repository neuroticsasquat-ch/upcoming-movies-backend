from upmovies.app.tokens import new_csrf_token, new_session_id


def test_new_session_id_is_url_safe_and_unique():
    a = new_session_id()
    b = new_session_id()
    assert a != b
    assert len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_new_csrf_token_is_url_safe():
    a = new_csrf_token()
    b = new_csrf_token()
    assert a != b
    assert len(a) >= 32
