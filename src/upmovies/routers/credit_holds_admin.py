"""Session + is_admin protected endpoints over `ingest.credit_hold` (D-8, §4).

Human-facing, so `require_current_admin` (session cookie + `is_admin`) rather than the
ADMIN_TOKEN `require_admin` next door: deciding whether a credit really is vandalism is a
judgement somebody makes by looking, not a thing a cron does.

**JSON only — there is no page in this ticket.** The endpoints exist anyway because a hold is
the one part of the sweep that can be *correct by the rule and wrong in fact*, and without a
release path a genuine beat withheld by a mis-tuned threshold would sit there until it aged out
with nobody able to do anything about it. An admin token and `curl` are enough to unblock one,
which is the bar until the page exists.

CSRF guards the release and not the list, matching `moderation_admin` on the one and
`resolution_admin` on the other: the write is a cookie-authed mutation, the read is not.
"""

from datetime import date, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.deps import get_session, require_csrf, require_current_admin
from upmovies.ingest import credit_holds

router = APIRouter(
    prefix="/admin/credit-holds",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)

HOLD_PAGE_LIMIT = 500


class HoldFilmOut(BaseModel):
    """The film the credit was attached to. Null when it has since been deleted, which leaves
    the hold standing and its film gone."""

    id: UUID
    tmdb_id: int
    title: str


class HoldPersonOut(BaseModel):
    """The person the credit names, with the two dates two of the three reasons are decided
    on — so "why is this held" is answerable from the row rather than from TMDB."""

    id: int
    name: str
    birthday: date | None
    deathday: date | None


class CreditHoldOut(BaseModel):
    """One hold, open or closed."""

    id: UUID
    film: HoldFilmOut | None
    person: HoldPersonOut | None
    credit_type: str
    reason: str
    changed_at: datetime
    held_at: datetime
    released_at: datetime | None
    release_reason: str | None


@router.get("", response_model=list[CreditHoldOut])
async def list_credit_holds(
    open: bool = Query(default=True, description="true: open holds; false: released ones"),
    db: AsyncSession = Depends(get_session),
) -> list[CreditHoldOut]:
    """Holds, newest first — the open ones by default, the released ones with `open=false`.

    The two are one endpoint rather than two because the question an admin arrives with is
    "what is being withheld", and the released set is the same rows a day later; splitting
    them would make "did my release take" a different URL from the one that showed the hold.
    """
    rows = await credit_holds.list_holds(db, is_open=open, limit=HOLD_PAGE_LIMIT)
    return [
        CreditHoldOut(
            id=row.hold.id,
            film=(
                HoldFilmOut(id=row.film.id, tmdb_id=row.film.tmdb_id, title=row.film.title)
                if row.film is not None
                else None
            ),
            person=(
                HoldPersonOut(
                    id=row.person.id,
                    name=row.person.name,
                    birthday=row.person.birthday,
                    deathday=row.person.deathday,
                )
                if row.person is not None
                else None
            ),
            credit_type=row.hold.credit_type,
            reason=row.hold.reason,
            changed_at=row.hold.changed_at,
            held_at=row.hold.held_at,
            released_at=row.hold.released_at,
            release_reason=row.hold.release_reason,
        )
        for row in rows
    ]


class ReleasedOut(BaseModel):
    """What the release changed, and nothing else — the shape of `credit_holds.Released`.

    Not a `CreditHoldOut`: the film and person on that shape are joins the release does not
    make, and answering with them nulled would read as "this hold has no film" rather than as
    "you did not ask for one". The list endpoint is where a hold is looked at.
    """

    id: UUID
    released_at: datetime
    release_reason: str


@router.post(
    "/{hold_id}/release",
    response_model=ReleasedOut,
    dependencies=[Depends(require_csrf)],
)
async def release_credit_hold(
    hold_id: UUID, db: AsyncSession = Depends(get_session)
) -> ReleasedOut:
    """Let one held attachment through. It cards on the next sweep pass, still subject to the
    ordinary quarantine and suppression rules, and no check holds it again."""
    try:
        released = await credit_holds.release_hold(db, hold_id=hold_id)
    except credit_holds.HoldNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="hold_not_found"
        ) from None
    except credit_holds.HoldAlreadyReleased:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="hold_already_released"
        ) from None
    await db.commit()
    return ReleasedOut(**vars(released))
