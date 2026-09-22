"""`catalog.film_credit_change` written through a real `upsert_film` round trip.

The unit tests cover the diff in isolation; these cover the thing that can actually go wrong
in production — that the rule survives the delete-and-reinsert rebuild, which unconditionally
destroys and recreates every credit row on every ingest.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow
from upmovies.catalog.models import Film, FilmCreditChange, FilmFieldChange
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film


def _cast(person_id: int, order: int) -> dict:
    return {
        "id": person_id,
        "name": f"person-{person_id}",
        "credit_id": f"cast-{person_id}",
        "character": "Someone",
        "order": order,
    }


def _crew(person_id: int, job: str, department: str = "Directing") -> dict:
    return {
        "id": person_id,
        "name": f"person-{person_id}",
        "credit_id": f"crew-{person_id}-{job}",
        "job": job,
        "department": department,
    }


def _details(tmdb_id: int, *, cast: list[dict] | None = None, crew: list[dict] | None = None):
    return TMDBMovieDetails.model_validate(
        make_details(tmdb_id, credits={"cast": cast or [], "crew": crew or []})
    )


async def _changes(session, tmdb_id: int) -> list[FilmCreditChange]:
    stmt = (
        select(FilmCreditChange)
        .join(Film, Film.id == FilmCreditChange.film_id)
        .where(Film.tmdb_id == tmdb_id)
        .order_by(FilmCreditChange.id)
    )
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return list(result.scalars().all())


async def _observed_at(session, tmdb_id: int):
    stmt = select(Film.credits_observed_at).where(Film.tmdb_id == tmdb_id)
    return (await session.execute(stmt)).scalar_one()


async def test_first_credit_ingest_writes_no_change_rows(session):
    """The headline test (ADR-0014, spec §5.3). Admitting a film records its credits as a
    baseline: no attachment rows, however many seed-grade credits it arrives with."""
    await upsert_film(
        session,
        _details(9001, cast=[_cast(1, 0), _cast(2, 1)], crew=[_crew(10, "Director")]),
    )
    await session.commit()

    assert await _changes(session, 9001) == []


async def test_second_ingest_adding_a_director_writes_one_added_row(session):
    await upsert_film(session, _details(9002, cast=[_cast(1, 0)]))
    await session.commit()
    await upsert_film(session, _details(9002, cast=[_cast(1, 0)], crew=[_crew(10, "Director")]))
    await session.commit()

    changes = await _changes(session, 9002)
    assert len(changes) == 1
    assert (changes[0].person_id, changes[0].credit_type, changes[0].job, changes[0].change) == (
        10,
        "crew",
        "Director",
        "added",
    )


async def test_reingesting_identical_credits_writes_nothing(session):
    """The subtle one: `_upsert_credits` deletes and reinserts unconditionally, so a diff
    phrased over the rebuild's writes rather than over set membership would see every credit
    as removed-then-added on every run."""
    details = _details(9003, cast=[_cast(1, 0), _cast(2, 3)], crew=[_crew(10, "Director")])
    await upsert_film(session, details)
    await session.commit()
    await upsert_film(session, details)
    await session.commit()
    await upsert_film(session, details)
    await session.commit()

    assert await _changes(session, 9003) == []


async def test_a_non_seed_grade_credit_appearing_writes_nothing(session):
    """A 40th-billed extra and a gaffer are not seed grade, so TMDB's churn on them stays out
    of the history entirely."""
    await upsert_film(session, _details(9004, cast=[_cast(1, 0)]))
    await session.commit()
    await upsert_film(
        session,
        _details(
            9004,
            cast=[_cast(1, 0), _cast(2, 40)],
            crew=[_crew(11, "Gaffer", "Lighting"), _crew(12, "Executive Producer", "Production")],
        ),
    )
    await session.commit()

    assert await _changes(session, 9004) == []


async def test_a_dropped_director_writes_a_removed_row(session):
    await upsert_film(session, _details(9005, cast=[_cast(1, 0)], crew=[_crew(10, "Director")]))
    await session.commit()
    await upsert_film(session, _details(9005, cast=[_cast(1, 0)]))
    await session.commit()

    changes = await _changes(session, 9005)
    assert len(changes) == 1
    assert (changes[0].person_id, changes[0].job, changes[0].change) == (10, "Director", "removed")


async def test_a_film_first_seen_with_only_non_seed_credits_is_still_observed(session):
    """`previous is None` means *no credits at all*, not *no seed-grade credits*. A film first
    seen holding only a gaffer has been observed, so the director arriving next run is a real
    attachment and must not be swallowed as a baseline."""
    await upsert_film(session, _details(9006, crew=[_crew(11, "Gaffer", "Lighting")]))
    await session.commit()
    await upsert_film(
        session,
        _details(9006, crew=[_crew(11, "Gaffer", "Lighting"), _crew(10, "Director")]),
    )
    await session.commit()

    changes = await _changes(session, 9006)
    assert len(changes) == 1
    assert (changes[0].person_id, changes[0].change) == (10, "added")


async def test_a_writer_director_losing_the_writing_credit_writes_one_row(session):
    """Identity is (person, credit_type, job): the same person holds two seed-grade crew
    credits, and losing one of them is a change while the other is untouched."""
    await upsert_film(
        session,
        _details(9007, crew=[_crew(10, "Director"), _crew(10, "Screenplay", "Writing")]),
    )
    await session.commit()
    await upsert_film(session, _details(9007, crew=[_crew(10, "Director")]))
    await session.commit()

    changes = await _changes(session, 9007)
    assert len(changes) == 1
    assert (changes[0].person_id, changes[0].job, changes[0].change) == (
        10,
        "Screenplay",
        "removed",
    )


async def test_a_film_admitted_with_no_credits_at_all_is_still_observed(session):
    """The case that defeats a row-count baseline signal. A speculative TMDB entry admitted
    with an empty credits payload holds zero credit rows, so "holds no credits" would read as
    "never observed" and make it baseline a second time — swallowing the first director to
    attach, which is the most valuable event the credit half exists to raise."""
    await upsert_film(session, _details(9009))
    await session.commit()
    await upsert_film(session, _details(9009, crew=[_crew(10, "Director")]))
    await session.commit()

    changes = await _changes(session, 9009)
    assert len(changes) == 1
    assert (changes[0].person_id, changes[0].job, changes[0].change) == (10, "Director", "added")


async def test_observing_credits_writes_no_film_field_change_row(session):
    """`credits_observed_at` is ingest bookkeeping, so it is on the trigger's denylist: a
    history row for it would make a film look active to `dormant_film_clause` on the very day
    it was admitted."""
    await upsert_film(session, _details(9010, crew=[_crew(10, "Director")]))
    await session.commit()

    stmt = (
        select(FilmFieldChange.field)
        .join(Film, Film.id == FilmFieldChange.film_id)
        .where(Film.tmdb_id == 9010)
    )
    assert "credits_observed_at" not in set((await session.execute(stmt)).scalars().all())


async def test_the_observation_marker_is_written_once_and_never_reset(session):
    """A later ingest must not be able to re-baseline a film by resetting the marker."""
    await upsert_film(session, _details(9011, crew=[_crew(10, "Director")]))
    await session.commit()
    first = await _observed_at(session, 9011)
    assert first is not None

    await upsert_film(session, _details(9011, crew=[_crew(10, "Director"), _crew(11, "Writer")]))
    await session.commit()
    assert await _observed_at(session, 9011) == first


async def test_a_cast_member_falling_out_of_the_top_five_is_recorded(session):
    """Pinning a boundary the spec leaves open rather than leaving it to be discovered. Seed
    grade for cast *is* top-5 billing (§3.2), so a 5th-billed actor demoted to 6th leaves the
    seed set and that is what the history records. Whether such a pair should ever card is
    NEU-1083's call — this test exists so that call is made against a known behaviour."""
    await upsert_film(session, _details(9012, cast=[_cast(1, 0), _cast(2, 4)]))
    await session.commit()
    await upsert_film(session, _details(9012, cast=[_cast(1, 0), _cast(2, 5)]))
    await session.commit()

    changes = await _changes(session, 9012)
    assert [(c.person_id, c.change) for c in changes] == [(2, "removed")]


async def test_history_accumulates_across_ingests(session):
    """Append-only: an attachment and a later detachment both stay on the record."""
    await upsert_film(session, _details(9008, cast=[_cast(1, 0)]))
    await session.commit()
    await upsert_film(session, _details(9008, cast=[_cast(1, 0)], crew=[_crew(10, "Director")]))
    await session.commit()
    await upsert_film(session, _details(9008, cast=[_cast(1, 0)]))
    await session.commit()

    changes = await _changes(session, 9008)
    assert [c.change for c in changes] == ["added", "removed"]
    assert all(c.changed_at is not None for c in changes)


# --- admission is an attachment for a followed person (EF-4, NEU-1436) -----------------------


async def _follow_person(session, user, person_id: int) -> None:
    session.add(
        Follow(user_id=user.id, entity_type="person", entity_id=str(person_id), source="manual")
    )
    await session.commit()


async def test_admitting_a_film_with_a_followed_director_writes_an_added_row(session, make_user):
    """EF-4's headline. The baseline rule would swallow the one attachment the follow was made
    for: the followed director's *next* film, entering the catalog with them already on it."""
    user = await make_user(email="follower@example.com")
    await _follow_person(session, user, 10)

    await upsert_film(
        session,
        _details(9020, cast=[_cast(1, 0), _cast(2, 1)], crew=[_crew(10, "Director")]),
    )
    await session.commit()

    changes = await _changes(session, 9020)
    assert [(c.person_id, c.credit_type, c.job, c.change) for c in changes] == [
        (10, "crew", "Director", "added")
    ]
    assert changes[0].changed_at is not None
    assert await _observed_at(session, 9020) is not None


async def test_admitting_the_same_film_with_nobody_following_writes_nothing(session):
    """The same payload, no follows: still a baseline. The exception is keyed on the follow
    graph and nothing else."""
    await upsert_film(
        session,
        _details(9021, cast=[_cast(1, 0), _cast(2, 1)], crew=[_crew(10, "Director")]),
    )
    await session.commit()

    assert await _changes(session, 9021) == []


async def test_admission_writes_one_row_per_job_for_a_followed_writer_director(session, make_user):
    """Two recorded credits, so two rows. They card once — `crew_attached` groups per film,
    type and pass (D-7)."""
    user = await make_user(email="follower@example.com")
    await _follow_person(session, user, 10)

    await upsert_film(
        session,
        _details(9022, crew=[_crew(10, "Director"), _crew(10, "Screenplay", "Writing")]),
    )
    await session.commit()

    changes = await _changes(session, 9022)
    assert sorted((c.job, c.change) for c in changes) == [
        ("Director", "added"),
        ("Screenplay", "added"),
    ]


async def test_admission_writes_a_followed_persons_non_seed_credit(session, make_user):
    """Recorded grade, not seed grade (D-49): a followed gaffer's credit is written down on
    admission exactly as a followed director's is, while the unfollowed gaffer beside them
    stays baseline."""
    user = await make_user(email="follower@example.com")
    await _follow_person(session, user, 13)

    await upsert_film(
        session,
        _details(
            9023,
            crew=[_crew(13, "Gaffer", "Lighting"), _crew(14, "Best Boy", "Lighting")],
        ),
    )
    await session.commit()

    changes = await _changes(session, 9023)
    assert [(c.person_id, c.job, c.change) for c in changes] == [(13, "Gaffer", "added")]


async def test_a_follow_created_after_admission_fabricates_nothing(session, make_user):
    """D-49, through the admission path. The film was admitted while nobody followed anyone,
    so it baselined. The follow is made, and the very next ingest brings the identical
    credits: both sides of the diff are judged by the same followed set at the same moment, so
    the credit that never moved is in `previous` and in `current` alike and nothing is
    written. Without that, the user would be told a director "attached" to a film they had
    been on all along."""
    details = _details(9024, cast=[_cast(1, 0)], crew=[_crew(10, "Director")])
    await upsert_film(session, details)
    await session.commit()
    assert await _changes(session, 9024) == []

    user = await make_user(email="follower@example.com")
    await _follow_person(session, user, 10)

    await upsert_film(session, details)
    await session.commit()

    assert await _changes(session, 9024) == []


async def test_a_film_admitted_with_no_credits_baselines_then_cards_the_followed_arrival(
    session, make_user
):
    """The observed-marker rule is untouched by the exception. A speculative entry admitted
    with an empty credits payload has never been observed, so nothing is written for it — and
    the followed director arriving next ingest is an ordinary diff `added`, not an admission
    one."""
    user = await make_user(email="follower@example.com")
    await _follow_person(session, user, 10)

    await upsert_film(session, _details(9025))
    await session.commit()
    assert await _changes(session, 9025) == []

    await upsert_film(session, _details(9025, crew=[_crew(10, "Director")]))
    await session.commit()

    changes = await _changes(session, 9025)
    assert [(c.person_id, c.change) for c in changes] == [(10, "added")]


async def test_a_lapsed_users_follow_still_records_an_admission(session, make_user):
    """No entitlement filter anywhere on this path (D-1436.5). The row costs nothing and is
    exactly what should already be there when they come back — and the observation cannot be
    backfilled once it is gone."""
    lapsed = await make_user(
        email="lapsed@example.com", entitled_until=datetime.now(UTC) - timedelta(days=30)
    )
    await _follow_person(session, lapsed, 10)

    await upsert_film(session, _details(9026, crew=[_crew(10, "Director")]))
    await session.commit()

    assert [c.change for c in await _changes(session, 9026)] == ["added"]
