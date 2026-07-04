"""Builds the prompt-cached film roster from `catalog.film`. Films are referred to by a
1-based index (not their UUID) so the model never has to copy ids verbatim — the linker
maps the index back to a film_id."""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmGenre, Genre
from upmovies.catalog.queries import active_film_clause
from upmovies.config import get_settings

_OVERVIEW_MAX = 120


@dataclass(frozen=True)
class RosterEntry:
    film_id: UUID
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    genres: list[str]


@dataclass(frozen=True)
class Roster:
    entries: list[RosterEntry]
    text: str

    def film_id_for_index(self, index: object) -> UUID | None:
        if isinstance(index, int) and 1 <= index <= len(self.entries):
            return self.entries[index - 1].film_id
        return None


def _render(entries: list[RosterEntry]) -> str:
    lines: list[str] = []
    for i, e in enumerate(entries, start=1):
        parts = [f'#{i} "{e.title}"']
        if e.year is not None:
            parts.append(f"({e.year})")
        if e.original_title and e.original_title != e.title:
            parts.append(f"[orig: {e.original_title}]")
        if e.genres:
            parts.append(f"genres: {', '.join(e.genres)}")
        line = " ".join(parts)
        if e.overview:
            line += f" — {e.overview}"
        lines.append(line)
    return "\n".join(lines)


async def build_roster(session: AsyncSession) -> Roster:
    excluded = get_settings().tmdb_excluded_statuses
    today = datetime.now(UTC).date()
    films = (
        (
            await session.execute(
                select(Film)
                .where(active_film_clause(today=today, excluded_statuses=excluded))
                .order_by(Film.title)
            )
        )
        .scalars()
        .all()
    )
    genre_rows = (
        await session.execute(
            select(FilmGenre.film_id, Genre.name).join(Genre, Genre.id == FilmGenre.genre_id)
        )
    ).all()
    genres_by_film: dict[UUID, list[str]] = defaultdict(list)
    for film_id, name in genre_rows:
        genres_by_film[film_id].append(name)

    entries = [
        RosterEntry(
            film_id=f.id,
            title=f.title,
            original_title=f.original_title,
            year=f.release_date.year if f.release_date else None,
            overview=(f.overview[:_OVERVIEW_MAX] if f.overview else None),
            genres=sorted(genres_by_film.get(f.id, [])),
        )
        for f in films
    ]
    return Roster(entries=entries, text=_render(entries))
