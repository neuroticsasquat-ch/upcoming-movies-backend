from __future__ import annotations


class DomainError(Exception):
    """Base for app-level expected errors."""


class EmailInUse(DomainError):
    pass


class InvalidCredentials(DomainError):
    pass


class NotFound(DomainError):
    pass


class NothingToStop(DomainError):
    """`DELETE /me/watchlist/{film_id}` for a film nothing covers and nobody has muted (D-45).

    Its own error rather than `NotFound`, because the film exists and the two answers say
    different things to the caller: `film_not_found` is a bad id, `watchlist_item_not_found` is
    a real film that was never on this user's list."""


class InvalidInvite(DomainError):
    """Invite code is unknown, already consumed, or doesn't match the signup email."""


class InvalidToken(DomainError):
    """A mailed token is unknown, already consumed, expired, or issued for another purpose.

    One error for all four, because the holder of a link is told the same thing either way —
    ask for a new one — and distinguishing them would say whether a token ever existed."""
