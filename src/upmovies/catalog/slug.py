"""Slug generation for `catalog.film` public URLs. The pure functions (`base_slug`,
`resolve_unique`, `backfill_slugs`) are the single source of truth shared by the live insert
path and the Alembic backfill; `assign_slug` wraps them with the DB clash check. Slugs are
derived from title + release year, collision-safe via a deterministic `-{tmdb_id}` suffix, and
immutable once assigned."""

from datetime import date
from uuid import UUID

from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film


def base_slug(title: str, release_date: date | None, tmdb_id: int) -> str:
    """`<slugified-title>-<year>`, or the bare title slug when there is no release date.
    An empty stem (untransliterable / all-punctuation title) falls back to `film-{tmdb_id}`,
    which is already globally unique (tmdb_id is unique), so it needs no year or collision step."""
    stem = slugify(title)
    if not stem:
        return f"film-{tmdb_id}"
    if release_date is not None:
        return f"{stem}-{release_date.year}"
    return stem


def resolve_unique(base: str, taken: set[str], tmdb_id: int) -> str:
    """Return `base` if free, else append `-{tmdb_id}` (deterministic, globally unique).
    Callers must pass ALL currently assigned slugs in `taken` (not just base-form slugs)
    so that tmdb_id-suffixed slugs already in use are also checked."""
    return f"{base}-{tmdb_id}" if base in taken else base


def backfill_slugs(
    rows: list[tuple[UUID, str, date | None, int]],
) -> list[tuple[UUID, str]]:
    """Assign a unique slug to each `(id, title, release_date, tmdb_id)` row. Callers pass rows
    pre-sorted by `tmdb_id` ascending so the lowest tmdb_id deterministically wins the clean base
    and disambiguation is stable. Pure — the migration delegates its backfill to this."""
    taken: set[str] = set()
    out: list[tuple[UUID, str]] = []
    for film_id, title, release_date, tmdb_id in rows:
        slug = resolve_unique(base_slug(title, release_date, tmdb_id), taken, tmdb_id)
        taken.add(slug)
        out.append((film_id, slug))
    return out


async def assign_slug(
    session: AsyncSession, *, title: str, release_date: date | None, tmdb_id: int
) -> str:
    """Collision-safe slug for a NEW film. Builds the base, checks the unique index for a clash,
    and disambiguates with `-{tmdb_id}` if needed. The DB unique constraint is the final
    backstop."""
    base = base_slug(title, release_date, tmdb_id)
    clash = (
        await session.execute(select(Film.slug).where(Film.slug == base).limit(1))
    ).scalar_one_or_none()
    return resolve_unique(base, {base} if clash is not None else set(), tmdb_id)
