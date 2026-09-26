"""Session + is_admin + CSRF protected endpoints for minting and listing invite codes from the
admin UI (NEU-1408, D-1408.1).

Human-facing, so `require_current_admin` (session cookie + `is_admin`) with `require_csrf` on the
write, the same gate `/admin/users` sits behind. The bearer `ADMIN_TOKEN` path this router used to
carry is gone rather than kept alongside: nothing but its own tests ever called it, and a route
that accepts two credentials is a third auth shape to reason about for nothing.

Minting sends nothing. The admin copies the signup link the page builds and delivers it themselves;
an invite gets someone an account, and access is still a separate grant at `/admin/users`."""

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import InviteCreateRequest, InviteOut
from upmovies.app.models import Invite
from upmovies.app.services import invite_service
from upmovies.deps import get_session, require_csrf, require_current_admin

router = APIRouter(
    prefix="/admin/invites",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


def _to_out(invite: Invite, consumed_by_email: str | None) -> InviteOut:
    return InviteOut(
        code=invite.code,
        email_hint=invite.email_hint,
        created_at=invite.created_at,
        consumed_at=invite.consumed_at,
        consumed_by_user_id=invite.consumed_by_user_id,
        consumed_by_email=consumed_by_email,
    )


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=InviteOut,
    dependencies=[Depends(require_csrf)],
)
async def create_invite_route(
    payload: InviteCreateRequest,
    db: AsyncSession = Depends(get_session),
) -> InviteOut:
    invite = await invite_service.create_invite(
        db, email_hint=str(payload.email_hint) if payload.email_hint else None
    )
    # Freshly minted, so unconsumed by construction.
    return _to_out(invite, None)


@router.get("", response_model=list[InviteOut])
async def list_invites_route(
    db: AsyncSession = Depends(get_session),
) -> list[InviteOut]:
    return [_to_out(invite, email) for invite, email in await invite_service.list_invites(db)]
