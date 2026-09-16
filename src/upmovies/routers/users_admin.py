"""Session + is_admin + CSRF protected endpoints for listing accounts and granting or revoking
their entitlement from the admin UI (D-38).

Human-facing, so `require_current_admin` (session cookie + `is_admin`) rather than the
`ADMIN_TOKEN` `require_admin` that gates `/admin/invites` next door — whose shape this otherwise
borrows. Granting access is a decision a person makes about another person, and the audit line it
writes is only worth writing if it names one.

These routes are the *only* way `app.user.entitled_until` is written until the billing project
takes over. There is no signup trial and no global setting (D-37), so an account that nobody has
visited here has no access at all."""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import AdminUserOut, AdminUserPage, EntitlementGrantRequest
from upmovies.app.errors import NotFound
from upmovies.app.models import User
from upmovies.app.services import entitlement_service
from upmovies.deps import get_session, require_csrf, require_current_admin

router = APIRouter(
    prefix="/admin/users",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)


def _to_out(user: User) -> AdminUserOut:
    return AdminUserOut(
        id=user.id,
        email=user.email,
        is_admin=user.is_admin,
        email_verified_at=user.email_verified_at,
        entitled_until=user.entitled_until,
        created_at=user.created_at,
    )


@router.get("", response_model=AdminUserPage)
async def list_users(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    q: str | None = Query(default=None, max_length=320),
    db: AsyncSession = Depends(get_session),
) -> AdminUserPage:
    """Accounts, newest first, optionally narrowed to those whose address contains `q`.

    Expired grants stay in the list rather than reading as never-granted: the page renders the
    raw `entitled_until`, so "lapsed last month" and "never had access" are distinguishable,
    which is the difference between extending a grant and making one."""
    items, total = await entitlement_service.list_accounts(
        db, limit=limit, offset=offset, email_query=q
    )
    return AdminUserPage(items=[_to_out(u) for u in items], total=total, limit=limit, offset=offset)


@router.put(
    "/{user_id}/entitlement",
    response_model=AdminUserOut,
    dependencies=[Depends(require_csrf)],
)
async def grant_entitlement(
    user_id: UUID,
    payload: EntitlementGrantRequest,
    db: AsyncSession = Depends(get_session),
    admin: User = Depends(require_current_admin),
) -> AdminUserOut:
    """Set this account's access expiry — the one write that grants or extends a grant.

    PUT rather than POST because it is idempotent by construction: the body carries the new
    expiry outright rather than a duration to add, so replaying it lands the same date. An
    admin extending a grant is choosing a date on a picker, not asking for "another year"."""
    try:
        user = await entitlement_service.grant(
            db, user_id=user_id, entitled_until=payload.entitled_until, granted_by=admin
        )
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found"
        ) from None
    return _to_out(user)


@router.delete(
    "/{user_id}/entitlement",
    response_model=AdminUserOut,
    dependencies=[Depends(require_csrf)],
)
async def revoke_entitlement(
    user_id: UUID,
    db: AsyncSession = Depends(get_session),
    admin: User = Depends(require_current_admin),
) -> AdminUserOut:
    """End this account's access, returning it to the state every signup starts in.

    Returns the account rather than 204 so the grant page can re-render the row it just changed
    without a second round trip, matching the grant above."""
    try:
        user = await entitlement_service.revoke(db, user_id=user_id, revoked_by=admin)
    except NotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found"
        ) from None
    return _to_out(user)
