import pytest
from argon2 import PasswordHasher, extract_parameters

from upmovies.app import passwords
from upmovies.app.passwords import hash_password, verify_password


# `tests/conftest.py` swaps in a minimum-cost hasher for the suite (NEU-1393). This file is
# the one place that tests the hasher itself, so it reinstalls the hasher `passwords.py`
# actually built -- yielded by the conftest fixture -- rather than constructing a fresh
# `PasswordHasher()`: the round-trip and format assertions below are only meaningful against
# production's real object, and the parameter test below would not notice a weakened
# `_hasher` in `src/` if it were asserting on one it built itself.
@pytest.fixture(scope="module", autouse=True)
def production_password_hasher(cheap_password_hasher: PasswordHasher):
    cheap = passwords._hasher
    passwords._hasher = cheap_password_hasher
    yield
    passwords._hasher = cheap


def test_hash_password_returns_argon2_string():
    h = hash_password("hunter2")
    assert h.startswith("$argon2id$")


def test_hash_password_uses_library_default_parameters():
    # The property under test is "we use argon2-cffi's defaults", not the specific numbers,
    # so an argon2-cffi upgrade that raises them does not break this.
    params = extract_parameters(hash_password("hunter2"))
    defaults = PasswordHasher()
    assert params.time_cost == defaults.time_cost
    assert params.memory_cost == defaults.memory_cost
    assert params.parallelism == defaults.parallelism
    assert params.type == defaults.type


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
