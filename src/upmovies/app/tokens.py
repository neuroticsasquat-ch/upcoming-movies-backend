import secrets


def new_session_id() -> str:
    return secrets.token_urlsafe(32)


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def new_email_token() -> str:
    """A token to put in a link in an email (verification, and the reset and email-change
    flows that follow it in M1).

    Same generator and same width as the session id above, deliberately: a verification link
    is a bearer credential for the duration of its life, and there is no reason to make the
    one that travels through a mail server the narrower of the two."""
    return secrets.token_urlsafe(32)


def new_ical_token() -> str:
    """The token in a user's calendar feed URL (D-34).

    Same generator and width as the session id again, and here the reason is load-bearing rather
    than merely consistent: `/calendar/{token}.ics` is fetched by a calendar client that cannot
    hold a cookie, so this string *is* the authentication for that feed — an unauthenticated
    bearer credential, sitting in a URL that a phone will re-fetch for years. 256 bits of
    `secrets` is what makes guessing one not worth attempting; rotation (D-34) is what handles a
    URL that leaked rather than one that was guessed."""
    return secrets.token_urlsafe(32)
