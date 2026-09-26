"""`catalog.film_company_change` written through a real `upsert_film` round trip (EF-5).

The unit tests cover the diff in isolation; these cover the thing that can actually go wrong
in production — that the rule survives the delete-and-reinsert rebuild in `_rebuild_joins`,
which unconditionally destroys and recreates every company row on every ingest.
"""

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow
from upmovies.catalog.models import Film, FilmCompanyChange, FilmFieldChange, FilmProductionCompany
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film


def _details(tmdb_id: int, company_ids: list[int]) -> TMDBMovieDetails:
    return TMDBMovieDetails.model_validate(
        make_details(
            tmdb_id,
            production_companies=[
                {"id": company_id, "name": f"Studio {company_id}"} for company_id in company_ids
            ],
        )
    )


async def _changes(session, tmdb_id: int) -> list[FilmCompanyChange]:
    stmt = (
        select(FilmCompanyChange)
        .join(Film, Film.id == FilmCompanyChange.film_id)
        .where(Film.tmdb_id == tmdb_id)
        .order_by(FilmCompanyChange.id)
    )
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return list(result.scalars().all())


async def _observed_at(session, tmdb_id: int):
    stmt = select(Film.companies_observed_at).where(Film.tmdb_id == tmdb_id)
    return (await session.execute(stmt)).scalar_one()


async def _live_companies(session, tmdb_id: int) -> set[int]:
    stmt = (
        select(FilmProductionCompany.company_id)
        .join(Film, Film.id == FilmProductionCompany.film_id)
        .where(Film.tmdb_id == tmdb_id)
    )
    return set((await session.execute(stmt)).scalars().all())


async def test_first_company_ingest_writes_no_change_rows(session):
    """The headline test (ADR-0014, spec §5.3). Admitting a film records its companies as a
    baseline: no attachment rows, however many studios it arrives with."""
    await upsert_film(session, _details(9101, [1, 2]))
    await session.commit()

    assert await _changes(session, 9101) == []
    assert await _live_companies(session, 9101) == {1, 2}


async def test_the_first_ingest_stamps_the_observed_marker(session):
    await upsert_film(session, _details(9102, [1]))
    await session.commit()

    assert await _observed_at(session, 9102) is not None


async def test_the_marker_is_write_once(session):
    await upsert_film(session, _details(9103, [1]))
    await session.commit()
    first = await _observed_at(session, 9103)

    await upsert_film(session, _details(9103, [1, 2]))
    await session.commit()

    assert await _observed_at(session, 9103) == first


async def test_a_studio_arriving_on_a_later_ingest_writes_one_added_row(session):
    await upsert_film(session, _details(9104, [1]))
    await session.commit()
    await upsert_film(session, _details(9104, [1, 2]))
    await session.commit()

    changes = await _changes(session, 9104)
    assert [(c.company_id, c.change) for c in changes] == [(2, "added")]


async def test_a_studio_leaving_writes_one_removed_row(session):
    await upsert_film(session, _details(9105, [1, 2]))
    await session.commit()
    await upsert_film(session, _details(9105, [1]))
    await session.commit()

    changes = await _changes(session, 9105)
    assert [(c.company_id, c.change) for c in changes] == [(2, "removed")]


async def test_add_remove_and_re_add_writes_three_rows(session):
    """The sequence the rebuild would flatten: `film_production_company` holds the same one
    row at the start and at the end, and only the history says anything happened."""
    await upsert_film(session, _details(9106, [1]))
    await session.commit()
    await upsert_film(session, _details(9106, [1, 2]))
    await session.commit()
    await upsert_film(session, _details(9106, [1]))
    await session.commit()
    await upsert_film(session, _details(9106, [1, 2]))
    await session.commit()

    changes = await _changes(session, 9106)
    assert [(c.company_id, c.change) for c in changes] == [
        (2, "added"),
        (2, "removed"),
        (2, "added"),
    ]
    assert await _live_companies(session, 9106) == {1, 2}


async def test_an_unchanged_company_set_writes_nothing_however_often_it_is_rebuilt(session):
    await upsert_film(session, _details(9107, [1, 2]))
    await session.commit()
    await upsert_film(session, _details(9107, [2, 1]))
    await session.commit()
    await upsert_film(session, _details(9107, [1, 2]))
    await session.commit()

    assert await _changes(session, 9107) == []


async def test_a_film_admitted_with_no_companies_is_observed_not_unobserved(session):
    """The case the marker exists for: a speculative entry arrives with an empty company
    list, and the first studio to attach must card rather than be swallowed as a second
    baseline."""
    await upsert_film(session, _details(9108, []))
    await session.commit()
    await upsert_film(session, _details(9108, [3]))
    await session.commit()

    changes = await _changes(session, 9108)
    assert [(c.company_id, c.change) for c in changes] == [(3, "added")]


async def test_the_marker_writes_no_film_field_change_row(session):
    """`companies_observed_at` is in `FILM_FIELD_CHANGE_DENYLIST`: it is ingest bookkeeping,
    and a history row for it would card as a public event and revive the film in
    `dormant_film_clause` on the day it was admitted."""
    await upsert_film(session, _details(9109, [1]))
    await session.commit()
    await upsert_film(session, _details(9109, [1, 2]))
    await session.commit()

    fields = (
        (
            await session.execute(
                select(FilmFieldChange.field)
                .join(Film, Film.id == FilmFieldChange.film_id)
                .where(Film.tmdb_id == 9109)
            )
        )
        .scalars()
        .all()
    )
    assert "companies_observed_at" not in fields


# --- admission is an attachment for a followed studio (EF-4, NEU-1436) -----------------------


async def _follow_company(session, user, company_id: int) -> None:
    session.add(
        Follow(user_id=user.id, entity_type="company", entity_id=str(company_id), source="manual")
    )
    await session.commit()


async def test_admitting_a_film_with_a_followed_studio_writes_an_added_row(session, make_user):
    """EF-4 for companies. The followed studio's next production enters the catalog already
    carrying it, and the baseline rule would swallow exactly that."""
    user = await make_user(email="follower@example.com")
    await _follow_company(session, user, 2)

    await upsert_film(session, _details(9120, [1, 2, 3]))
    await session.commit()

    changes = await _changes(session, 9120)
    assert [(c.company_id, c.change) for c in changes] == [(2, "added")]
    assert changes[0].changed_at is not None
    assert await _observed_at(session, 9120) is not None
    assert await _live_companies(session, 9120) == {1, 2, 3}


async def test_admitting_the_same_film_with_nobody_following_writes_nothing(session):
    await upsert_film(session, _details(9121, [1, 2, 3]))
    await session.commit()

    assert await _changes(session, 9121) == []


async def test_a_company_follow_created_after_admission_fabricates_nothing(session, make_user):
    """The film baselined while nobody followed the studio. The follow is made and the next
    ingest brings the identical company set: an ordinary diff over the stored rows, so there
    is nothing to write. A studio that never moved must not be announced as joining."""
    await upsert_film(session, _details(9122, [1, 2]))
    await session.commit()
    assert await _changes(session, 9122) == []

    user = await make_user(email="follower@example.com")
    await _follow_company(session, user, 2)

    await upsert_film(session, _details(9122, [1, 2]))
    await session.commit()

    assert await _changes(session, 9122) == []


async def test_a_person_follow_at_the_same_id_does_not_admit_a_company(session, make_user):
    """The `entity_type` filter, proved rather than assumed: a person id is as numeric as a
    company id, so a builder that lost the filter would read one as the other."""
    user = await make_user(email="follower@example.com")
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="2", source="manual"))
    await session.commit()

    await upsert_film(session, _details(9123, [1, 2]))
    await session.commit()

    assert await _changes(session, 9123) == []
