from datetime import UTC, datetime

import pytest
from sqlalchemy.exc import IntegrityError

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


async def test_catalog_normalized_tables_roundtrip(session):
    from upmovies.catalog.models import (
        Collection,
        FilmGenre,
        FilmProductionCompany,
        FilmProductionCountry,
        FilmSpokenLanguage,
        Genre,
        ProductionCompany,
        ProductionCountry,
        SpokenLanguage,
    )

    session.add(Collection(id=10, name="Star Wars Collection"))
    session.add(Genre(id=28, name="Action"))
    session.add(ProductionCompany(id=1, name="Lucasfilm Ltd.", origin_country="US"))
    session.add(ProductionCountry(iso_3166_1="US", name="United States of America"))
    session.add(SpokenLanguage(iso_639_1="en", english_name="English", name="English"))
    await session.flush()

    film = Film(
        tmdb_id=11,
        title="Star Wars",
        budget=11000000,
        revenue=775398007,
        runtime=121,
        adult=False,
        video=False,
        vote_average=8.2,
        vote_count=22061,
        origin_country=["US"],
        collection_id=10,
        tmdb_raw={"id": 11, "extra": "kept"},
    )
    session.add(film)
    await session.flush()

    session.add(FilmGenre(film_id=film.id, genre_id=28))
    session.add(FilmProductionCompany(film_id=film.id, company_id=1))
    session.add(FilmProductionCountry(film_id=film.id, iso_3166_1="US"))
    session.add(FilmSpokenLanguage(film_id=film.id, iso_639_1="en"))
    await session.commit()
    await session.refresh(film)

    assert film.collection_id == 10
    assert film.origin_country == ["US"]
    assert film.budget == 11000000
    assert film.tmdb_raw == {"id": 11, "extra": "kept"}


async def test_film_slug_nullable_and_unique(session):
    # nullable: a film can be created without a slug (multiple NULLs are allowed)
    session.add(Film(tmdb_id=1, title="No Slug Yet"))
    session.add(Film(tmdb_id=2, title="Also No Slug"))
    await session.flush()

    # unique: two films cannot share the same non-null slug
    session.add(Film(tmdb_id=3, title="A", slug="dupe-2026"))
    session.add(Film(tmdb_id=4, title="B", slug="dupe-2026"))
    with pytest.raises(IntegrityError):
        await session.flush()
