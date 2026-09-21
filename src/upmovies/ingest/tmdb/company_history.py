"""Studio history: the diff `catalog.film_production_company`'s rebuild throws away (EF-5).

`_rebuild_joins` deletes a film's company rows and reinserts the current set on every ingest,
so the table can answer "who is making this now?" and nothing else. A studio *joining* is
invisible — the row simply exists afterwards, with no record that it did not exist before.
This module recovers that signal at the one point in the system where both sides are already
in hand, and writes it to `catalog.film_company_change` for the sweep to card.

It is `ingest.tmdb.credit_history` for companies, deliberately built to the same shape, and
the two differences are worth naming:

- **There is no recorded grade.** A credit is written down only when it is seed grade or its
  person is followed (D-49), because TMDB hands us forty cast members per film and most of
  them are noise. A film carries a handful of production companies, TMDB publishes no ordering
  over them, and no product decision distinguishes them — so every company that crosses the
  set is recorded, and the `followed` set this module's credit counterpart threads through
  both sides of its diff has no analogue here.
- **The previous set is an argument, not a lookup.** `diff_companies` takes it, and
  `load_observed_companies` is a separate call the rebuild makes. That is the seam NEU-1436
  (EF-4) attaches to: admission-as-attachment has to replace *only* the `previous is None`
  branch — writing an `added` row for each incoming company a user already follows — and a
  diff that read the stored side itself would leave nowhere to stand.

**First observation is a baseline, never a change** (ADR-0014, spec §5.3) is the property this
module exists to guarantee, and it is expressed structurally: `diff_companies` takes
`previous=None` for a film whose companies the catalog has never observed and returns nothing
for it whatever the incoming set contains. `previous=set()` is a different statement — the
film *was* observed and TMDB listed no companies for it — and a studio arriving then is a
genuine attachment.

Which of the two a film is, is read from the durable `film.companies_observed_at` marker and
**not** from `film_production_company` being empty, for the reason the credit half documents at
length: a speculative TMDB entry is routinely admitted with no companies attached, and
inferring "never observed" from "holds nothing" would make that film baseline again on the very
next ingest — swallowing the first studio to attach, which is the single most valuable event
the company half exists to raise.
"""

from collections.abc import Collection
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmCompanyChange, FilmProductionCompany
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

COMPANY_ADDED = "added"
COMPANY_REMOVED = "removed"


@dataclass(frozen=True)
class CompanyChange:
    """One production company crossing into or out of a film's company set."""

    company_id: int
    change: str


def companies_from_details(details: TMDBMovieDetails) -> set[int]:
    """The company ids in a TMDB details payload — the *incoming* side of the diff.

    A set of ids rather than of rows: `film_production_company` is keyed on
    `(film_id, company_id)` and carries nothing else, so the id is the whole identity and a
    payload that lists one company twice is one attachment.
    """
    return {company.id for company in details.production_companies}


async def load_observed_companies(session: AsyncSession, film_id: UUID) -> set[int] | None:
    """The company ids the catalog currently holds for a film — the *stored* side of the diff —
    or None if it has never observed this film's companies at all.

    Observedness comes from `film.companies_observed_at`, not from the join rows: a film
    observed holding nothing returns an empty set, and a studio arriving next run is a genuine
    attachment rather than a second baseline.

    Must be called **before** the rebuild's delete, which is the only reason this is a function
    and not a subquery.
    """
    observed_at = (
        await session.execute(select(Film.companies_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    if observed_at is None:
        return None
    stmt = select(FilmProductionCompany.company_id).where(FilmProductionCompany.film_id == film_id)
    return set((await session.execute(stmt)).scalars().all())


def diff_companies(
    *, previous: Collection[int] | None, current: Collection[int]
) -> list[CompanyChange]:
    """The company attachments and detachments between two observations of a film.

    `previous is None` means this is the film's first observed company set, which is a
    **baseline, never a change** — the rule this whole module exists to guarantee, and the one
    branch NEU-1436 replaces to make admission an attachment for followed studios (EF-4).

    The diff is over set membership, not over the writes the rebuild performs. The rebuild
    deletes and reinserts unconditionally, so a diff phrased in terms of what it *did* would
    see every company as removed-then-added on every single run.

    Additions before removals, each sorted by id, so a run's rows land in a stable order.
    """
    if previous is None:
        return []
    before, after = set(previous), set(current)
    return [
        *(CompanyChange(company_id=c, change=COMPANY_ADDED) for c in sorted(after - before)),
        *(CompanyChange(company_id=c, change=COMPANY_REMOVED) for c in sorted(before - after)),
    ]


async def record_company_changes(
    session: AsyncSession, film_id: UUID, changes: list[CompanyChange]
) -> None:
    """Append the diff to `catalog.film_company_change`. Pure DB I/O — the caller commits."""
    if not changes:
        return
    await session.execute(
        insert(FilmCompanyChange).values(
            [{"film_id": film_id, "company_id": c.company_id, "change": c.change} for c in changes]
        )
    )


async def mark_companies_observed(session: AsyncSession, film_id: UUID) -> None:
    """Record that the catalog has now seen this film's production companies, if it had not
    already.

    Write-once, guarded on the column rather than on the caller remembering the order, exactly
    as `mark_credits_observed` is: the marker's whole job is to be the thing the baseline rule
    cannot lose, so it must not be resettable by a later ingest. Pure DB I/O — the caller
    commits.
    """
    await session.execute(
        update(Film)
        .where(Film.id == film_id, Film.companies_observed_at.is_(None))
        .values(companies_observed_at=func.now())
    )
