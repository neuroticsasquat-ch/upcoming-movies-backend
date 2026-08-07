from datetime import date

from upmovies.catalog.models import (
    Collection,
    Film,
    FilmAlternativeTitle,
    FilmCredit,
    FilmGenre,
    Genre,
    Person,
)
from upmovies.link.retrieval.index import build_candidate_index
from upmovies.link.retrieval.normalize import significant_tokens


def _future() -> date:
    """A release date that keeps a film active whenever the suite runs."""
    return date(date.today().year + 1, 7, 15)


async def test_index_covers_titles_original_titles_and_alternative_titles(session):
    film = Film(
        tmdb_id=1,
        title="Nagabandham",
        original_title="నాగబంధం",
        release_date=_future(),
    )
    session.add(film)
    await session.flush()
    session.add(FilmAlternativeTitle(film_id=film.id, title="The Snake Bond", iso_3166_1="US"))
    await session.commit()

    index = await build_candidate_index(session)

    assert index.size == 1
    assert index.films_for_token("nagabandham") == frozenset({film.id})
    assert index.films_for_token("snake") == frozenset({film.id})
    # The original title is searchable under its normalized token — which is not the
    # literal source string, since normalize composes to NFC before splitting.
    (original_token,) = significant_tokens("నాగబంధం")
    assert index.films_for_token(original_token) == frozenset({film.id})


async def test_search_scope_is_unchanged_released_and_canceled_films_stay_out(session):
    session.add_all(
        [
            Film(tmdb_id=10, title="Active", release_date=_future(), status="Post Production"),
            Film(tmdb_id=11, title="Released", release_date=date(2026, 1, 1), status="Released"),
            Film(tmdb_id=12, title="Canceled", release_date=_future(), status="Canceled"),
        ]
    )
    await session.commit()

    index = await build_candidate_index(session)

    assert [f.title for f in index.films] == ["Active"]


async def test_alternative_titles_of_inactive_films_do_not_leak_into_the_index(session):
    released = Film(tmdb_id=20, title="Released", release_date=date(2026, 1, 1), status="Released")
    session.add(released)
    await session.flush()
    session.add(FilmAlternativeTitle(film_id=released.id, title="Estrenada", iso_3166_1="ES"))
    await session.commit()

    index = await build_candidate_index(session)

    assert index.size == 0
    assert index.films_for_token("estrenada") == frozenset()


async def test_display_fields_are_carried_on_the_index(session):
    session.add(Collection(id=99, name="The Runner Collection"))
    session.add(Genre(id=18, name="Drama"))
    session.add(Genre(id=53, name="Thriller"))
    session.add_all(
        [
            Person(id=1, name="Ana Ruiz"),
            Person(id=2, name="Lee Park"),
            Person(id=3, name="Mira Sen"),
            Person(id=4, name="Tom Ade"),
            Person(id=5, name="Unbilled Extra"),
        ]
    )
    await session.flush()
    release = _future()
    film = Film(
        tmdb_id=2,
        title="Runner",
        original_title="Coureur",
        release_date=release,
        overview="A courier runs.",
        collection_id=99,
    )
    session.add(film)
    await session.flush()
    session.add_all(
        [
            FilmGenre(film_id=film.id, genre_id=53),
            FilmGenre(film_id=film.id, genre_id=18),
            FilmCredit(
                credit_id="c-dir",
                film_id=film.id,
                person_id=1,
                credit_type="crew",
                department="Directing",
                job="Director",
            ),
            FilmCredit(
                credit_id="c-0",
                film_id=film.id,
                person_id=2,
                credit_type="cast",
                credit_order=0,
            ),
            FilmCredit(
                credit_id="c-1",
                film_id=film.id,
                person_id=3,
                credit_type="cast",
                credit_order=1,
            ),
            FilmCredit(
                credit_id="c-2",
                film_id=film.id,
                person_id=4,
                credit_type="cast",
                credit_order=2,
            ),
            FilmCredit(
                credit_id="c-3",
                film_id=film.id,
                person_id=5,
                credit_type="cast",
                credit_order=3,
            ),
        ]
    )
    await session.commit()

    index = await build_candidate_index(session)
    entry = index.film(film.id)

    assert entry is not None
    assert entry.title == "Runner"
    assert entry.original_title == "Coureur"
    assert entry.year == release.year
    assert entry.overview == "A courier runs."
    assert entry.genres == ("Drama", "Thriller")
    assert entry.director == "Ana Ruiz"
    # Top three billed only — the fourth is dropped (§4.3).
    assert entry.cast == ("Lee Park", "Mira Sen", "Tom Ade")
    assert entry.collection == "The Runner Collection"


async def test_films_without_credits_genres_or_a_collection_still_index(session):
    film = Film(tmdb_id=3, title="Bare Bones", release_date=_future())
    session.add(film)
    await session.commit()

    index = await build_candidate_index(session)
    entry = index.film(film.id)

    assert entry is not None
    assert entry.genres == ()
    assert entry.cast == ()
    assert entry.director is None
    assert entry.collection is None
    assert entry.original_title is None


async def test_undated_films_stay_in_scope_and_carry_no_year(session):
    film = Film(tmdb_id=4, title="Untitled Project", release_date=None)
    session.add(film)
    await session.commit()

    index = await build_candidate_index(session)
    entry = index.film(film.id)

    assert entry is not None
    assert entry.year is None


async def test_multiple_directors_are_joined_in_billing_order(session):
    session.add_all([Person(id=10, name="Joel Coen"), Person(id=11, name="Ethan Coen")])
    await session.flush()
    film = Film(tmdb_id=5, title="Two Directors", release_date=_future())
    session.add(film)
    await session.flush()
    session.add_all(
        [
            FilmCredit(
                credit_id="d-1",
                film_id=film.id,
                person_id=11,
                credit_type="crew",
                job="Director",
                credit_order=1,
            ),
            FilmCredit(
                credit_id="d-0",
                film_id=film.id,
                person_id=10,
                credit_type="crew",
                job="Director",
                credit_order=0,
            ),
        ]
    )
    await session.commit()

    index = await build_candidate_index(session)
    entry = index.film(film.id)

    assert entry is not None
    assert entry.director == "Joel Coen, Ethan Coen"


async def test_the_index_is_ordered_by_title(session):
    session.add_all(
        [
            Film(tmdb_id=30, title="Zulu Dawn", release_date=_future()),
            Film(tmdb_id=31, title="Alpha Wave", release_date=_future()),
        ]
    )
    await session.commit()

    index = await build_candidate_index(session)

    assert [f.title for f in index.films] == ["Alpha Wave", "Zulu Dawn"]


async def test_an_empty_catalog_builds_an_empty_index(session):
    index = await build_candidate_index(session)

    assert index.size == 0
    assert index.films == ()
