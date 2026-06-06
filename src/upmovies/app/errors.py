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
