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
