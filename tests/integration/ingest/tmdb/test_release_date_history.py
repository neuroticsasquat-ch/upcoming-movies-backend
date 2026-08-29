"""`catalog.film_release_date_change` written through a real `upsert_film` round trip.

The unit tests cover the diff in isolation; these cover what can actually go wrong in
production — that the rule survives `_rebuild_release_dates`, which unconditionally destroys
and recreates every release row on every ingest (NEU-1121).
"""

from datetime import date

from sqlalchemy import select

from tests.fixtures.tmdb import make_details
from upmovies.catalog.models import Film, FilmFieldChange, FilmReleaseDateChange
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails
from upmovies.ingest.tmdb.upsert import upsert_film


def _release(iso: str, rtype: int, when: str) -> dict:
    return {
        "iso_3166_1": iso,
        "release_dates": [{"type": rtype, "release_date": f"{when}T00:00:00.000Z"}],
    }


def _details(tmdb_id: int, *, releases: list[dict], origin: list[str] | None = None):
    payload = make_details(tmdb_id, release_dates={"results": releases})
    payload["origin_country"] = origin if origin is not None else ["US"]
    return TMDBMovieDetails.model_validate(payload)


async def _changes(session, tmdb_id: int) -> list[FilmReleaseDateChange]:
    stmt = (
        select(FilmReleaseDateChange)
        .join(Film, Film.id == FilmReleaseDateChange.film_id)
        .where(Film.tmdb_id == tmdb_id)
        .order_by(FilmReleaseDateChange.id)
    )
    result = await session.execute(stmt, execution_options={"populate_existing": True})
    return list(result.scalars().all())


async def _observed_at(session, tmdb_id: int):
    stmt = select(Film.release_dates_observed_at).where(Film.tmdb_id == tmdb_id)
    return (await session.execute(stmt)).scalar_one()


async def test_first_ingest_writes_no_change_rows(session):
    """The headline test (spec §5.3). Admitting a film records its whole release slate as a
    baseline, however many displayable dates it arrives with — otherwise the first sweep after
    deploy would card the entire catalog's release list."""
    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    assert await _changes(session, 1) == []
    assert await _observed_at(session, 1) is not None


async def test_a_date_arriving_after_the_baseline_is_a_change(session):
    await upsert_film(session, _details(1, releases=[]))
    await session.commit()

    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    (change,) = await _changes(session, 1)
    assert (change.iso_3166_1, change.release_type, change.change) == ("US", 3, "set")
    assert change.new_date == date(2027, 12, 17)
    assert change.previous_date is None


async def test_a_moved_date_records_both_sides(session):
    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    await upsert_film(session, _details(1, releases=[_release("US", 3, "2028-01-15")]))
    await session.commit()

    (change,) = await _changes(session, 1)
    assert change.change == "moved"
    assert change.previous_date == date(2027, 12, 17)
    assert change.new_date == date(2028, 1, 15)


async def test_an_unchanged_slate_records_nothing_however_often_it_is_reingested(session):
    """The rebuild deletes and reinserts every row every run, so a diff phrased in terms of
    what it *did* would see every date as removed-then-added on every ingest."""
    for _ in range(3):
        await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
        await session.commit()

    assert await _changes(session, 1) == []


async def test_a_foreign_theatrical_date_is_never_recorded(session):
    """Cliffhanger: origin US, only release row is German. The page shows no release dates at
    all, so there is nothing for a card to be about."""
    await upsert_film(session, _details(1, releases=[], origin=["US"]))
    await session.commit()

    await upsert_film(
        session, _details(1, releases=[_release("DE", 3, "2027-04-29")], origin=["US"])
    )
    await session.commit()

    assert await _changes(session, 1) == []


async def test_a_non_theatrical_date_is_never_recorded(session):
    """Premiere/digital/physical/TV are dropped by the page, so they raise no events either —
    the primary date being one of these is how the old path cited invisible dates."""
    await upsert_film(session, _details(1, releases=[]))
    await session.commit()

    await upsert_film(session, _details(1, releases=[_release("US", 4, "2027-12-17")]))
    await session.commit()

    assert await _changes(session, 1) == []


async def test_every_origin_country_counts_not_just_the_first(session):
    """The drift NEU-1121 closes: the page read `origin_country[0]`, the event-visibility
    predicate read all of them. One definition now, and it is the wider one."""
    await upsert_film(session, _details(1, releases=[], origin=["GB", "FR"]))
    await session.commit()

    await upsert_film(
        session, _details(1, releases=[_release("FR", 3, "2027-12-17")], origin=["GB", "FR"])
    )
    await session.commit()

    (change,) = await _changes(session, 1)
    assert change.iso_3166_1 == "FR"


async def test_limited_and_wide_are_recorded_as_separate_subjects(session):
    await upsert_film(
        session,
        _details(1, releases=[_release("US", 2, "2027-12-10"), _release("US", 3, "2027-12-17")]),
    )
    await session.commit()

    await upsert_film(
        session,
        _details(1, releases=[_release("US", 2, "2027-12-11"), _release("US", 3, "2027-12-17")]),
    )
    await session.commit()

    (change,) = await _changes(session, 1)
    assert change.release_type == 2
    assert change.new_date == date(2027, 12, 11)


async def test_a_withdrawn_date_is_not_a_change(session):
    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    await upsert_film(session, _details(1, releases=[]))
    await session.commit()

    assert await _changes(session, 1) == []


async def test_the_observation_marker_is_written_once_and_never_reset(session):
    await upsert_film(session, _details(1, releases=[]))
    await session.commit()
    first = await _observed_at(session, 1)

    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    assert await _observed_at(session, 1) == first


async def test_the_marker_is_not_recorded_as_a_field_change(session):
    """`release_dates_observed_at` is ingest bookkeeping, so it is on the trigger's denylist.
    Without that, its own write lands a `film_field_change` row on every film's first refresh —
    which would both flood that table and make every film look active to `dormant_film_clause`,
    so dormancy would stop governing anything."""
    await upsert_film(session, _details(1, releases=[]))
    await session.commit()
    await upsert_film(session, _details(1, releases=[_release("US", 3, "2027-12-17")]))
    await session.commit()

    stmt = (
        select(FilmFieldChange.field)
        .join(Film, Film.id == FilmFieldChange.film_id)
        .where(Film.tmdb_id == 1)
    )
    assert "release_dates_observed_at" not in set((await session.execute(stmt)).scalars().all())


async def test_governing_date_move_records_governing_movement_not_moved_row(session):
    """NEU-1206: when a non-governing row moves earlier and becomes the governing date,
    the stored change cites the governing date's movement, not the moved row's previous value.
    """
    # Baseline: governing date is 15 Dec, with a later sibling at 20 Dec.
    await upsert_film(
        session,
        _details(
            1,
            releases=[
                _release("US", 3, "2027-12-15"),
                _release("US", 3, "2027-12-20"),
            ],
        ),
    )
    await session.commit()

    # The 20 Dec row moves to 10 Dec, becoming the new governing date.
    await upsert_film(
        session,
        _details(
            1,
            releases=[
                _release("US", 3, "2027-12-15"),
                _release("US", 3, "2027-12-10"),
            ],
        ),
    )
    await session.commit()

    (change,) = await _changes(session, 1)
    assert change.change == "moved"
    assert change.previous_date == date(2027, 12, 15)
    assert change.new_date == date(2027, 12, 10)
