"""`is_entitled` — the rule itself, over a loaded row and with no database in sight."""

from datetime import UTC, datetime, timedelta

from upmovies.app.entitlements import is_entitled
from upmovies.app.models import User


def _user() -> User:
    return User(email="a@example.com", password_hash="x", display_name="A")


def test_a_new_account_is_not_entitled():
    # NULL is the default for every signup and there is no trial grant (D-37).
    assert _user().entitled_until is None
    assert is_entitled(_user()) is False


def test_a_lapsed_grant_is_not_entitled():
    user = _user()
    user.entitled_until = datetime.now(UTC) - timedelta(seconds=1)
    assert is_entitled(user) is False


def test_a_live_grant_is_entitled():
    user = _user()
    user.entitled_until = datetime.now(UTC) + timedelta(days=365)
    assert is_entitled(user) is True
