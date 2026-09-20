"""Upsert TMDB movie details into the `catalog` schema: the canonical `catalog.film`
spine (keyed by `tmdb_id`) plus its normalized genre/company/country/language/collection
relations. Pure DB I/O — the caller owns the transaction (commit/rollback)."""

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmCredit,
    FilmGenre,
    FilmProductionCompany,
    FilmProductionCountry,
    FilmReleaseDate,
    FilmSpokenLanguage,
    Genre,
    Person,
    ProductionCompany,
    ProductionCountry,
    SpokenLanguage,
)
from upmovies.catalog.slug import assign_slug
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.credit_history import (
    diff_recorded_credits,
    load_followed_person_ids,
    load_recorded_credits,
    mark_credits_observed,
    record_credit_changes,
    recorded_credits_from_details,
)
from upmovies.ingest.tmdb.release_date_history import (
    diff_release_dates,
    displayable_from_details,
    load_displayable_releases,
    mark_release_dates_observed,
    record_release_date_changes,
)
from upmovies.ingest.tmdb.schemas import (
    TMDBCastMember,
    TMDBCrewMember,
    TMDBMovieDetails,
    TMDBPersonSearchHit,
)


async def mark_film_missing(session: AsyncSession, tmdb_id: int) -> None:
    """Record that TMDB has no entry at this film's id, as of now. Caller commits.

    Re-stamps a film that is already tombstoned, deliberately: the column drives the re-check
    cadence in `refresh_set_clause`, so a confirmed-still-missing film has to move forward or
    it comes back due on every subsequent pass (NEU-1124).

    Writes nothing else. The film keeps its page, its events and its stories; the spec is
    explicit that films are never deleted (§4.4).
    """
    await session.execute(
        update(Film).where(Film.tmdb_id == tmdb_id).values(tmdb_missing_at=datetime.now(UTC))
    )


async def mark_person_missing(session: AsyncSession, person_id: int) -> None:
    """Record that TMDB has no entry at this person's id, as of now. Caller commits.

    Takes them out of the seed set until a film ingest names them again — which is what clears
    the tombstone, so no cadence is needed on this side (NEU-1124). A person we hold no row for
    is a no-op: the seed set is built from `film_credit`, and a credit can outlive the person
    record we never wrote.
    """
    await session.execute(
        update(Person).where(Person.id == person_id).values(tmdb_missing_at=datetime.now(UTC))
    )


async def ensure_person_details(
    session: AsyncSession, client: TMDBClient, person_id: int
) -> Person | None:
    """Make sure this person's `/person/{id}` fields are on their `catalog.person` row, fetching
    them once if they are not. Returns the row, or None when we hold none. Caller commits.

    **Lazy, and once.** The sanity holds (D-8, NEU-1370) need `birthday` and `deathday`, which
    neither credits endpoint returns; backfilling them for every seed person would be tens of
    thousands of requests for dates that decide nothing about almost all of them. So this is
    called only for the people a credit event is about to name — one request per newly carded
    person, never per seed person — and `details_observed_at` is what makes it once rather than
    once per pass.

    Never refreshed after the first fetch, deliberately: a birthday does not change, and a death
    recorded upstream after we read the person is missed until someone clears the stamp by hand.
    That is the cheap side of the trade — the alternative is a cadence over the whole person
    table to catch an event that is rare and only ever *adds* a hold.

    A 404 tombstones the person the way every other id-addressed fetch here does, and returns
    None: TMDB has no such person, so there are no dates to decide anything on, and the caller
    treats that as "no reason to hold" rather than as a failure. It **also stamps**
    `details_observed_at`, because the question was asked and answered — without it a deleted
    person id sitting in the rolling window would be re-requested on every pass for the whole
    of `SWEEP_EVENT_LOOKBACK_DAYS`, which is the once-per-person guarantee gone. `tmdb_missing_at`
    cannot stand in for it: the person upsert clears that on revival, and the two columns answer
    different questions ("is this id live" against "have we asked for the dates").
    """
    person = await session.get(Person, person_id)
    if person is None or person.details_observed_at is not None:
        return person
    try:
        details = await client.person_details(person_id)
    except TMDBNotFound:
        await mark_person_missing(session, person_id)
        person.details_observed_at = datetime.now(UTC)
        await session.flush()
        return None
    person.birthday = details.birthday
    person.deathday = details.deathday
    # Refreshed rather than left alone: the response carries them, and writing the dates beside
    # a `popularity` we have just been handed a fresher value for would be a choice to hold a
    # stale one. `name` and the rest stay with the credits path, which sees them far more often.
    person.popularity = details.popularity
    person.profile_path = details.profile_path
    person.details_observed_at = datetime.now(UTC)
    await session.flush()
    return person


async def upsert_people(
    session: AsyncSession,
    members: Iterable[TMDBCastMember | TMDBCrewMember | TMDBPersonSearchHit],
) -> None:
    """Upsert `catalog.person` rows for these cast/crew entries or person search hits.
    Caller commits.

    Split out of `_upsert_credits` for the Letterboxd import (D-15), which needs exactly this
    and nothing else around it: a film the user rated four stars contributes its director and
    top-2 billing as people to follow, and is then discarded — no `catalog.film` row, no
    credits, because the catalog is the *upcoming*-film spine and a rated back-catalogue film
    has nothing left to announce (`NEU-1356-letterboxd-import.md` §3). Sharing the write rather
    than restating it is what keeps the two paths agreeing on the conflict set, and in
    particular on clearing `tmdb_missing_at`.

    A `/search/person` hit is accepted alongside them because person resolution (D-21) needs
    exactly the same write for the candidate it accepts: the hit carries the same seven person
    fields, and a person named in a trade story is as good a proof that the id is live as one
    named in a film's credits. Sharing this rather than writing `catalog.person` a third way
    is what keeps every path agreeing on the conflict set — most of all on clearing
    `tmdb_missing_at`, which a separate resolver write would be free to forget.

    Deduped by TMDB id, first entry winning, so one person billed and also credited as a
    writer is one row — and so the statement cannot raise the "ON CONFLICT DO UPDATE command
    cannot affect row a second time" that a duplicate id inside one `VALUES` produces."""
    people_by_id: dict[int, dict] = {}
    for m in members:
        if m.id not in people_by_id:
            people_by_id[m.id] = {
                "id": m.id,
                "name": m.name,
                "original_name": m.original_name,
                "profile_path": m.profile_path,
                "known_for_department": m.known_for_department,
                "gender": m.gender,
                "popularity": m.popularity,
            }
    if not people_by_id:
        return
    stmt = insert(Person).values(list(people_by_id.values()))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Person.id],
        set_={
            "name": stmt.excluded.name,
            "original_name": stmt.excluded.original_name,
            "profile_path": stmt.excluded.profile_path,
            "known_for_department": stmt.excluded.known_for_department,
            "gender": stmt.excluded.gender,
            "popularity": stmt.excluded.popularity,
            # TMDB returning this person at all — in a film's credits, or as a search hit —
            # is proof the id is live again, which is the whole revival path for a
            # tombstoned seed person (NEU-1124).
            "tmdb_missing_at": None,
        },
    )
    await session.execute(stmt)


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
    await _rebuild_alternative_titles(session, film_id, details)
    await _upsert_credits(session, film_id, details)


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
    # A successful read is proof the id is live, so it clears any tombstone
    # `mark_film_missing` left. TMDB restores and re-merges deleted entries, and without this
    # the refresh set's exclusion would be permanent on our side alone (NEU-1124).
    update_set["tmdb_missing_at"] = None
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
    """Rebuild `catalog.film_release_date`, capturing the displayable diff on the way through.

    The rebuild is the only place both sides of that diff exist: the stored rows are about to
    be deleted and the incoming ones are in the payload. `catalog.film_release_date_change` is
    written from them first, so a US wide date moving survives as history rather than being
    overwritten into silence. First observation is a baseline, never a change — see
    `ingest.tmdb.release_date_history`.
    """
    origin_country = (
        await session.execute(select(Film.origin_country).where(Film.id == film_id))
    ).scalar_one_or_none()
    # Read the stored side before the delete below wipes it.
    previous = await load_displayable_releases(session, film_id, origin_country=origin_country)
    changes = diff_release_dates(
        previous=previous,
        current=displayable_from_details(details, origin_country=origin_country),
    )
    await record_release_date_changes(session, film_id, changes)
    await mark_release_dates_observed(session, film_id)

    # Delete-then-reinsert, mirroring `_rebuild_joins`: a film that drops its release_dates
    # between runs (empty or absent payload) must have its stale rows cleared, so the delete
    # is unconditional and only the insert is guarded.
    await session.execute(delete(FilmReleaseDate).where(FilmReleaseDate.film_id == film_id))

    if not details.release_dates or not details.release_dates.results:
        return

    # Every type TMDB returns, filtered by nothing but a missing date. Narrowing this to the
    # displayable cut would look like a saving and would cost the no-backfill property
    # `catalog.release_grade` documents: a type admitted by a later widening has to already be
    # stored, or its first ingest reads it as a date newly *set* and cards the whole catalog.
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
        if entry.release_date is not None  # skip entries TMDB returned with empty date
    ]
    if rows:
        await session.execute(insert(FilmReleaseDate).values(rows))


async def _rebuild_alternative_titles(
    session: AsyncSession, film_id: UUID, details: TMDBMovieDetails
) -> None:
    # Delete-then-reinsert, mirroring `_rebuild_release_dates`: a film that drops its
    # alternative_titles between runs must have its stale rows cleared, so the delete is
    # unconditional and only the insert is guarded.
    await session.execute(
        delete(FilmAlternativeTitle).where(FilmAlternativeTitle.film_id == film_id)
    )

    if not details.alternative_titles or not details.alternative_titles.titles:
        return

    rows = [
        {
            "film_id": film_id,
            "iso_3166_1": t.iso_3166_1,
            "title": t.title,
            "title_type": t.type,
        }
        for t in details.alternative_titles.titles
    ]
    if rows:
        await session.execute(insert(FilmAlternativeTitle).values(rows))


async def _upsert_credits(session: AsyncSession, film_id: UUID, details: TMDBMovieDetails) -> None:
    """Upsert people from credits, then rebuild film_credit rows for this film.

    People are upserted (on_conflict_do_update) so that the same person appearing
    in multiple films only grows the catalog — never raises a duplicate-key error.
    Film credits are rebuilt (delete-and-reinsert) each run so that a person dropped
    from the cast/crew between runs is correctly removed.

    The rebuild is also where recorded-grade credit *history* is captured: both sides of the
    diff are in hand here and nowhere else, so `catalog.film_credit_change` is written from
    them before the old side is destroyed. First observation is a baseline, never a change —
    see `ingest.tmdb.credit_history`.
    """
    if not details.credits:
        return

    # Recorded grade's second half (D-49), read once and handed to *both* sides of the diff
    # below: judging them by two different answers is what would fabricate an attachment for
    # a credit that never moved. One query per film, on the same session as the rest.
    followed = await load_followed_person_ids(session)

    # Read the stored side before the delete below wipes it.
    previous_recorded_credits = await load_recorded_credits(session, film_id, followed=followed)

    # Step 1 — People: union of cast + crew, deduped by TMDB person id.
    await upsert_people(session, [*details.credits.cast, *details.credits.crew])

    # Step 2 — Credits rebuild: delete stale rows then reinsert current set.
    await session.execute(delete(FilmCredit).where(FilmCredit.film_id == film_id))

    credit_rows: dict[str, dict] = {}
    for m in details.credits.cast:
        if m.credit_id not in credit_rows:
            credit_rows[m.credit_id] = {
                "credit_id": m.credit_id,
                "film_id": film_id,
                "person_id": m.id,
                "credit_type": "cast",
                "department": "Acting",
                "job": None,
                "character": m.character,
                "credit_order": m.order,
            }
    for m in details.credits.crew:
        if m.credit_id not in credit_rows:
            credit_rows[m.credit_id] = {
                "credit_id": m.credit_id,
                "film_id": film_id,
                "person_id": m.id,
                "credit_type": "crew",
                "department": m.department,
                "job": m.job,
                "character": None,
                "credit_order": None,
            }

    if credit_rows:
        await session.execute(insert(FilmCredit).values(list(credit_rows.values())))

    # Step 3 — History: what the rebuild would otherwise have thrown away. The marker is set
    # last and only here, so the very first pass writes a baseline and every later one a diff.
    await record_credit_changes(
        session,
        film_id,
        diff_recorded_credits(
            previous=previous_recorded_credits,
            current=recorded_credits_from_details(details, followed=followed),
        ),
    )
    await mark_credits_observed(session, film_id)
