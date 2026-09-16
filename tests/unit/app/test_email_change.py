"""The pieces of NEU-1341 that need no database: the link the confirmation carries, and the
copy of the two mails the flow sends."""

from upmovies.app.services.email_change_service import (
    EMAIL_CHANGE,
    EMAIL_CHANGE_NOTICE,
    _one_line,
    email_change_url,
)
from upmovies.config import get_settings
from upmovies.mail import render

CONFIRM_URL = "https://backlotter.com/email-change?token=abc"


def _confirmation(**overrides):
    context = {
        "display_name": "Ada",
        "product_name": "Backlotter",
        "current_email": "ada@example.com",
        "new_email": "ada@newmail.com",
        "email_change_url": CONFIRM_URL,
        "expires_in_hours": 1,
    }
    context.update(overrides)
    return render(EMAIL_CHANGE, context, sender="no-reply@backlotter.com", to="ada@newmail.com")


def _notice(**overrides):
    context = {
        "display_name": "Ada",
        "product_name": "Backlotter",
        "current_email": "ada@example.com",
        "new_email": "ada@newmail.com",
        "expires_in_hours": 1,
    }
    context.update(overrides)
    return render(
        EMAIL_CHANGE_NOTICE, context, sender="no-reply@backlotter.com", to="ada@example.com"
    )


def test_email_change_url_points_at_the_public_site_and_carries_the_token():
    settings = get_settings().model_copy(update={"public_base_url": "https://backlotter.com/"})
    # Same trailing-slash trap as `verify_url` and `reset_url`: `//email-change` is a 404 on
    # the frontend router.
    assert (
        email_change_url("tok abc", settings) == "https://backlotter.com/email-change?token=tok+abc"
    )


def test_the_confirmation_renders_both_parts_around_the_link():
    envelope = _confirmation()
    assert envelope.subject == "Confirm your new email address"
    assert CONFIRM_URL in envelope.text
    assert CONFIRM_URL in envelope.html
    assert "Ada" in envelope.text
    # It names the address being left, so a reader who was not expecting the mail can tell
    # whose account someone is trying to move onto their inbox.
    assert "ada@example.com" in envelope.text


def test_the_confirmation_says_nothing_has_changed_yet():
    """The copy carries the flow's central promise, and the test is here so a rewrite cannot
    quietly drop it."""
    text = " ".join(_confirmation().text.split())
    assert "Nothing changes until you use it" in text


def test_the_notice_names_the_destination_and_carries_no_link():
    envelope = _notice()
    assert envelope.subject == "Someone asked to change your email address"
    assert "ada@newmail.com" in envelope.text
    assert "ada@newmail.com" in envelope.html
    # No token and no link: the notice warns, and a second actionable mail would hand whoever
    # holds the old inbox a way to complete the change it is warning about.
    assert "/email-change?" not in envelope.text
    assert "/email-change?" not in envelope.html


def test_the_notice_offers_the_remedy_that_works_when_the_password_is_already_lost():
    """The notice is written for the case where somebody else knows the password — in which
    case they may well have changed it, and "sign in and change your password" is advice the
    owner cannot take. Both remedies have to be there, and both have to be ones the code
    actually honours: `account_service.change_password` and `reset_service.consume` each call
    `email_change_service.retire_pending`."""
    text = " ".join(_notice().text.split())
    assert "Setting a new password cancels the pending change" in text
    assert "forgot password" in text


def test_the_notice_does_not_claim_the_session_drop_is_what_stops_it():
    """It is not. `confirm` never looks at sessions — a pending change is a bearer token in
    somebody's inbox, and `retire_pending` is the only thing that revokes it. The mail naming
    the wrong mechanism is how the next person to touch `change_password` deletes that call
    believing the session drop covers it."""
    text = " ".join(_notice().text.split())
    assert "signs you out on every device" not in text
    assert "because a new password signs out every other session" not in text


def test_a_newline_in_a_display_name_cannot_open_a_new_paragraph_in_the_mail():
    """`_one_line` guards the rows written before `SignupRequest` refused control characters.
    This flow mails an address the caller merely names, so an un-collapsed newline here is
    arbitrary text delivered to a stranger over the product's own sending domain."""
    injected = "Ada\n\nYour account is suspended, click https://evil.example.com"
    assert _one_line(injected) == ("Ada Your account is suspended, click https://evil.example.com")

    envelope = _confirmation(display_name=_one_line(injected))
    body_lines = [line for line in envelope.text.splitlines() if "evil.example.com" in line]
    assert len(body_lines) == 1
    assert body_lines[0].startswith("Hi Ada")


def test_the_copy_pluralises_the_window():
    """The TTL is a settings value that lands in prose in both mails, so it has to read
    correctly at the default of 1 and at anything else."""
    for envelope in (_confirmation(), _notice()):
        assert "1 hour" in " ".join(envelope.text.split())
        assert "hours" not in envelope.text
    for envelope in (_confirmation(expires_in_hours=6), _notice(expires_in_hours=6)):
        assert "6 hours" in " ".join(envelope.text.split())
