"""Upsert TMDB movie details into the `catalog` schema: the canonical `catalog.film`
spine (keyed by `tmdb_id`) plus its normalized genre/company/country/language/collection
relations. Pure DB I/O — the caller owns the transaction (commit/rollback)."""

from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmGenre,
    FilmProductionCompany,
    FilmProductionCountry,
    FilmReleaseDate,
    FilmSpokenLanguage,
    Genre,
    ProductionCompany,
    ProductionCountry,
    SpokenLanguage,
)
from upmovies.catalog.slug import assign_slug
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


async def upsert_film(session: AsyncSession, details: TMDBMovieDetails) -> None:
    """Insert/update a film and its relations (matched on `tmdb_id`). The surrogate `id`
    and `created_at` are preserved; `updated_at` is bumped. Reference rows are upserted by
    their natural keys; join rows are rebuilt (delete-and-reinsert) so a film dropping a
    genre/company between runs is reflected."""
    collection_id = await _upsert_collection(session, details)
    film_id = await _upsert_film_row(session, details, collection_id)
    await _upsert_references(session, details)
    await _rebuild_joins(session, film_id, details)
    await _rebuild_release_dates(session, film_id, details)


async def _upsert_collection(session: AsyncSession, details: TMDBMovieDetails) -> int | None:
    c = details.belongs_to_collection
    if c is None:
        return None
    stmt = insert(Collection).values(
        id=c.id, name=c.name, poster_path=c.poster_path, backdrop_path=c.backdrop_path
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[Collection.id],
        set_={
            "name": stmt.excluded.name,
            "poster_path": stmt.excluded.poster_path,
            "backdrop_path": stmt.excluded.backdrop_path,
        },
    )
    await session.execute(stmt)
    return c.id


async def _upsert_film_row(
    session: AsyncSession, details: TMDBMovieDetails, collection_id: int | None
) -> UUID:
    slug = await _slug_for_insert(session, details)
    values = {
        "tmdb_id": details.id,
        "slug": slug,
        "imdb_id": details.imdb_id,
        "title": details.title,
        "original_title": details.original_title,
        "release_date": details.release_date,
        "status": details.status,
        "overview": details.overview,
        "poster_path": details.poster_path,
        "adult": details.adult,
        "backdrop_path": details.backdrop_path,
        "budget": details.budget,
        "homepage": details.homepage,
        "original_language": details.original_language,
        "popularity": details.popularity,
        "revenue": details.revenue,
        "runtime": details.runtime,
        "tagline": details.tagline,
        "video": details.video,
        "vote_average": details.vote_average,
        "vote_count": details.vote_count,
        "origin_country": details.origin_country,
        "collection_id": collection_id,
        "tmdb_raw": details.tmdb_raw,
    }
    update_set = {k: v for k, v in values.items() if k not in ("tmdb_id", "slug")}
    update_set["updated_at"] = func.now()
    stmt = (
        insert(Film)
        .values(**values)
        .on_conflict_do_update(index_elements=[Film.tmdb_id], set_=update_set)
        .returning(Film.id)
    )
    return (await session.execute(stmt)).scalar_one()


async def _slug_for_insert(session: AsyncSession, details: TMDBMovieDetails) -> str | None:
    """An existing film (matched on `tmdb_id`) keeps its stored slug — it is excluded from the
    `DO UPDATE` set, so the value here is only used when the row is actually inserted. A new film
    gets a freshly assigned collision-safe slug."""
    row = (await session.execute(select(Film.slug).where(Film.tmdb_id == details.id))).one_or_none()
    if row is not None:
        existing_slug = row[0]
        if existing_slug is not None:
            return existing_slug
    return await assign_slug(
        session, title=details.title, release_date=details.release_date, tmdb_id=details.id
    )


async def _upsert_references(session: AsyncSession, details: TMDBMovieDetails) -> None:
    if details.genres:
        stmt = insert(Genre).values([{"id": g.id, "name": g.name} for g in details.genres])
        stmt = stmt.on_conflict_do_update(
            index_elements=[Genre.id], set_={"name": stmt.excluded.name}
        )
        await session.execute(stmt)

    if details.production_companies:
        stmt = insert(ProductionCompany).values(
            [
                {
                    "id": c.id,
                    "name": c.name,
                    "logo_path": c.logo_path,
                    "origin_country": c.origin_country,
                }
                for c in details.production_companies
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProductionCompany.id],
            set_={
                "name": stmt.excluded.name,
                "logo_path": stmt.excluded.logo_path,
                "origin_country": stmt.excluded.origin_country,
            },
        )
        await session.execute(stmt)

    if details.production_countries:
        stmt = insert(ProductionCountry).values(
            [{"iso_3166_1": pc.iso_3166_1, "name": pc.name} for pc in details.production_countries]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[ProductionCountry.iso_3166_1], set_={"name": stmt.excluded.name}
        )
        await session.execute(stmt)

    if details.spoken_languages:
        stmt = insert(SpokenLanguage).values(
            [
                {"iso_639_1": sl.iso_639_1, "english_name": sl.english_name, "name": sl.name}
                for sl in details.spoken_languages
            ]
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[SpokenLanguage.iso_639_1],
            set_={"english_name": stmt.excluded.english_name, "name": stmt.excluded.name},
        )
        await session.execute(stmt)


async def _rebuild_joins(session: AsyncSession, film_id: UUID, details: TMDBMovieDetails) -> None:
    await session.execute(delete(FilmGenre).where(FilmGenre.film_id == film_id))
    await session.execute(
        delete(FilmProductionCompany).where(FilmProductionCompany.film_id == film_id)
    )
    await session.execute(
        delete(FilmProductionCountry).where(FilmProductionCountry.film_id == film_id)
    )
    await session.execute(delete(FilmSpokenLanguage).where(FilmSpokenLanguage.film_id == film_id))

    if details.genres:
        await session.execute(
            insert(FilmGenre).values(
                [{"film_id": film_id, "genre_id": g.id} for g in details.genres]
            )
        )
    if details.production_companies:
        await session.execute(
            insert(FilmProductionCompany).values(
                [{"film_id": film_id, "company_id": c.id} for c in details.production_companies]
            )
        )
    if details.production_countries:
        await session.execute(
            insert(FilmProductionCountry).values(
                [
                    {"film_id": film_id, "iso_3166_1": pc.iso_3166_1}
                    for pc in details.production_countries
                ]
            )
        )
    if details.spoken_languages:
        await session.execute(
            insert(FilmSpokenLanguage).values(
                [{"film_id": film_id, "iso_639_1": sl.iso_639_1} for sl in details.spoken_languages]
            )
        )


async def _rebuild_release_dates(
    session: AsyncSession, film_id: UUID, details: TMDBMovieDetails
) -> None:
    if not details.release_dates or not details.release_dates.results:
        return

    await session.execute(delete(FilmReleaseDate).where(FilmReleaseDate.film_id == film_id))

    rows = [
        {
            "film_id": film_id,
            "iso_3166_1": country.iso_3166_1,
            "release_type": entry.type,
            "release_date": entry.release_date,
            "certification": entry.certification,
            "note": entry.note,
            "iso_639_1": entry.iso_639_1,
        }
        for country in details.release_dates.results
        for entry in country.release_dates
    ]
    if rows:
        await session.execute(insert(FilmReleaseDate).values(rows))
