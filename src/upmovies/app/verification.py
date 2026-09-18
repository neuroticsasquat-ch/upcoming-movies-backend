"""Whether a user's email address is confirmed — the one place that reads
`User.email_verified_at`.

A module of its own, beside the entitlement seam it will sit next to (D-38's
`app/entitlements.py`), because the *callers* are not here yet: the M7 notify and digest
passes are what actually act on this (D-31 suppresses queued mail to an unverified user), and
they should import a named rule rather than re-deriving `email_verified_at is not None` in
each pass.

Note what this is not: an access gate. Verification gates outbound mail and nothing else
(D-18) — an unverified user keeps full app access — so there is deliberately no
`require_verified()` dependency here to be reached for by mistake. The batch clause below is
not one either: it selects *recipients*, and the pass records the users it excludes as
`suppressed` rather than skipping them."""

from sqlalchemy import ColumnElement

from upmovies.app.models import User


def is_verified(user: User) -> bool:
    """True once this user has confirmed their email address."""
    return user.email_verified_at is not None


def verified_user_clause() -> ColumnElement[bool]:
    """The batch form of the same rule: a predicate over `app.user` for a pass that selects
    recipients rather than answering a request.

    Beside `entitlements.entitled_user_clause()` and shaped like it deliberately — D-31's
    suppression and D-39's are one decision at one place in the notify pass, and a rule spelled
    in Python here and in SQL there would be two definitions of "may we mail this person"."""
    return User.email_verified_at.is_not(None)
