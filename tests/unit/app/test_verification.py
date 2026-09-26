"""`is_verified` and the link the verification mail carries — the two pieces of NEU-1339 that
need no database."""

from datetime import UTC, datetime

from upmovies.app.models import User
from upmovies.app.services.verification_service import verify_url
from upmovies.app.tokens import new_email_token
from upmovies.app.verification import is_verified
from upmovies.config import get_settings


def test_a_user_is_unverified_until_the_column_is_stamped():
    user = User(email="a@example.com", password_hash="x", display_name="A")
    assert is_verified(user) is False
    user.email_verified_at = datetime.now(UTC)
    assert is_verified(user) is True


def test_new_email_token_is_url_safe_and_unique():
    a = new_email_token()
    b = new_email_token()
    assert a != b
    assert len(a) >= 32
    assert all(c.isalnum() or c in "-_" for c in a)


def test_verify_url_points_at_the_public_site_and_carries_the_token():
    settings = get_settings().model_copy(update={"public_base_url": "https://backlotter.com/"})
    # The trailing slash on the setting is the whole reason this is a function and not an
    # f-string at the call site: `//verify` is a 404 on the frontend router.
    assert verify_url("tok abc", settings) == "https://backlotter.com/verify?token=tok+abc"
