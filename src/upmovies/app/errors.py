from __future__ import annotations


class DomainError(Exception):
    """Base for app-level expected errors."""


class EmailInUse(DomainError):
    pass


class InvalidCredentials(DomainError):
    pass


class NotFound(DomainError):
    pass


class InvalidInvite(DomainError):
    """Invite code is unknown, already consumed, or doesn't match the signup email."""


class InvalidToken(DomainError):
    """A mailed token is unknown, already consumed, expired, or issued for another purpose.

    One error for all four, because the holder of a link is told the same thing either way —
    ask for a new one — and distinguishing them would say whether a token ever existed."""
