"""Displayable release-date history: the diff `catalog.film_release_date`'s rebuild throws away.

`_rebuild_release_dates` deletes a film's release rows and reinserts the current set on every
ingest, so the table can answer "when does it open?" and nothing else. A US wide date *moving*
is invisible — the row simply holds the new value afterwards, with no record of the old one.
This module recovers that signal at the one point where both sides are in hand, and writes it
to `catalog.film_release_date_change` for the sweep to card (NEU-1121).

**Why not the primary date.** Before NEU-1121 release-date events came from `film.release_date`
via the `film_field_change` trigger — free, and wrong. That column is TMDB's *primary* date: the
earliest release in any country of any type. The film page shows US-or-origin theatrical dates
only, so the event routinely cited a date the page never displayed, and on 2026-08-11 a fifth of
them were for films whose page showed no release date at all. The subject of a release-date
event is now a **displayable** row (`catalog.release_grade`), and the primary date raises none.

**First observation is a baseline, never a change** (ADR-0014, spec §5.3), expressed structurally
exactly as `credit_history` does it: `diff_release_dates` takes `previous=None` for a film whose
release slate the catalog has never observed and returns nothing for it, whatever the incoming
set contains. `previous=[]` is a different statement — the film *was* observed and had no
displayable date — and a date arriving then is a real beat.

Which of the two a film is, is read from the durable `film.release_dates_observed_at` marker and
**not** from `film_release_date` being empty. An undated film is admitted with no release rows at
all, so inferring "never observed" from "holds nothing" would make it baseline again on every
ingest and swallow the first date it is ever given — which for this project's whole undated
population is the single most valuable event there is.
"""

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmReleaseDate, FilmReleaseDateChange
from upmovies.catalog.release_grade import is_displayable_release
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

RELEASE_DATE_SET = "set"
RELEASE_DATE_MOVED = "moved"


@dataclass(frozen=True)
class DisplayableRelease:
    """One displayable release row, reduced to the fields the diff compares.

    `(iso_3166_1, release_type)` is the **subject** — the thing a card is about. US limited and
    US wide are two subjects on one film, because the page lists them as two lines and a
    distributor can move one without the other.
    """

    iso_3166_1: str
    release_type: int
    release_date: date


@dataclass(frozen=True)
class ReleaseDateChange:
    """One displayable release date being set or moved. Withdrawals are not changes here."""

    release: DisplayableRelease
    change: str
    previous_date: date | None


def _subject(r: DisplayableRelease) -> tuple[str, int]:
    return (r.iso_3166_1, r.release_type)


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def displayable_from_rows(
    rows: Iterable[tuple[str, int, datetime | date]], *, origin_country: Sequence[str] | None
) -> list[DisplayableRelease]:
    """The displayable subset of `(iso_3166_1, release_type, release_date)` tuples."""
    return [
        DisplayableRelease(iso, rtype, _as_date(when))
        for iso, rtype, when in rows
        if is_displayable_release(iso_3166_1=iso, release_type=rtype, origin_country=origin_country)
    ]


def displayable_from_details(
    details: TMDBMovieDetails, *, origin_country: Sequence[str] | None
) -> list[DisplayableRelease]:
    """The displayable releases in a TMDB details payload — the *incoming* side of the diff."""
    if not details.release_dates or not details.release_dates.results:
        return []
    return displayable_from_rows(
        (
            (country.iso_3166_1, entry.type, entry.release_date)
            for country in details.release_dates.results
            for entry in country.release_dates
            if entry.release_date is not None
        ),
        origin_country=origin_country,
    )


async def load_displayable_releases(
    session: AsyncSession, film_id: UUID, *, origin_country: Sequence[str] | None
) -> list[DisplayableRelease] | None:
    """The displayable releases the catalog currently holds — the *stored* side of the diff —
    or None if it has never observed this film's release slate at all.

    Observedness comes from `film.release_dates_observed_at`, not from the rows: see the module
    docstring for why that distinction is load-bearing for undated films specifically.

    Must be called **before** the rebuild's delete, which is the only reason this is a function
    and not a subquery.
    """
    observed_at = (
        await session.execute(select(Film.release_dates_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    if observed_at is None:
        return None
    rows = (
        await session.execute(
            select(
                FilmReleaseDate.iso_3166_1,
                FilmReleaseDate.release_type,
                FilmReleaseDate.release_date,
            ).where(FilmReleaseDate.film_id == film_id)
        )
    ).all()
    return displayable_from_rows(
        ((iso, rtype, when) for iso, rtype, when in rows), origin_country=origin_country
    )


def diff_release_dates(
    *,
    previous: Collection[DisplayableRelease] | None,
    current: Collection[DisplayableRelease],
) -> list[ReleaseDateChange]:
    """The displayable release dates set or moved between two observations of a film.

    `previous is None` means this is the film's first observed slate, which is a **baseline,
    never a change** — the rule this module exists to guarantee.

    A subject present on both sides with a different date is `moved`; one present only now is
    `set`. A subject that *disappeared* yields nothing: the page simply stops listing it, and
    "the US release date was withdrawn" is not a beat this site cards — matching what
    `classify_field_change` already did for the primary date, where there would be no date to
    render either.

    Sorted by subject so a run's rows land in a stable order.
    """
    if previous is None:
        return []
    before = {_subject(r): r.release_date for r in previous}
    changes: list[ReleaseDateChange] = []
    for release in sorted(current, key=_subject):
        was = before.get(_subject(release))
        if was is None:
            changes.append(
                ReleaseDateChange(release=release, change=RELEASE_DATE_SET, previous_date=None)
            )
        elif was != release.release_date:
            changes.append(
                ReleaseDateChange(release=release, change=RELEASE_DATE_MOVED, previous_date=was)
            )
    return changes


async def record_release_date_changes(
    session: AsyncSession, film_id: UUID, changes: list[ReleaseDateChange]
) -> None:
    """Append the diff to `catalog.film_release_date_change`. Pure DB I/O — caller commits."""
    if not changes:
        return
    await session.execute(
        insert(FilmReleaseDateChange).values(
            [
                {
                    "film_id": film_id,
                    "iso_3166_1": c.release.iso_3166_1,
                    "release_type": c.release.release_type,
                    "previous_date": c.previous_date,
                    "new_date": c.release.release_date,
                    "change": c.change,
                }
                for c in changes
            ]
        )
    )


async def mark_release_dates_observed(session: AsyncSession, film_id: UUID) -> None:
    """Record that the catalog has now seen this film's release slate, if it had not already.

    Write-once, guarded on the column rather than on the caller remembering the order — the
    marker's whole job is to be the thing the baseline rule cannot lose. Pure DB I/O.
    """
    await session.execute(
        update(Film)
        .where(Film.id == film_id, Film.release_dates_observed_at.is_(None))
        .values(release_dates_observed_at=func.now())
    )
