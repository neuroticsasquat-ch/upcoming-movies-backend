from upmovies.app.passwords import hash_password, verify_password


def test_hash_password_returns_argon2_string():
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$")


def test_verify_password_correct():
    h = hash_password("hunter2")
    assert verify_password("hunter2", h) is True


def test_verify_password_wrong():
    h = hash_password("hunter2")
    assert verify_password("wrong", h) is False


def test_verify_password_handles_invalid_hash():
    assert verify_password("hunter2", "not-a-real-hash") is False


def test_two_hashes_of_same_password_differ():
    a = hash_password("hunter2")
    b = hash_password("hunter2")
    assert a != b
