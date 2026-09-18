"""The **headline release**: the one date a film row leads with where there is room for one.

A watchlist row, a timeline row, an iCal entry — each shows a film once, with a single date.
That date has to be a date this site would actually display, or the row disagrees with the film
page it links to. `catalog.film.release_date` cannot do the job: it is TMDB's *primary* date,
the earliest release in any country of any type, and the film page never lists it (see
`release_grade`). NEU-1121 already closed the same trap for release-date events.

So the headline release is a **choice among the film's governing release dates** — one per
`(country, release_type)` subject, earliest row in the subject, restricted to the *theatrical*
half of the cut `release_grade` defines (`THEATRICAL_RELEASE_TYPES`) — not a new quantity:

1. the earliest **upcoming** governing date (`kind="upcoming"`), today counting as upcoming;
2. failing that, the most recent **past** one (`kind="released"`) — a watchlist is partly a
   record of things already out, and "No date yet" on a released film is wrong;
3. failing that, the **primary** date (`kind="primary"`, no country or bucket), which mirrors
   the film page's own unlabelled fallback in `public.service.get_film` so the two surfaces
   never disagree. The caller is expected to mark it unconfirmed rather than pass it off as a
   listed date.

**Theatrical only, on purpose**, even though the film page and the calendar now also list the
US home release (D-26): "the one date this film leads with" is the opening, and a digital date
three months later must not displace it. D-34's iCal feed wants a VEVENT for *each* of
theatrical, digital and physical, which is a different question and asks it separately.

The primary is the **last** resort rather than the first precisely because it is the date the
page declines to show. A film with no displayable row and no primary date has no headline
release at all, and callers render that as "No date yet".

Same-day ties break wide before limited, then `US` before an origin country, then row id, so
the answer is deterministic for a film whose subjects land together. `today` is supplied by the
caller as a Python-side UTC date, never SQL `CURRENT_DATE`, following `get_calendar`.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Literal
from uuid import UUID

from sqlalchemy import Date, and_, any_, case, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmReleaseDate
from upmovies.catalog.release_grade import (
    PRIMARY_REGION,
    THEATRICAL_RELEASE_TYPES,
    release_bucket,
)

HeadlineReleaseKind = Literal["upcoming", "released", "primary"]
"""Which of the three rules produced the date — the frontend renders each differently, and
`primary` in particular is the unconfirmed one."""


@dataclass(frozen=True, slots=True)
class HeadlineRelease:
    """One film's headline release: the date, which rule chose it, and the subject it came from.

    `country` and `bucket` are non-null for the two displayable kinds and null exactly when
    `kind` is `"primary"` — the primary date belongs to no country and no theatrical bucket.
    """

    date: date
    kind: HeadlineReleaseKind
    country: str | None
    bucket: str | None

    def __post_init__(self) -> None:
        """The contract `HeadlineReleaseOut` publishes, held here so both ends of it are one
        object's business: a displayable kind always names the subject it came from, and the
        primary fallback belongs to no country and no theatrical bucket."""
        is_primary = self.kind == "primary"
        if is_primary is not (self.country is None) or is_primary is not (self.bucket is None):
            raise ValueError(f"country and bucket are set iff kind is not 'primary': {self!r}")


async def headline_releases(
    session: AsyncSession, film_ids: Collection[UUID], *, today: date
) -> dict[UUID, HeadlineRelease]:
    """The headline release of each of `film_ids` that has one, in two statements flat.

    Two, not one per film: this serves list endpoints, so the region test is expressed in SQL
    (`iso_3166_1 = 'US' OR iso_3166_1 = ANY(film.origin_country)` — `displayable_regions`'
    semantics) rather than as a Python filter over fetched rows, and the whole batch resolves in
    one round trip per statement however long the watchlist is. The second statement runs only
    for the ids the first left unresolved.

    Films with neither a displayable release row nor a primary date are **absent** from the
    result; callers keep their place in the list and render the absence.
    """
    if not film_ids:
        return {}

    ids = list(film_ids)

    # One row per (film, country, type) subject, holding the subject's governing date — the
    # earliest date in it (NEU-1206) — as a UTC calendar date, the way `get_calendar` casts it.
    # `min_id` rides along as the tie-break of last resort.
    governing = (
        select(
            FilmReleaseDate.film_id.label("film_id"),
            FilmReleaseDate.iso_3166_1.label("iso_3166_1"),
            FilmReleaseDate.release_type.label("release_type"),
            func.min(cast(func.timezone("UTC", FilmReleaseDate.release_date), Date)).label(
                "governing_date"
            ),
            func.min(FilmReleaseDate.id).label("min_id"),
        )
        .join(Film, Film.id == FilmReleaseDate.film_id)
        .where(
            FilmReleaseDate.film_id.in_(ids),
            FilmReleaseDate.release_type.in_(tuple(sorted(THEATRICAL_RELEASE_TYPES))),
            or_(
                FilmReleaseDate.iso_3166_1 == PRIMARY_REGION,
                # `displayable_regions` drops falsy origin entries; the `!= ""` is that same
                # filter, and it has to be here rather than in Python or a film carrying an
                # empty origin code would surface rows the film page declines to list.
                and_(
                    FilmReleaseDate.iso_3166_1 != "",
                    FilmReleaseDate.iso_3166_1 == any_(Film.origin_country),
                ),
            ),
        )
        .group_by(
            FilmReleaseDate.film_id,
            FilmReleaseDate.iso_3166_1,
            FilmReleaseDate.release_type,
        )
        .cte("governing")
    )

    is_upcoming = governing.c.governing_date >= today

    # `DISTINCT ON (film_id)` keeps the first row per film under this ordering, which *is* the
    # selection rule: upcoming subjects before past ones, soonest among the upcoming, latest
    # among the past, then the deterministic tie-break. The two `CASE`s are how one ORDER BY
    # expresses ascending within one group and descending within the other; each is null in the
    # group it does not govern, where the rows are already tied.
    chosen = (
        select(
            governing.c.film_id,
            governing.c.governing_date,
            governing.c.iso_3166_1,
            governing.c.release_type,
            is_upcoming.label("is_upcoming"),
        )
        .distinct(governing.c.film_id)
        .order_by(
            governing.c.film_id.asc(),
            is_upcoming.desc(),
            case((is_upcoming, governing.c.governing_date)).asc(),
            case((~is_upcoming, governing.c.governing_date)).desc(),
            governing.c.release_type.desc(),  # 3 (wide) before 2 (limited)
            (governing.c.iso_3166_1 == PRIMARY_REGION).desc(),
            governing.c.min_id.asc(),
        )
    )

    resolved: dict[UUID, HeadlineRelease] = {
        row.film_id: HeadlineRelease(
            date=row.governing_date,
            kind="upcoming" if row.is_upcoming else "released",
            country=row.iso_3166_1,
            bucket=release_bucket(row.release_type),
        )
        for row in await session.execute(chosen)
    }

    unresolved = [film_id for film_id in ids if film_id not in resolved]
    if not unresolved:
        return resolved

    primaries = await session.execute(
        select(Film.id, Film.release_date).where(
            Film.id.in_(unresolved), Film.release_date.is_not(None)
        )
    )
    for film_id, primary_date in primaries:
        resolved[film_id] = HeadlineRelease(
            date=primary_date, kind="primary", country=None, bucket=None
        )
    return resolved
