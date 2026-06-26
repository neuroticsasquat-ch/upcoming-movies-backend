from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmCredit,
    FilmGenre,
    FilmProductionCompany,
    Genre,
    Person,
    ProductionCompany,
)
from upmovies.main import app
from upmovies.news.models import Event, EventStory, EventSummary, Story


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as c:
        yield c


@pytest.fixture
def make_film(session: AsyncSession):
    counter = {"n": 0}

    async def _make(
        *,
        slug: str,
        title: str = "A Film",
        status: str | None = "Planned",
        release_date: date | None = date(2026, 7, 17),
        poster_path: str | None = "/poster.jpg",
        popularity: float | None = None,
        overview: str | None = None,
        tagline: str | None = None,
        runtime: int | None = None,
        vote_average: float | None = None,
        vote_count: int | None = None,
        original_language: str | None = None,
        backdrop_path: str | None = None,
        collection_id: int | None = None,
    ) -> Film:
        counter["n"] += 1
        film = Film(
            tmdb_id=1000 + counter["n"],
            slug=slug,
            title=title,
            status=status,
            release_date=release_date,
            poster_path=poster_path,
            popularity=popularity,
            overview=overview,
            tagline=tagline,
            runtime=runtime,
            vote_average=vote_average,
            vote_count=vote_count,
            original_language=original_language,
            backdrop_path=backdrop_path,
            collection_id=collection_id,
        )
        session.add(film)
        await session.commit()
        await session.refresh(film)
        return film

    return _make


@pytest.fixture
def make_collection(session: AsyncSession):
    async def _make(
        *,
        id: int,
        name: str,
        poster_path: str | None = None,
    ) -> Collection:
        col = Collection(id=id, name=name, poster_path=poster_path)
        session.add(col)
        await session.commit()
        return col

    return _make


@pytest.fixture
def attach_genres(session: AsyncSession):
    async def _attach(film: Film, genres: list[tuple[int, str]]) -> None:
        for genre_id, genre_name in genres:
            session.add(Genre(id=genre_id, name=genre_name))
        await session.flush()
        for genre_id, _name in genres:
            session.add(FilmGenre(film_id=film.id, genre_id=genre_id))
        await session.commit()

    return _attach


@pytest.fixture
def attach_companies(session: AsyncSession):
    async def _attach(film: Film, companies: list[tuple[int, str]]) -> None:
        for company_id, company_name in companies:
            session.add(ProductionCompany(id=company_id, name=company_name))
        await session.flush()
        for company_id, _name in companies:
            session.add(FilmProductionCompany(film_id=film.id, company_id=company_id))
        await session.commit()

    return _attach


@pytest.fixture
def add_event(session: AsyncSession):
    async def _add(
        *,
        film: Film,
        event_type: str = "casting",
        confidence: str = "confirmed",
        occurred_at: datetime = datetime(2025, 3, 1, tzinfo=UTC),
        created_at: datetime | None = None,
        summary: str | None = "A neutral summary.",
        sources: tuple[dict, ...] = (),
    ) -> Event:
        event = Event(
            film_id=film.id,
            event_type=event_type,
            confidence=confidence,
            occurred_at=occurred_at,
        )
        if created_at is not None:
            event.created_at = created_at
        session.add(event)
        await session.flush()  # populate event.id
        if summary is not None:
            session.add(
                EventSummary(
                    event_id=event.id,
                    summary=summary,
                    model="claude-haiku-4-5",
                    prompt_version="1",
                    source_updated_at=occurred_at,
                )
            )
        for src in sources:
            story = Story(
                source=src.get("source", "Deadline"),
                url=src["url"],
                title=src.get("title", "Story title"),
                published_at=src.get("published_at"),
                outlet=src.get("outlet"),
            )
            session.add(story)
            await session.flush()  # populate story.id
            session.add(EventStory(event_id=event.id, story_id=story.id))
        await session.commit()
        await session.refresh(event)
        return event

    return _add


@pytest.fixture
def add_release_date(session: AsyncSession):
    async def _add(
        *,
        film: Film,
        iso_3166_1: str = "US",
        release_type: int = 3,
        release_date: datetime,
        certification: str | None = None,
        note: str | None = None,
        iso_639_1: str | None = None,
    ) -> None:
        from upmovies.catalog.models import FilmReleaseDate

        rd = FilmReleaseDate(
            film_id=film.id,
            iso_3166_1=iso_3166_1,
            release_type=release_type,
            release_date=release_date,
            certification=certification,
            note=note,
            iso_639_1=iso_639_1,
        )
        session.add(rd)
        await session.commit()

    return _add


@pytest.fixture
def attach_alt_titles(session: AsyncSession):
    async def _attach(film: Film, titles: list[str]) -> None:
        for title in titles:
            session.add(FilmAlternativeTitle(film_id=film.id, title=title))
        await session.commit()

    return _attach


@pytest.fixture
def attach_credits(session: AsyncSession):
    async def _attach(
        film: Film,
        *,
        cast: list[dict] | None = None,
        crew: list[dict] | None = None,
    ) -> None:
        """Insert Person + FilmCredit rows for a film.

        cast entries: {id, name, character, credit_order, profile_path}
        crew entries: {id, name, job}
        """
        cast = cast or []
        crew = crew or []

        # Insert Person rows (avoid duplicates across cast and crew)
        seen_person_ids: set[int] = set()
        for entry in cast + crew:
            person_id = entry["id"]
            if person_id not in seen_person_ids:
                seen_person_ids.add(person_id)
                session.add(
                    Person(
                        id=person_id,
                        name=entry["name"],
                        profile_path=entry.get("profile_path"),
                    )
                )
        await session.flush()

        # Insert FilmCredit rows for cast members
        for entry in cast:
            session.add(
                FilmCredit(
                    credit_id=f"cast-{film.id}-{entry['id']}",
                    film_id=film.id,
                    person_id=entry["id"],
                    credit_type="cast",
                    character=entry.get("character"),
                    credit_order=entry.get("credit_order"),
                )
            )

        # Insert FilmCredit rows for crew members
        for entry in crew:
            session.add(
                FilmCredit(
                    credit_id=f"crew-{film.id}-{entry['id']}-{entry.get('job', 'unknown')}",
                    film_id=film.id,
                    person_id=entry["id"],
                    credit_type="crew",
                    job=entry.get("job"),
                    department=entry.get("department", "Directing"),
                )
            )

        await session.commit()

    return _attach
