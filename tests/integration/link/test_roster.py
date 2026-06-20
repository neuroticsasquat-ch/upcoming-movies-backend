from datetime import date

from upmovies.catalog.models import Film, FilmGenre, Genre
from upmovies.link.roster import build_roster


async def test_build_roster_includes_title_year_genre_and_index(session):
    film = Film(tmdb_id=1, title="The Odyssey", release_date=date(2026, 7, 15), overview="Epic.")
    session.add(film)
    await session.flush()
    session.add(Genre(id=12, name="Adventure"))
    await session.flush()
    session.add(FilmGenre(film_id=film.id, genre_id=12))
    await session.commit()

    roster = await build_roster(session)

    assert len(roster.entries) == 1
    entry = roster.entries[0]
    assert entry.title == "The Odyssey"
    assert entry.year == 2026
    assert entry.genres == ["Adventure"]
    assert '#1 "The Odyssey"' in roster.text
    assert "(2026)" in roster.text
    assert roster.film_id_for_index(1) == film.id
    assert roster.film_id_for_index(2) is None
