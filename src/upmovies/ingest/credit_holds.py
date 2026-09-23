"""Reading and manually releasing `ingest.credit_hold` (D-8, §4).

The admin half of the sanity holds. The sweep opens and closes these rows on its own and needs
nothing here; this exists for the case the design is explicit about — a hold that is *correct
by the rule and wrong in fact*. A prolific documentary producer really does pick up twenty
credits in a day, and a posthumous release really is attached years after the death, so
withholding those beats forever would be the check doing more damage than the vandalism it was
built for. A manual release is the escape hatch, and it is honoured for as long as the row
survives the rolling window — `sanity_holds` reads `release_reason = 'manual'` and never
re-holds the change it names.

Kept out of the sweep module because the direction of use is opposite: everything in
`ingest.sweep` runs from a scheduled pass with a `session_factory` and owns its own
transactions, while these two run inside a request against the session `deps.get_session`
hands them, and the router commits.

`open_hold_keys` is the exception, and is here rather than in the sweep for a different
reason: *two* readers outside the sweep now have to honour an open hold — the sweep's own
backlog loader and the Tier-A short-circuit (`news.attachment_confirm`, NEU-1371) — and the
short-circuit cannot import `ingest.sweep`, which imports it.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, Person
from upmovies.ingest.models import RELEASE_MANUAL, CreditHold


async def open_hold_keys(
    session: AsyncSession, *, since: datetime
) -> set[tuple[UUID, int, str, datetime]]:
    """Every attachment an open hold is currently withholding, keyed the way the sweep's
    backlog keys an attachment: `(film_id, person_id, role, changed_at)`.

    `credit_hold.credit_type` stores the seed-grade *role*, not TMDB's `cast`/`crew` split,
    which is what makes the two keys comparable without re-deriving anything.
    """
    stmt = select(
        CreditHold.film_id, CreditHold.person_id, CreditHold.credit_type, CreditHold.changed_at
    ).where(CreditHold.released_at.is_(None), CreditHold.changed_at >= since)
    return {
        (film_id, person_id, role, changed_at)
        for film_id, person_id, role, changed_at in await session.execute(stmt)
    }


class HoldNotFound(Exception):
    """No `ingest.credit_hold` row with this id."""


class HoldAlreadyReleased(Exception):
    """The hold is already closed, so there is nothing to release.

    Its own case rather than an idempotent no-op: the three release reasons mean different
    things, and quietly re-stamping an `expired` row as `manual` would claim an admin let a
    beat through that no longer exists to be let through.
    """


@dataclass(frozen=True)
class HoldRow:
    """One hold with the two names that make it readable — the film and the person. Both are
    joined rather than looked up per row: the page is a list, and a hold whose whole point is
    "does this credit look right to you" is unreadable as a pair of ids."""

    hold: CreditHold
    film: Film | None
    person: Person | None


async def list_holds(session: AsyncSession, *, is_open: bool, limit: int) -> list[HoldRow]:
    """Holds, newest first, either the open ones or the released ones.

    No cursor, unlike the resolution queue: the open set is bounded by the rolling window that
    expires it, so "every hold in play" is a page rather than a stream. `limit` is a ceiling
    against a vandalism run big enough to be its own denial of service, not a paging device.
    """
    stmt = (
        select(CreditHold, Film, Person)
        .outerjoin(Film, Film.id == CreditHold.film_id)
        .outerjoin(Person, Person.id == CreditHold.person_id)
        .order_by(CreditHold.held_at.desc(), CreditHold.id.desc())
        .limit(limit)
    )
    stmt = stmt.where(
        CreditHold.released_at.is_(None) if is_open else CreditHold.released_at.isnot(None)
    )
    return [
        HoldRow(hold=hold, film=film, person=person)
        for hold, film, person in await session.execute(stmt)
    ]


@dataclass(frozen=True)
class Released:
    """What a manual release did, with both fields non-optional.

    The row's own `released_at`/`release_reason` are nullable — that is what makes a hold open
    — so returning the row would hand the caller two Optionals it has just been guaranteed are
    set. Saying so in the return type is what keeps the caller from narrowing them with an
    `assert`, which `python -O` strips.
    """

    id: UUID
    released_at: datetime
    release_reason: str


async def release_hold(session: AsyncSession, *, hold_id: UUID) -> Released:
    """Release one hold by hand. Caller commits.

    Writes `manual` rather than `cleared` deliberately, even though both re-admit the change:
    `cleared` is a claim that the *condition* stopped holding, which a human overriding it has
    not established. The distinction is what `sanity_holds` reads to know it must not re-hold
    this change on the next pass — a `cleared` row it may re-hold, because the condition it
    tests can come back.

    The change is re-admitted to the backlog and cards on the next sweep pass, still subject
    to the ordinary quarantine and suppression rules: releasing a hold says the credit is not
    vandalism, not that it skips the queue.
    """
    hold = await session.get(CreditHold, hold_id)
    if hold is None:
        raise HoldNotFound
    if hold.released_at is not None:
        raise HoldAlreadyReleased
    released_at = datetime.now(UTC)
    hold.released_at = released_at
    hold.release_reason = RELEASE_MANUAL
    await session.flush()
    return Released(id=hold.id, released_at=released_at, release_reason=RELEASE_MANUAL)
