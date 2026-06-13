"""Upsert TMDB movie details into the canonical `catalog.film` spine, keyed by
`tmdb_id`. Pure DB I/O — the caller owns the transaction (commit/rollback)."""

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


async def upsert_film(session: AsyncSession, details: TMDBMovieDetails) -> None:
    """Insert a new film or update the changed fields of an existing one (matched on
    `tmdb_id`). The surrogate `id` and `created_at` are preserved; `updated_at` is bumped."""
    values = {
        "tmdb_id": details.id,
        "imdb_id": details.imdb_id,
        "title": details.title,
        "original_title": details.original_title,
        "release_date": details.release_date,
        "status": details.status,
        "overview": details.overview,
        "poster_path": details.poster_path,
    }
    update_set = {k: v for k, v in values.items() if k != "tmdb_id"}
    update_set["updated_at"] = func.now()
    stmt = (
        insert(Film)
        .values(**values)
        .on_conflict_do_update(index_elements=[Film.tmdb_id], set_=update_set)
    )
    await session.execute(stmt)
