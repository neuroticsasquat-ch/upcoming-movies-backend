from datetime import UTC, datetime

from upmovies.app.models import User
from upmovies.catalog.models import Film
from upmovies.news.models import Story


async def test_film_story_seam_roundtrips(session):
    film = Film(tmdb_id=12345, title="Untitled Project")
    session.add(film)
    await session.flush()

    story = Story(
        source="deadline",
        url="https://example.com/a",
        title="Casting announced",
        fetched_at=datetime.now(UTC),
        film_id=film.id,
    )
    session.add(story)
    await session.commit()
    await session.refresh(story)

    assert story.film_id == film.id
    assert film.tmdb_id == 12345


async def test_user_email_is_unique(session):
    session.add(User(email="a@b.com", password_hash="x", display_name="A"))
    await session.commit()
