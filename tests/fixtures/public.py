from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
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
    ) -> Film:
        counter["n"] += 1
        film = Film(
            tmdb_id=1000 + counter["n"],
            slug=slug,
            title=title,
            status=status,
            release_date=release_date,
            poster_path=poster_path,
        )
        session.add(film)
        await session.commit()
        await session.refresh(film)
        return film

    return _make


@pytest.fixture
def add_event(session: AsyncSession):
    async def _add(
        *,
        film: Film,
        event_type: str = "casting",
        confidence: str = "confirmed",
        occurred_at: datetime = datetime(2025, 3, 1, tzinfo=UTC),
        summary: str | None = "A neutral summary.",
        sources: tuple[dict, ...] = (),
    ) -> Event:
        event = Event(
            film_id=film.id,
            event_type=event_type,
            confidence=confidence,
            occurred_at=occurred_at,
        )
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
            )
            session.add(story)
            await session.flush()  # populate story.id
            session.add(EventStory(event_id=event.id, story_id=story.id))
        await session.commit()
        await session.refresh(event)
        return event

    return _add
