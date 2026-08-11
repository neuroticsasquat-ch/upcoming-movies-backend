"""Seed-grade credit history: the diff `catalog.film_credit`'s rebuild throws away.

`_upsert_credits` deletes a film's credit rows and reinserts the current set on every ingest,
so the table can answer "who is attached now?" and nothing else. A director being *attached*
is invisible — the row simply exists afterwards, with no record that it did not exist before.
This module recovers that signal at the one point in the system where both sides are already
in hand, and writes it to `catalog.film_credit_change` for NEU-1083 to card.

**First observation is a baseline, never a change** (ADR-0014, spec §5.3). This is the
safety-critical property of the whole credit half, and it is expressed structurally rather
than left to fall out of the rebuild's ordering: `diff_seed_credits` takes `previous=None`
for a film whose credits the catalog has never observed, and returns nothing for it whatever
the incoming set contains. `previous=set()` is a different statement — the film *was*
observed and held no seed-grade credit — and a director arriving then is a genuine
attachment.

Which of the two a film is, is read from the durable `film.credits_observed_at` marker and
**not** from `film_credit` being empty. A speculative TMDB entry can be admitted with an
empty credits payload, and inferring "never observed" from "holds no credits" would make
that film baseline again on the very next ingest — swallowing the first director to attach,
which is the single most valuable event the credit half exists to raise.

`film_field_change` gets the equivalent protection by accident, being a `BEFORE UPDATE`
trigger. Accidents do not survive a rewrite, and getting this wrong would emit tens of
thousands of false "attached to direct" rows the first day the expansion ran.
"""

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCredit, FilmCreditChange
from upmovies.catalog.seed_grade import is_seed_grade
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

CREDIT_ADDED = "added"
CREDIT_REMOVED = "removed"


@dataclass(frozen=True)
class SeedCredit:
    """One seed-grade credit, identified the way the diff must compare it.

    Not by `credit_id`: TMDB reissues those, and a reissued id on an unchanged attachment
    would read as a detachment followed by a re-attachment. `job` is part of the identity
    because one person can hold two seed-grade crew credits on a film they both wrote and
    directed, and losing one of those is a change.
    """

    person_id: int
    credit_type: str
    job: str | None


@dataclass(frozen=True)
class CreditChange:
    """One seed-grade credit crossing into or out of a film's credit set."""

    credit: SeedCredit
    change: str


def seed_credits_from_details(details: TMDBMovieDetails) -> set[SeedCredit]:
    """The seed-grade credits in a TMDB details payload — the *incoming* side of the diff."""
    credits = details.credits
    if credits is None:
        return set()
    seed: set[SeedCredit] = set()
    for member in credits.cast:
        if is_seed_grade("cast", None, member.order):
            seed.add(SeedCredit(person_id=member.id, credit_type="cast", job=None))
    for member in credits.crew:
        if is_seed_grade("crew", member.job, None):
            seed.add(SeedCredit(person_id=member.id, credit_type="crew", job=member.job))
    return seed


async def load_seed_credits(session: AsyncSession, film_id: UUID) -> set[SeedCredit] | None:
    """The seed-grade credits the catalog currently holds for a film — the *stored* side of
    the diff — or None if it has never observed this film's credits at all.

    Observedness comes from `film.credits_observed_at`, not from the credit rows: a film
    observed holding nothing returns an empty set, and a director arriving next run is a
    genuine attachment rather than a second baseline.

    Must be called **before** the rebuild's delete, which is the only reason this is a
    function and not a subquery.
    """
    observed_at = (
        await session.execute(select(Film.credits_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    if observed_at is None:
        return None
    stmt = select(
        FilmCredit.person_id, FilmCredit.credit_type, FilmCredit.job, FilmCredit.credit_order
    ).where(FilmCredit.film_id == film_id)
    return {
        SeedCredit(person_id=person_id, credit_type=credit_type, job=job)
        for person_id, credit_type, job, credit_order in (await session.execute(stmt)).all()
        if is_seed_grade(credit_type, job, credit_order)
    }


def diff_seed_credits(
    *, previous: Collection[SeedCredit] | None, current: Collection[SeedCredit]
) -> list[CreditChange]:
    """The seed-grade attachments and detachments between two observations of a film.

    `previous is None` means this is the film's first observed credit set, which is a
    **baseline, never a change** — the rule this whole module exists to guarantee.

    The diff is over set membership, not over the writes the rebuild performs. The rebuild
    deletes and reinserts unconditionally, so a diff phrased in terms of what it *did* would
    see every credit as removed-then-added on every single run.

    Additions before removals, each sorted, so a run's rows land in a stable order.
    """
    if previous is None:
        return []
    before, after = set(previous), set(current)
    return [
        *(CreditChange(credit=c, change=CREDIT_ADDED) for c in _ordered(after - before)),
        *(CreditChange(credit=c, change=CREDIT_REMOVED) for c in _ordered(before - after)),
    ]


def _ordered(credits: set[SeedCredit]) -> list[SeedCredit]:
    """A stable order for one side of the diff. `job` is normalized because a cast credit
    carries None there and None does not compare against a crew job."""
    return sorted(credits, key=lambda c: (c.person_id, c.credit_type, c.job or ""))


async def record_credit_changes(
    session: AsyncSession, film_id: UUID, changes: list[CreditChange]
) -> None:
    """Append the diff to `catalog.film_credit_change`. Pure DB I/O — the caller commits."""
    if not changes:
        return
    await session.execute(
        insert(FilmCreditChange).values(
            [
                {
                    "film_id": film_id,
                    "person_id": c.credit.person_id,
                    "credit_type": c.credit.credit_type,
                    "job": c.credit.job,
                    "change": c.change,
                }
                for c in changes
            ]
        )
    )


async def mark_credits_observed(session: AsyncSession, film_id: UUID) -> None:
    """Record that the catalog has now seen this film's credits, if it had not already.

    Write-once, guarded on the column rather than on the caller remembering the order: the
    marker's whole job is to be the thing the baseline rule cannot lose, so it must not be
    resettable by a later ingest. Pure DB I/O — the caller commits.
    """
    await session.execute(
        update(Film)
        .where(Film.id == film_id, Film.credits_observed_at.is_(None))
        .values(credits_observed_at=func.now())
    )
