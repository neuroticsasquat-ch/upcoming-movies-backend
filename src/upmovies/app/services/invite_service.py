"""Invite-code generation. Consumption logic lives inside account_service.signup
since it's part of the user-creation transaction."""

import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import Invite
from upmovies.app.repos import invite_repo


def _new_code() -> str:
    """A URL-safe random code with enough entropy that brute-forcing is impractical."""
    return secrets.token_urlsafe(16)


async def create_invite(db: AsyncSession, *, email_hint: str | None = None) -> Invite:
    """Generate a fresh invite, persist it, commit. Returns the row."""
    code = _new_code()
    invite = await invite_repo.create(db, code=code, email_hint=email_hint)
    await db.commit()
    return invite


async def list_invites(db: AsyncSession) -> list[Invite]:
    """Admin-only listing of every invite ever issued."""
    return await invite_repo.list_all(db)
