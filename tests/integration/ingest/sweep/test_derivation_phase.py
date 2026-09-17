"""The sweep's derived-watchlist phase: D-13's maintenance half, run over every entitled user
once the credits pass has written the day's new credits.

The phase itself holds no rule about *which* film qualifies — that is
`app.services.derivation_service`, covered in `tests/integration/app/test_derivation_service.py`.
What is only true here is who it runs for: entitled users with follows, and nobody else (D-39).
A film that qualified for the first time because of a credit this very sweep recorded is the
case the phase exists for, so it is the one asserted end to end.
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.app.models import Follow, WatchlistDismissal, WatchlistItem
from upmovies.catalog.models import FilmCredit, Person
from upmovies.ingest.models import IngestRun
from upmovies.ingest.sweep import run_watchlist_derivation
from upmovies.ingest.sweep.derivation_phase import load_derivation_user_ids

TODAY = date(2026, 9, 17)
IN_PLAY = date(2026, 12, 25)
EXCLUDED = frozenset({"Released", "Canceled"})
ENTITLED_UNTIL = datetime(2099, 1, 1, tzinfo=UTC)


async def _run(session_factory, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "run_id": run_id,
        "today": TODAY,
        "excluded_statuses": EXCLUDED,
    }
    return await run_watchlist_derivation(**{**kwargs, **overrides})


@pytest.fixture
async def entitled_user(make_user):
    return await make_user(email="entitled@example.com", entitled_until=ENTITLED_UNTIL)


async def _direct(session, film, person_id: int = 525) -> None:
    """A director credit, creating the person if this is their first."""
    if await session.get(Person, person_id) is None:
        session.add(Person(id=person_id, name=f"Director {person_id}"))
        await session.flush()
    session.add(
        FilmCredit(
            credit_id=f"c-{film.tmdb_id}-{person_id}",
            film_id=film.id,
            person_id=person_id,
            credit_type="crew",
            job="Director",
            department="Directing",
        )
    )
    await session.flush()


async def _film_ids(session, user) -> set:
    rows = await session.execute(
        select(WatchlistItem.film_id).where(WatchlistItem.user_id == user.id)
    )
    return set(rows.scalars().all())


async def test_a_newly_credited_film_is_derived_for_a_follower(
    session, session_factory, run_id, entitled_user
):
    """The phase's reason to exist: the follow was created yesterday, the credit arrived on this
    sweep, and no request will ever run for this user."""
    film = await add_film(session, tmdb_id=600, release_date=IN_PLAY)
    session.add(
        Follow(user_id=entitled_user.id, entity_type="person", entity_id="525", source="manual")
    )
    await _direct(session, film)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.users_considered == 1
    assert result.items_created == 1
    assert result.failures == 0
    assert not result.aborted
    assert await _film_ids(session, entitled_user) == {film.id}


async def test_an_unentitled_user_with_follows_gains_nothing(
    session, session_factory, run_id, make_user
):
    """D-39's batch checkpoint. The follow graph is intact and the film qualifies; the only
    thing missing is the grant, and deriving rows the account cannot see would be work spent on
    every credits pass forever."""
    unentitled = await make_user(email="nobody@example.com")
    film = await add_film(session, tmdb_id=601, release_date=IN_PLAY)
    session.add(
        Follow(user_id=unentitled.id, entity_type="person", entity_id="525", source="manual")
    )
    await _direct(session, film)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.users_considered == 0
    assert result.items_created == 0
    assert await _film_ids(session, unentitled) == set()


async def test_a_lapsed_grant_stops_maintenance_and_keeps_the_rows(
    session, session_factory, run_id, make_user
):
    """D-40: expiry suppresses, never destroys. The item derived while the grant was live is
    still there; the film that qualified after it lapsed is not added."""
    lapsed = await make_user(
        email="lapsed@example.com", entitled_until=datetime(2020, 1, 1, tzinfo=UTC)
    )
    kept = await add_film(session, tmdb_id=602, release_date=IN_PLAY)
    later = await add_film(session, tmdb_id=603, release_date=IN_PLAY)
    session.add(WatchlistItem(user_id=lapsed.id, film_id=kept.id, source="derived_from_follow"))
    session.add(Follow(user_id=lapsed.id, entity_type="person", entity_id="525", source="manual"))
    await _direct(session, later)
    await session.commit()

    await _run(session_factory, run_id)

    assert await _film_ids(session, lapsed) == {kept.id}


async def test_a_user_with_no_follows_is_not_considered(
    session, session_factory, run_id, entitled_user
):
    """An entitled account that has followed nothing has nothing to derive from, and selecting
    it would cost one statement per signup on every sweep."""
    await add_film(session, tmdb_id=604, release_date=IN_PLAY)
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.users_considered == 0
    assert result.items_created == 0


async def test_a_dismissal_survives_the_sweep(session, session_factory, run_id, entitled_user):
    """The dismissal is permanent (D-13), and the pass that would otherwise put the film back
    every single day is exactly the one it has to bind."""
    film = await add_film(session, tmdb_id=605, release_date=IN_PLAY)
    session.add(WatchlistDismissal(user_id=entitled_user.id, film_id=film.id))
    session.add(
        Follow(
            user_id=entitled_user.id,
            entity_type="title",
            entity_id=str(film.id),
            source="manual",
        )
    )
    await session.commit()

    result = await _run(session_factory, run_id)

    assert result.users_considered == 1
    assert result.items_created == 0
    assert await _film_ids(session, entitled_user) == set()


async def test_the_phase_is_idempotent_across_sweeps(
    session, session_factory, run_id, entitled_user
):
    film = await add_film(session, tmdb_id=606, release_date=IN_PLAY)
    session.add(
        Follow(
            user_id=entitled_user.id,
            entity_type="title",
            entity_id=str(film.id),
            source="manual",
        )
    )
    await session.commit()

    first = await _run(session_factory, run_id)
    second = await _run(session_factory, run_id)

    assert (first.items_created, second.items_created) == (1, 0)
    assert await _film_ids(session, entitled_user) == {film.id}


async def test_one_users_failure_does_not_cost_the_others(
    session, session_factory, run_id, make_user, monkeypatch
):
    """The pipeline contract: a session per user, so a failure mid-pass leaves the users either
    side of it derived."""
    first = await make_user(email="a@example.com", entitled_until=ENTITLED_UNTIL)
    second = await make_user(email="b@example.com", entitled_until=ENTITLED_UNTIL)
    film = await add_film(session, tmdb_id=607, release_date=IN_PLAY)
    for user in (first, second):
        session.add(
            Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
        )
    await session.commit()

    from upmovies.ingest.sweep import derivation_phase

    real = derivation_phase.derive_for_user
    calls = {"n": 0}

    async def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated derivation failure")
        return await real(*args, **kwargs)

    monkeypatch.setattr(derivation_phase, "derive_for_user", flaky)

    result = await _run(session_factory, run_id)

    assert result.failures == 1
    assert result.items_created == 1
    assert not result.aborted
    derived = (await session.execute(select(WatchlistItem.user_id))).scalars().all()
    assert len(derived) == 1


async def test_the_phase_aborts_after_consecutive_failures(
    session, session_factory, run_id, make_user, monkeypatch
):
    """The same guard every sweep phase carries: an outage stops the pass rather than burning a
    statement per user, and the run reports what stopped it."""
    for i in range(3):
        user = await make_user(email=f"u{i}@example.com", entitled_until=ENTITLED_UNTIL)
        session.add(
            Follow(user_id=user.id, entity_type="title", entity_id=str(uuid4()), source="manual")
        )
    await session.commit()

    from upmovies.ingest.sweep import derivation_phase

    async def boom(*args, **kwargs):
        raise RuntimeError("simulated outage")

    monkeypatch.setattr(derivation_phase, "derive_for_user", boom)

    result = await _run(session_factory, run_id, failure_threshold=2)

    assert result.aborted
    assert result.abort_error is not None
    assert result.failures == 2
    row = await session.get(IngestRun, run_id, execution_options={"populate_existing": True})
    assert row is not None and row.items_failed == 2


async def test_the_pass_records_progress_against_the_run(
    session, session_factory, run_id, entitled_user
):
    """`last_progress_at` is what `mark_stale_runs_cancelled` reads, and this phase runs last —
    a long pass that never touched the column would look like an orphan (NEU-1117)."""
    film = await add_film(session, tmdb_id=608, release_date=IN_PLAY)
    session.add(
        Follow(
            user_id=entitled_user.id,
            entity_type="title",
            entity_id=str(film.id),
            source="manual",
        )
    )
    await session.commit()

    await _run(session_factory, run_id)

    row = await session.get(IngestRun, run_id, execution_options={"populate_existing": True})
    assert row is not None
    assert row.last_progress_at is not None
    assert row.items_processed == 1


async def test_only_entitled_users_with_follows_are_selected(session, make_user, entitled_user):
    """The selection read on its own, because the phase's cost is one statement per row it
    returns and the two filters are easy to drop independently."""
    await make_user(email="unentitled@example.com")
    session.add(
        Follow(user_id=entitled_user.id, entity_type="person", entity_id="525", source="manual")
    )
    await session.commit()

    assert await load_derivation_user_ids(session) == [entitled_user.id]
