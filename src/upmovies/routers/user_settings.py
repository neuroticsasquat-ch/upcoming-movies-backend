"""`/me/settings`: the digest cadence and the calendar token (D-33, D-34), subscriber-only
(D-39).

Same shape as `/me/follows` next door — `require_entitled()` applied once at the router so a
route added later cannot forget it, cookie session plus CSRF on the writes. The gate is what
makes the lazily created settings row a subscriber-only artefact: an unentitled account never
gets one, and D-40 keeps the row (token included) when a grant lapses."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import UserSettingsOut, UserSettingsUpdateRequest
from upmovies.app.entitlements import require_entitled
from upmovies.app.models import User
from upmovies.app.services import settings_service
from upmovies.deps import get_session, require_csrf

entitled = require_entitled()

router = APIRouter(prefix="/me/settings", tags=["me"], dependencies=[Depends(entitled)])


@router.get("", response_model=UserSettingsOut)
async def get_settings(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> UserSettingsOut:
    """This user's settings, creating the defaults row on the first read (D-33)."""
    row = await settings_service.get_or_create(db, user=user)
    return UserSettingsOut.model_validate(row)


@router.patch("", response_model=UserSettingsOut, dependencies=[Depends(require_csrf)])
async def update_settings(
    payload: UserSettingsUpdateRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> UserSettingsOut:
    row = await settings_service.set_digest_cadence(
        db, user=user, digest_cadence=payload.digest_cadence
    )
    return UserSettingsOut.model_validate(row)


@router.post(
    "/ical-token/rotate", response_model=UserSettingsOut, dependencies=[Depends(require_csrf)]
)
async def rotate_ical_token(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> UserSettingsOut:
    """Issue a new calendar token, breaking every calendar subscribed to the old URL (D-34).

    A POST rather than a PATCH of `ical_token`: the caller does not choose the new value, and
    the old one cannot be got back. 200 with the whole settings row, so the panel that rendered
    the old URL can redraw from one response."""
    row = await settings_service.rotate_ical_token(db, user=user)
    return UserSettingsOut.model_validate(row)
