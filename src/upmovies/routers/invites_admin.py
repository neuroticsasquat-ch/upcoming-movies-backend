"""Admin endpoints for managing invite codes. Protected by ADMIN_TOKEN."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import InviteCreateRequest, InviteOut
from upmovies.app.services import invite_service
from upmovies.deps import get_session, require_admin

router = APIRouter(prefix="/admin/invites", tags=["admin"], dependencies=[Depends(require_admin)])


@router.post("", status_code=status.HTTP_201_CREATED, response_model=InviteOut)
async def create_invite_route(
    payload: InviteCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> InviteOut:
    invite = await invite_service.create_invite(
        db, email_hint=str(payload.email_hint) if payload.email_hint else None
    )
    return InviteOut(
        code=invite.code,
        email_hint=invite.email_hint,
        created_at=invite.created_at,
        consumed_at=invite.consumed_at,
        consumed_by_user_id=invite.consumed_by_user_id,
    )


@router.get("", response_model=list[InviteOut])
async def list_invites_route(
    db: AsyncSession = Depends(get_session),
) -> list[InviteOut]:
    invites = await invite_service.list_invites(db)
    return [
        InviteOut(
            code=i.code,
            email_hint=i.email_hint,
            created_at=i.created_at,
            consumed_at=i.consumed_at,
            consumed_by_user_id=i.consumed_by_user_id,
        )
        for i in invites
    ]
