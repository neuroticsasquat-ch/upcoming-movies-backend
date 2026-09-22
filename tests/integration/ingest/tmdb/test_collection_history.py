"""The synthetic `collection_id` history row admission writes for a followed franchise
(EF-4, NEU-1436, D-1436.4).

`film_field_change_trg` is `BEFORE UPDATE`, so a film **inserted** already belonging to a
collection writes no history and NEU-1434's reader never learns of it. That is right for every
franchise nobody follows and wrong for the one case the follow was made for, so the admission
path writes the row the trigger would have written on an update.

These are integration tests and not unit ones because the property under test is precisely
what the trigger does and does not do — which only a real round trip can show. The rows
written here must be indistinguishable from the trigger's, including on the *count*: an update
into a followed collection has to stay one row, not two.
"""

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow
from upmovies.catalog.models import COLLECTION_FIELD, Film, FilmFieldChange
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film

DUNE = {"id": 726871, "name": "Dune Collection"}
ALIEN = {"id": 8091, "name": "Alien Collection"}


def _details(tmdb_id: int, collection: dict | None = None) -> TMDBMovieDetails:
    return TMDBMovieDetails.model_validate(make_details(tmdb_id, belongs_to_collection=collection))


async def _collection_changes(session, tmdb_id: int) -> list[FilmFieldChange]:
    stmt = (
        select(FilmFieldChange)
        .join(Film, Film.id == FilmFieldChange.film_id)
        .where(Film.tmdb_id == tmdb_id, FilmFieldChange.field == COLLECTION_FIELD)
        .order_by(FilmFieldChange.id)
    )
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return list(result.scalars().all())


async def _follow_franchise(session, user, collection_id: int) -> None:
    session.add(
        Follow(
            user_id=user.id,
            entity_type="franchise",
            entity_id=str(collection_id),
            source="manual",
        )
    )
    await session.commit()


async def test_a_film_inserted_into_a_followed_franchise_writes_the_row(session, make_user):
    """EF-4 for collections, and the whole reason this module exists: without it the most
    common way a film joins a franchise is the one way that never cards."""
    user = await make_user(email="follower@example.com")
    await _follow_franchise(session, user, DUNE["id"])

    await upsert_film(session, _details(9200, DUNE))
    await session.commit()

    (change,) = await _collection_changes(session, 9200)
    assert change.old_value is None
    assert change.new_value == DUNE["id"]
    assert change.changed_at is not None


async def test_a_film_inserted_into_an_unfollowed_franchise_writes_nothing(session):
    """Baseline, as every other field on a new film is."""
    await upsert_film(session, _details(9201, DUNE))
    await session.commit()

    assert await _collection_changes(session, 9201) == []


async def test_a_film_inserted_outside_every_franchise_writes_nothing(session, make_user):
    """There is nothing to record, whoever follows what."""
    user = await make_user(email="follower@example.com")
    await _follow_franchise(session, user, DUNE["id"])

    await upsert_film(session, _details(9202))
    await session.commit()

    assert await _collection_changes(session, 9202) == []


async def test_a_film_updated_into_a_followed_franchise_writes_exactly_one_row(session, make_user):
    """The trigger's row, and only the trigger's. A film already in the catalog joining a
    followed franchise is an `UPDATE`, which the trigger has always recorded — writing a
    second row here would card the same beat twice."""
    user = await make_user(email="follower@example.com")
    await _follow_franchise(session, user, DUNE["id"])

    await upsert_film(session, _details(9203))
    await session.commit()
    assert await _collection_changes(session, 9203) == []

    await upsert_film(session, _details(9203, DUNE))
    await session.commit()

    (change,) = await _collection_changes(session, 9203)
    assert (change.old_value, change.new_value) == (None, DUNE["id"])


async def test_re_ingesting_an_admitted_film_writes_no_second_row(session, make_user):
    """`film_inserted` is the pre-select's answer, not a marker that could drift: the second
    ingest updates the row, the collection is unchanged, and the trigger writes nothing."""
    user = await make_user(email="follower@example.com")
    await _follow_franchise(session, user, DUNE["id"])

    await upsert_film(session, _details(9204, DUNE))
    await session.commit()
    await upsert_film(session, _details(9204, DUNE))
    await session.commit()

    assert len(await _collection_changes(session, 9204)) == 1


async def test_a_film_inserted_into_a_franchise_someone_else_follows_writes_the_row(
    session, make_user
):
    """The set is system-wide and asks nothing about who is looking, exactly as the person and
    company sets do."""
    other = await make_user(email="other@example.com")
    await _follow_franchise(session, other, ALIEN["id"])

    await upsert_film(session, _details(9205, ALIEN))
    await session.commit()

    (change,) = await _collection_changes(session, 9205)
    assert change.new_value == ALIEN["id"]


async def test_a_company_follow_at_the_same_id_does_not_admit_a_franchise(session, make_user):
    """The `entity_type` filter, proved rather than assumed: a company id is as numeric as a
    collection id."""
    user = await make_user(email="follower@example.com")
    session.add(
        Follow(
            user_id=user.id,
            entity_type="company",
            entity_id=str(DUNE["id"]),
            source="manual",
        )
    )
    await session.commit()

    await upsert_film(session, _details(9206, DUNE))
    await session.commit()

    assert await _collection_changes(session, 9206) == []
