"""Whether a user's email address is confirmed — the one place that reads
`User.email_verified_at`.

A module of its own, beside the entitlement seam it will sit next to (D-38's
`app/entitlements.py`), because the *callers* are not here yet: the M7 notify and digest
passes are what actually act on this (D-31 suppresses queued mail to an unverified user), and
they should import a named rule rather than re-deriving `email_verified_at is not None` in
each pass.

Note what this is not: an access gate. Verification gates outbound mail and nothing else
(D-18) — an unverified user keeps full app access — so there is deliberately no
`require_verified()` dependency here to be reached for by mistake."""

from upmovies.app.models import User


def is_verified(user: User) -> bool:
    """True once this user has confirmed their email address."""
    return user.email_verified_at is not None
