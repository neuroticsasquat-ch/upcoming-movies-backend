"""The watch-provider poll end to end against a mocked TMDB: which films it selects, and what
it writes for them (D-27).

The scoped set carries most of the weight. It is two independent rules — a theatrical date
inside the age window, or *anybody's* follows covering the film (D-1414.3) — and the second is
not an optimisation of the first: a film somebody follows by title that never had a US
theatrical date has nothing to age, so rule 1 alone would never poll it and the user would never
hear that it landed. Rule 2 is the computed watchlist asked of every user at once, so it reaches
a film followed only through its director too, and drops one every covering user has muted.

The write is the other half, and it is two tables with opposite lifetimes over one read: the
ledger (`availability_first_seen`) is insert-only and is what `now_available` will card off
(D-28), while the snapshot (`film_availability_current`) is rebuilt wholesale so a film leaving
a service leaves the where-to-watch box (D-29) without disturbing the fact that it was there.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_credit, add_film
from tests.fixtures.tmdb import make_provider, make_watch_providers
from upmovies.app.models import Follow, WatchlistDismissal
from upmovies.catalog.models import (
    AvailabilityFirstSeen,
    Film,
    FilmAvailabilityCurrent,
    FilmReleaseDate,
    WatchProvider,
)
from upmovies.ingest import runs
from upmovies.ingest.models import IngestRun
from upmovies.ingest.providers import run_provider_poll
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.news.models import Event, EventSummary

BASE_URL = "https://api.themoviedb.org/3"
TODAY = date(2026, 9, 17)
MIN_AGE = 14
MAX_AGE = 365
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, pinned here the way `TODAY` is so the poll set's reach
does not depend on the environment the suite runs in."""
# The window is [TODAY - 365, TODAY - 14] inclusive.
IN_WINDOW = TODAY - timedelta(days=60)
TOO_RECENT = TODAY - timedelta(days=3)
TOO_OLD = TODAY - timedelta(days=400)
UPCOMING = TODAY + timedelta(days=30)

WIDE = 3
LIMITED = 2
DIGITAL = 4


@pytest.fixture
async def tmdb_client():
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        yield client


@pytest.fixture
async def run_id(session):
    run = await runs.create_run(session, kind="providers")
    await session.commit()
    return run


async def _add_release(
    session, film: Film, *, on: date, release_type: int = WIDE, region: str = "US"
) -> None:
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1=region,
            release_type=release_type,
            release_date=datetime.combine(on, datetime.min.time(), tzinfo=UTC),
        )
    )
    await session.flush()


async def _add_released_film(session, tmdb_id: int, *, on: date | None = IN_WINDOW, **overrides):
    """A film past its US theatrical date — the poll's ordinary subject, and one the sweep's
    `in_play_clause` has already dropped."""
    film = await add_film(session, tmdb_id, status=overrides.pop("status", "Released"), **overrides)
    if on is not None:
        await _add_release(session, film, on=on)
    return film


def _mock_providers(tmdb_id: int, **kwargs):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}/watch/providers").mock(
        return_value=httpx.Response(200, json=make_watch_providers(tmdb_id, **kwargs))
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "client": tmdb_client,
        "run_id": run_id,
        "today": TODAY,
        "min_age_days": MIN_AGE,
        "max_age_days": MAX_AGE,
    }
    return await run_provider_poll(**{**kwargs, **overrides})


async def _first_seen(session, film: Film) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(AvailabilityFirstSeen.provider_id, AvailabilityFirstSeen.monetization_type)
        .where(AvailabilityFirstSeen.film_id == film.id)
        .order_by(AvailabilityFirstSeen.id)
    )
    return [(pid, kind) for pid, kind in rows]


async def _current(session, film: Film) -> list[tuple[int, str]]:
    rows = await session.execute(
        select(FilmAvailabilityCurrent.provider_id, FilmAvailabilityCurrent.monetization_type)
        .where(FilmAvailabilityCurrent.film_id == film.id)
        .order_by(FilmAvailabilityCurrent.id)
    )
    return [(pid, kind) for pid, kind in rows]


# --- the scoped set (D-27) -----------------------------------------------------


@respx.mock
async def test_polls_a_film_inside_the_theatrical_window(
    session, session_factory, tmdb_client, run_id
):
    await _add_released_film(session, 100)
    await session.commit()
    _mock_providers(100, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_skips_films_outside_the_window_in_either_direction(
    session, session_factory, tmdb_client, run_id
):
    """The floor keeps the poll off a film still in cinemas, where a home release is not yet
    plausible; the ceiling is where a film that never got one stops costing a request a day."""
    await _add_released_film(session, 101, on=TOO_RECENT)
    await _add_released_film(session, 102, on=TOO_OLD)
    await _add_released_film(session, 103, on=UPCOMING)
    await session.commit()
    untouched = [
        respx.get(f"{BASE_URL}/movie/{tmdb_id}/watch/providers") for tmdb_id in (101, 102, 103)
    ]

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0
    assert not any(route.called for route in untouched)


@respx.mock
async def test_the_window_is_inclusive_at_both_ends(session, session_factory, tmdb_client, run_id):
    await _add_released_film(session, 104, on=TODAY - timedelta(days=MIN_AGE))
    await _add_released_film(session, 105, on=TODAY - timedelta(days=MAX_AGE))
    await session.commit()
    _mock_providers(104, flatrate=[8])
    _mock_providers(105, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (2, 2)


@respx.mock
async def test_a_subject_in_the_window_wins_over_an_older_one(
    session, session_factory, tmdb_client, run_id
):
    """The rule reads each US theatrical *subject's* governing date, not the film's earliest
    date. A title that opened limited 400 days ago and wide 60 days ago is squarely in the
    window on the beat an audience would name, and the earliest-across-the-film reading would
    drop it."""
    film = await _add_released_film(session, 106, on=None)
    await _add_release(session, film, on=TOO_OLD, release_type=LIMITED)
    await _add_release(session, film, on=IN_WINDOW, release_type=WIDE)
    await session.commit()
    _mock_providers(106, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_only_us_theatrical_dates_put_a_film_in_the_window(
    session, session_factory, tmdb_client, run_id
):
    """Region and type both matter: a French theatrical run says nothing about US
    availability, and a US *digital* date is the answer this poll exists to find rather than
    the question."""
    film_fr = await add_film(session, 107, status="Released")
    await _add_release(session, film_fr, on=IN_WINDOW, region="FR")
    film_digital = await add_film(session, 108, status="Released")
    await _add_release(session, film_digital, on=IN_WINDOW, release_type=DIGITAL)
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0


@respx.mock
async def test_a_followed_title_is_polled_whatever_its_dates_say(
    session, session_factory, tmdb_client, run_id, make_user
):
    """The other half of rule 2. A title follow carries the film's UUID in a text column, so
    this also pins the cast the predicate does on the way in."""
    user = await make_user(email="follower@example.com")
    film = await add_film(session, 201, status="Released")
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()
    _mock_providers(201, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_a_person_follow_reaching_no_credit_puts_nothing_in_the_poll_set(
    session, session_factory, tmdb_client, run_id, make_user
):
    """A person follow carries a TMDB person id in the same text column a title follow carries
    a film UUID in, and reading it as a film id would poll an arbitrary film or none. It
    reaches films through `catalog.film_credit` or not at all."""
    user = await make_user(email="person-follower@example.com")
    await add_film(session, 202, status="Released")
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="525", source="manual"))
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0


@respx.mock
async def test_a_film_covered_only_through_a_director_follow_is_polled(
    session, session_factory, tmdb_client, run_id, make_user
):
    """What M8 widened (D-1414.3). The old rule reached this film only where D-13 had already
    derived a watchlist item; now the poll reads the same coverage the alerts do, so a followed
    director's next film is polled for the offer that will card its `now_available` beat."""
    user = await make_user(email="director-follower@example.com")
    film = await add_film(session, 203)
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="525", source="manual"))
    await session.commit()
    _mock_providers(203, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_a_released_film_an_indirect_follow_reaches_is_polled_past_the_old_ceiling(
    session, session_factory, tmdb_client, run_id, make_user
):
    """The reach D-46 added. 250 days past its primary date and marked `Released`, with no US
    theatrical row to put it in rule 1 — under NEU-1414's window the status term cut it out on
    release day, and its streaming debut went unpolled and uncarded. The window now ends at
    `Canceled`, so rule 2 keeps it until the date ceiling; nobody following it still means no
    request."""
    user = await make_user(email="late-streamer@example.com")
    film = await add_film(session, 209, status="Released", release_date=TODAY - timedelta(days=250))
    await add_credit(session, film, 527, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    assert (await _run(session_factory, tmdb_client, run_id)).selected == 0

    session.add(Follow(user_id=user.id, entity_type="person", entity_id="527", source="manual"))
    await session.commit()
    _mock_providers(209, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_a_non_seed_credit_puts_the_film_in_the_poll_set(
    session, session_factory, tmdb_client, run_id, make_user
):
    """The poll's set is bounded by the same rule the alerts are, and EF-2 removed the cut from
    both at once: an unbilled cast credit is enough, because somebody following that person is
    waiting to hear when this film can be watched.

    The unbilled row rather than the writing credit this test used to carry: a writer was
    always seed grade, so it only ever proved the tier, while `credit_order IS NULL` is outside
    every cut there has ever been and is what a seed-grade term surviving here would drop."""
    user = await make_user(email="unbilled-follower@example.com")
    film = await add_film(session, 204)
    await add_credit(session, film, 526, credit_type="cast", credit_order=None)
    session.add(Follow(user_id=user.id, entity_type="person", entity_id="526", source="manual"))
    await session.commit()
    _mock_providers(204, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_a_film_every_covering_user_has_muted_leaves_the_poll_set(
    session, session_factory, tmdb_client, run_id, make_user
):
    """The mute is per covering *user*, not per film: the film stays in the set while anybody
    who covers it is still listening, and leaves it when the last of them stops."""
    muter = await make_user(email="muter@example.com")
    waiter = await make_user(email="waiter@example.com")
    film = await add_film(session, 205, status="Released")
    for user in (muter, waiter):
        session.add(
            Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
        )
    session.add(WatchlistDismissal(user_id=muter.id, film_id=film.id))
    await session.commit()
    _mock_providers(205, flatrate=[8])

    assert (await _run(session_factory, tmdb_client, run_id)).selected == 1

    session.add(WatchlistDismissal(user_id=waiter.id, film_id=film.id))
    await session.commit()

    assert (await _run(session_factory, tmdb_client, run_id)).selected == 0


@respx.mock
async def test_a_tombstoned_film_is_not_polled(session, session_factory, tmdb_client, run_id):
    """Its theatrical date goes on ageing inside the window, so without the exclusion a
    deleted id costs a request a day until it falls out the far end — and the poll is the only
    reader, the refresh phase having dropped the film as `Released` (NEU-1124)."""
    await _add_released_film(session, 109, tmdb_missing_at=datetime(2026, 9, 1, tzinfo=UTC))
    await session.commit()
    untouched = respx.get(f"{BASE_URL}/movie/109/watch/providers")

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, untouched.called) == (0, False)


@respx.mock
async def test_a_film_matching_both_rules_is_polled_once(
    session, session_factory, tmdb_client, run_id, make_user
):
    user = await make_user(email="both@example.com")
    film = await _add_released_film(session, 110)
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()
    route = _mock_providers(110, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, route.call_count) == (1, 1)


# --- what a poll writes --------------------------------------------------------


@respx.mock
async def test_writes_the_ledger_the_snapshot_and_the_providers(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 300)
    await session.commit()
    _mock_providers(300, flatrate=[8], rent=[2], buy=[2], link="https://example.test/watch")

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.offers, result.first_seen) == (3, 3)
    assert await _first_seen(session, film) == [(8, "flatrate"), (2, "rent"), (2, "buy")]
    assert await _current(session, film) == [(8, "flatrate"), (2, "rent"), (2, "buy")]
    providers = (
        await session.execute(select(WatchProvider.id).order_by(WatchProvider.id))
    ).scalars()
    assert list(providers) == [2, 8]
    link = (
        await session.execute(
            select(FilmAvailabilityCurrent.link).where(FilmAvailabilityCurrent.film_id == film.id)
        )
    ).scalars()
    assert set(link) == {"https://example.test/watch"}


@respx.mock
async def test_first_seen_is_inserted_once_however_often_the_offer_is_observed(
    session, session_factory, tmdb_client, run_id
):
    """The insert-only rule (D-27). The second poll writes no ledger row, which is what keeps
    `first_seen_at` saying *first* and what stops `now_available` carding the same beat twice."""
    film = await _add_released_film(session, 301)
    await session.commit()
    _mock_providers(301, flatrate=[8])
    first = await _run(session_factory, tmdb_client, run_id)
    stamped = (
        await session.execute(
            select(AvailabilityFirstSeen.first_seen_at).where(
                AvailabilityFirstSeen.film_id == film.id
            )
        )
    ).scalar_one()

    second = await _run(session_factory, tmdb_client, run_id)

    assert (first.first_seen, second.first_seen) == (1, 0)
    assert await _first_seen(session, film) == [(8, "flatrate")]
    restamped = (
        await session.execute(
            select(AvailabilityFirstSeen.first_seen_at).where(
                AvailabilityFirstSeen.film_id == film.id
            )
        )
    ).scalar_one()
    assert restamped == stamped


@respx.mock
async def test_each_film_is_stamped_when_it_was_seen(session, session_factory, tmdb_client, run_id):
    """`first_seen_at` is the ledger's only payload — it is what D-28 reports as when the film
    landed — so it has to say when *this* film was read, not when a pass that may run for an
    hour began."""
    film_a = await _add_released_film(session, 307)
    film_b = await _add_released_film(session, 308)
    await session.commit()
    _mock_providers(307, flatrate=[8])
    _mock_providers(308, flatrate=[8])

    await _run(session_factory, tmdb_client, run_id)

    stamps = [
        (
            await session.execute(
                select(AvailabilityFirstSeen.first_seen_at).where(
                    AvailabilityFirstSeen.film_id == film.id
                )
            )
        ).scalar_one()
        for film in (film_a, film_b)
    ]
    assert stamps[0] != stamps[1], "two films read seconds apart share one run-start stamp"


@respx.mock
async def test_a_duplicate_offer_in_one_payload_does_not_cost_the_film_its_poll(
    session, session_factory, tmdb_client, run_id
):
    """TMDB repeating a provider inside one monetization list must not collide on the natural
    key, fail the film, and spend the abort budget on an unambiguous payload."""
    film = await _add_released_film(session, 309)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/309/watch/providers").mock(
        return_value=httpx.Response(
            200,
            json=make_watch_providers(
                309,
                regions={
                    "US": {
                        "link": "https://example.test/watch",
                        "flatrate": [
                            {"provider_id": 8, "provider_name": "Eight"},
                            {"provider_id": 8, "provider_name": "Eight"},
                        ],
                    }
                },
            ),
        )
    )

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.polled, result.failures) == (1, 0)
    assert await _current(session, film) == [(8, "flatrate")]
    assert await _first_seen(session, film) == [(8, "flatrate")]


@respx.mock
async def test_a_new_offer_on_a_known_film_is_a_new_ledger_row(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 302)
    await session.commit()
    route = _mock_providers(302, flatrate=[8])
    await _run(session_factory, tmdb_client, run_id)
    route.mock(
        return_value=httpx.Response(200, json=make_watch_providers(302, flatrate=[8], rent=[2]))
    )

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.first_seen == 1
    assert await _first_seen(session, film) == [(8, "flatrate"), (2, "rent")]


@respx.mock
async def test_the_snapshot_is_rebuilt_and_the_ledger_is_not_disturbed(
    session, session_factory, tmdb_client, run_id
):
    """A film leaving a service disappears from the where-to-watch box and stays in the ledger
    — the product never tracks churn, and never forgets that it was once available."""
    film = await _add_released_film(session, 303)
    await session.commit()
    route = _mock_providers(303, flatrate=[8], rent=[2])
    await _run(session_factory, tmdb_client, run_id)
    route.mock(return_value=httpx.Response(200, json=make_watch_providers(303, rent=[2])))

    result = await _run(session_factory, tmdb_client, run_id)

    assert await _current(session, film) == [(2, "rent")]
    assert await _first_seen(session, film) == [(8, "flatrate"), (2, "rent")]
    assert result.first_seen == 0


@respx.mock
async def test_a_film_nobody_carries_empties_its_snapshot(
    session, session_factory, tmdb_client, run_id
):
    """TMDB answers an empty `results` for a film no provider holds. Treating that as "nothing
    to do" would freeze the box at whatever the last poll found."""
    film = await _add_released_film(session, 304)
    await session.commit()
    route = _mock_providers(304, flatrate=[8])
    await _run(session_factory, tmdb_client, run_id)
    route.mock(return_value=httpx.Response(200, json=make_watch_providers(304)))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (await _current(session, film), result.polled) == ([], 1)
    assert await _first_seen(session, film) == [(8, "flatrate")]


@respx.mock
async def test_a_renamed_provider_is_updated_rather_than_duplicated(
    session, session_factory, tmdb_client, run_id
):
    await _add_released_film(session, 305)
    await session.commit()
    route = _mock_providers(305, flatrate=[8])
    await _run(session_factory, tmdb_client, run_id)
    route.mock(
        return_value=httpx.Response(
            200,
            json=make_watch_providers(
                305,
                regions={
                    "US": {
                        "link": "https://example.test/watch",
                        "flatrate": [
                            {
                                "provider_id": 8,
                                "provider_name": "Eight Plus",
                                "logo_path": "/new.jpg",
                            }
                        ],
                    }
                },
            ),
        )
    )

    await _run(session_factory, tmdb_client, run_id)

    provider = await session.get(WatchProvider, 8, populate_existing=True)
    assert (provider.name, provider.logo_path) == ("Eight Plus", "/new.jpg")


@respx.mock
async def test_only_the_polled_region_is_rebuilt(session, session_factory, tmdb_client, run_id):
    """v1 polls US, but the tables are keyed by region — a US poll must not be the thing that
    deletes a later region's rows."""
    film = await _add_released_film(session, 306)
    session.add(WatchProvider(id=8, name="Eight"))
    await session.flush()
    session.add(
        FilmAvailabilityCurrent(
            film_id=film.id, region="GB", provider_id=8, monetization_type="flatrate"
        )
    )
    await session.commit()
    _mock_providers(306, rent=[2])

    await _run(session_factory, tmdb_client, run_id)

    rows = await session.execute(
        select(FilmAvailabilityCurrent.region, FilmAvailabilityCurrent.provider_id)
        .where(FilmAvailabilityCurrent.film_id == film.id)
        .order_by(FilmAvailabilityCurrent.region)
    )
    assert list(rows) == [("GB", 8), ("US", 2)]


# --- the per-item contract -----------------------------------------------------


@respx.mock
async def test_one_failure_does_not_cost_the_films_around_it(
    session, session_factory, tmdb_client, run_id
):
    await _add_released_film(session, 400)
    film = await _add_released_film(session, 401)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/400/watch/providers").mock(return_value=httpx.Response(500))
    _mock_providers(401, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.polled, result.failures) == (1, 1)
    assert await _current(session, film) == [(8, "flatrate")]


@respx.mock
async def test_a_404_tombstones_the_film_without_counting_as_an_outage(
    session, session_factory, tmdb_client, run_id
):
    """A deleted id is terminal, not evidence TMDB is down — the distinction that aborted the
    2026-08-11 sweep when it was missing (NEU-1124). It must not spend the failure budget."""
    await _add_released_film(session, 402)
    await _add_released_film(session, 403)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/402/watch/providers").mock(return_value=httpx.Response(404))
    _mock_providers(403, flatrate=[8])

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=1)

    assert (result.missing, result.failures, result.polled) == (1, 0, 1)
    assert not result.aborted
    film = (await session.execute(select(Film).where(Film.tmdb_id == 402))).scalar_one()
    await session.refresh(film)
    assert film.tmdb_missing_at is not None


@respx.mock
async def test_consecutive_failures_abort_the_poll(session, session_factory, tmdb_client, run_id):
    for tmdb_id in (404, 405, 406):
        await _add_released_film(session, tmdb_id)
    await session.commit()
    for tmdb_id in (404, 405, 406):
        respx.get(f"{BASE_URL}/movie/{tmdb_id}/watch/providers").mock(
            return_value=httpx.Response(500)
        )

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=2)

    assert result.aborted
    assert result.failures == 2, "the poll stops at the threshold rather than burning the set"


@respx.mock
async def test_progress_is_recorded_against_the_run(session, session_factory, tmdb_client, run_id):
    """The heartbeat contract the sweep shares (NEU-1117): a long quiet pass has to keep
    saying it is alive, or the stale-run cleanup cancels it mid-flight."""
    await _add_released_film(session, 407)
    await session.commit()
    _mock_providers(407, flatrate=[8])

    await _run(session_factory, tmdb_client, run_id)

    run = await session.get(IngestRun, run_id, populate_existing=True)
    assert run.items_processed == 1
    assert run.last_progress_at is not None


# --- now_available cards (D-28) -------------------------------------------------


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=1)


def _named(region_block: dict[str, list[tuple[int, str]]], *, link: str = "https://x/watch"):
    """A providers payload whose services carry real names, so a card's body can be read."""
    block: dict = {"link": link}
    for field, entries in region_block.items():
        block[field] = [make_provider(pid, provider_name=name) for pid, name in entries]
    return {"US": block}


async def _cards(session, film: Film) -> list[Event]:
    rows = await session.execute(
        select(Event)
        .where(Event.film_id == film.id, Event.event_type == "now_available")
        .order_by(Event.occurred_at, Event.id)
    )
    return list(rows.scalars())


async def _body(session, event: Event) -> str:
    summary = await session.get(EventSummary, event.id)
    assert summary is not None
    return summary.summary


@respx.mock
async def test_a_first_flatrate_observation_cards_now_available(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 501)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/501/watch/providers").mock(
        return_value=httpx.Response(
            200, json=make_watch_providers(501, regions=_named({"flatrate": [(8, "Netflix")]}))
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, now=NOW)

    (card,) = await _cards(session, film)
    assert card.confidence == "confirmed"
    assert card.provenance == "catalog"
    assert card.region == "US"
    assert card.subject_key == ["US:flatrate"]
    # The ledger row's own stamp, not the run's: the card dates to when the film landed.
    assert card.occurred_at == NOW
    assert await _body(session, card) == "Now streaming on Netflix."
    assert result.cards == 1


@respx.mock
async def test_a_move_between_services_cards_nothing(session, session_factory, tmdb_client, run_id):
    """Netflix → Hulu writes a new ledger row — Hulu has never carried this film — and still
    cards nothing: the *type* was first seen long ago, and churn is a non-goal (D-28)."""
    film = await _add_released_film(session, 502)
    await session.commit()
    route = respx.get(f"{BASE_URL}/movie/502/watch/providers").mock(
        return_value=httpx.Response(
            200, json=make_watch_providers(502, regions=_named({"flatrate": [(8, "Netflix")]}))
        )
    )
    await _run(session_factory, tmdb_client, run_id, now=NOW)
    route.mock(
        return_value=httpx.Response(
            200, json=make_watch_providers(502, regions=_named({"flatrate": [(15, "Hulu")]}))
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, now=LATER)

    assert result.first_seen == 1
    assert result.cards == 0
    assert [c.subject_key for c in await _cards(session, film)] == [["US:flatrate"]]


@respx.mock
async def test_a_first_rent_observation_cards_its_own_event(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 503)
    await session.commit()
    route = respx.get(f"{BASE_URL}/movie/503/watch/providers").mock(
        return_value=httpx.Response(
            200, json=make_watch_providers(503, regions=_named({"flatrate": [(8, "Netflix")]}))
        )
    )
    await _run(session_factory, tmdb_client, run_id, now=NOW)
    route.mock(
        return_value=httpx.Response(
            200,
            json=make_watch_providers(
                503,
                regions=_named(
                    {
                        "flatrate": [(8, "Netflix")],
                        "rent": [(2, "Apple TV"), (10, "Prime Video")],
                    }
                ),
            ),
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, now=LATER)

    assert result.cards == 1
    first, second = await _cards(session, film)
    assert first.subject_key == ["US:flatrate"]
    assert second.subject_key == ["US:rent"]
    assert second.occurred_at == LATER
    assert await _body(session, second) == "Available to rent on Apple TV and Prime Video."


@respx.mock
async def test_types_first_seen_in_one_observation_share_a_card(
    session, session_factory, tmdb_client, run_id
):
    """`uq_event_catalog_change` permits one catalog event per (film, type, timestamp), so a
    film that turns up under two monetization types in a single poll is one beat, carrying a
    token per type — the same rule that makes a US limited and US wide date move one card."""
    film = await _add_released_film(session, 504)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/504/watch/providers").mock(
        return_value=httpx.Response(
            200,
            json=make_watch_providers(
                504,
                regions=_named({"rent": [(2, "Apple TV")], "flatrate": [(8, "Netflix")]}),
            ),
        )
    )

    result = await _run(session_factory, tmdb_client, run_id, now=NOW)

    assert result.cards == 1
    (card,) = await _cards(session, film)
    assert card.subject_key == ["US:flatrate", "US:rent"]
    assert await _body(session, card) == (
        "Now streaming on Netflix. Available to rent on Apple TV."
    )


@respx.mock
async def test_a_second_poll_of_an_unchanged_film_cards_nothing(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 505)
    await session.commit()
    _mock_providers(505, flatrate=[8])
    await _run(session_factory, tmdb_client, run_id, now=NOW)

    result = await _run(session_factory, tmdb_client, run_id, now=LATER)

    assert result.first_seen == 0
    assert result.cards == 0
    assert len(await _cards(session, film)) == 1


@respx.mock
async def test_a_film_nobody_carries_cards_nothing(session, session_factory, tmdb_client, run_id):
    film = await _add_released_film(session, 506)
    await session.commit()
    _mock_providers(506)

    result = await _run(session_factory, tmdb_client, run_id, now=NOW)

    assert result.cards == 0
    assert await _cards(session, film) == []


@respx.mock
async def test_the_snapshot_is_written_in_the_order_tmdb_listed_the_services(
    session, session_factory, tmdb_client, run_id
):
    """The rebuild inserts a region's offers in the order the payload listed them, which is
    JustWatch's own ranking.

    Pinned here because the where-to-watch box (D-29, NEU-1376) reads the snapshot back
    `ORDER BY id` and calls that ranking: `public.service._where_to_watch` has no ordering of
    its own, so a dedup or grouping change in `offers_for_region` that reordered the insert
    would silently reorder the box with that endpoint's own tests still green.
    """
    film = await _add_released_film(session, 507)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/507/watch/providers").mock(
        return_value=httpx.Response(
            200,
            json=make_watch_providers(
                507,
                regions=_named(
                    {
                        "flatrate": [(8, "Netflix"), (15, "Hulu"), (1, "AMC+")],
                        "rent": [(2, "Apple TV")],
                    }
                ),
            ),
        )
    )

    await _run(session_factory, tmdb_client, run_id, now=NOW)

    assert await _current(session, film) == [
        (8, "flatrate"),
        (15, "flatrate"),
        (1, "flatrate"),
        (2, "rent"),
    ]
