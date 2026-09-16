"""The pieces of NEU-1340 that need no database: the link the reset mail carries, and the
copy it renders around it."""

from upmovies.app.services.reset_service import RESET, reset_url
from upmovies.config import get_settings
from upmovies.mail import render


def test_reset_url_points_at_the_public_site_and_carries_the_token():
    settings = get_settings().model_copy(update={"public_base_url": "https://backlotter.com/"})
    # Same trailing-slash trap as `verify_url`: `//reset` is a 404 on the frontend router.
    assert reset_url("tok abc", settings) == "https://backlotter.com/reset?token=tok+abc"


def test_the_reset_template_renders_both_parts_around_the_link():
    envelope = render(
        RESET,
        {
            "display_name": "Ada",
            "product_name": "Backlotter",
            "reset_url": "https://backlotter.com/reset?token=abc",
            "expires_in_hours": 1,
        },
        sender="no-reply@backlotter.com",
        to="ada@example.com",
    )
    assert envelope.subject == "Reset your password"
    assert "https://backlotter.com/reset?token=abc" in envelope.text
    assert "https://backlotter.com/reset?token=abc" in envelope.html
    assert "Ada" in envelope.text
    # The TTL is a settings value that lands in prose, so the copy has to read correctly at
    # the default of 1 as well as at any other number.
    assert "1 hour and" in " ".join(envelope.text.split())
    assert "hours" not in envelope.text


def test_the_reset_copy_pluralises_the_window():
    envelope = render(
        RESET,
        {
            "display_name": "Ada",
            "product_name": "Backlotter",
            "reset_url": "https://backlotter.com/reset?token=abc",
            "expires_in_hours": 6,
        },
        sender="no-reply@backlotter.com",
        to="ada@example.com",
    )
    assert "6 hours and" in " ".join(envelope.text.split())
