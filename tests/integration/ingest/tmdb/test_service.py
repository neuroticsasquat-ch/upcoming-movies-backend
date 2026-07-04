from datetime import date

import httpx
import respx
from sqlalchemy import select

from tests.fixtures.tmdb import make_details, make_discover_page, make_summary
from upmovies.catalog.models import Film
from upmovies.ingest.models import IngestRun
from upmovies.ingest.runs import create_run
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.service import run_tmdb_ingest

BASE = "https://api.themoviedb.org/3"
GTE = date(2026, 5, 14)
LTE = date(2029, 6, 13)


def _client() -> TMDBClient:
    return TMDBClient(
        base_url=BASE,
        api_key="test-key",
        rate_calls=50,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.001,
    )


def _discover(**params):
    return respx.get(f"{BASE}/discover/movie", params=params)


def _details(tmdb_id):
    return respx.get(f"{BASE}/movie/{tmdb_id}")


async def _run(
    session,
    *,
    min_popularity: float = 10.0,
    failure_threshold: int = 10,
    excluded_statuses: frozenset[str] = frozenset(),
    min_runtime: int = 0,
):
    run_id = await create_run(session, kind="tmdb")
    await session.commit()
    async with _client() as c:
        result = await run_tmdb_ingest(
            session_factory=lambda: session,
            client=c,
            run_id=run_id,
            release_date_gte=GTE,
            release_date_lte=LTE,
            min_popularity=min_popularity,
            failure_threshold=failure_threshold,
            excluded_statuses=excluded_statuses,
            min_runtime=min_runtime,
        )
    return run_id, result


async def _films(session):
    result = await session.execute(
        select(Film).order_by(Film.tmdb_id), execution_options={"populate_existing": True}
    )
    return list(result.scalars().all())


@respx.mock
async def test_ingest_inserts_new_films(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1, total_pages=1, results=[make_summary(1), make_summary(2)]
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1, title="One")))
    _details(2).mock(return_value=httpx.Response(200, json=make_details(2, title="Two")))

    _, result = await _run(session)

    assert (result.films_processed, result.films_failed) == (2, 0)
    films = await _films(session)
    assert {f.tmdb_id for f in films} == {1, 2}
    assert {f.title for f in films} == {"One", "Two"}


@respx.mock
async def test_ingest_sends_window_filter_without_locale(session):
    route = _discover(page="1").mock(
        return_value=httpx.Response(
            200, json=make_discover_page(page=1, total_pages=1, results=[make_summary(1)])
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1)))

    await _run(session)

    params = route.calls.last.request.url.params
    assert params["sort_by"] == "popularity.desc"
    assert params["primary_release_date.gte"] == "2026-05-14"
    assert params["primary_release_date.lte"] == "2029-06-13"
    assert "with_original_language" not in params
    assert "region" not in params


@respx.mock
async def test_ingest_updates_existing_film_idempotently(session):
    session.add(Film(tmdb_id=1, title="Stale"))
    await session.commit()

    _discover(page="1").mock(
        return_value=httpx.Response(
            200, json=make_discover_page(page=1, total_pages=1, results=[make_summary(1)])
        )
    )
    _details(1).mock(
        return_value=httpx.Response(200, json=make_details(1, title="Fresh", status="Released"))
    )

    await _run(session)
    await _run(session)

    films = await _films(session)
    assert len(films) == 1
    assert films[0].title == "Fresh"
    assert films[0].status == "Released"


@respx.mock
async def test_ingest_stops_at_popularity_threshold_and_skips_lower(session):
    # Results are popularity-desc; film 2 is below the floor, so it (and everything
    # after) must be skipped without fetching details.
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=1,
                results=[make_summary(1, popularity=50.0), make_summary(2, popularity=5.0)],
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1)))
    details2 = _details(2).mock(return_value=httpx.Response(200, json=make_details(2)))

    _, result = await _run(session, min_popularity=10.0)

    assert result.films_processed == 1
    assert details2.call_count == 0
    assert {f.tmdb_id for f in await _films(session)} == {1}


@respx.mock
async def test_ingest_stops_paging_once_below_threshold(session):
    page1 = _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=2,
                results=[make_summary(1, popularity=50.0), make_summary(2, popularity=5.0)],
            ),
        )
    )
    page2 = _discover(page="2").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=2, total_pages=2, results=[make_summary(3, popularity=4.0)]
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1)))

    _, result = await _run(session, min_popularity=10.0)

    assert result.films_processed == 1
    assert page1.call_count == 1
    assert page2.call_count == 0, "must not page past the popularity floor"


@respx.mock
async def test_ingest_pages_through_all_results(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=2,
                results=[make_summary(1, popularity=50.0), make_summary(2, popularity=40.0)],
            ),
        )
    )
    _discover(page="2").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=2, total_pages=2, results=[make_summary(3, popularity=30.0)]
            ),
        )
    )
    for i in (1, 2, 3):
        _details(i).mock(return_value=httpx.Response(200, json=make_details(i)))

    _, result = await _run(session)

    assert result.films_processed == 3
    assert {f.tmdb_id for f in await _films(session)} == {1, 2, 3}


@respx.mock
async def test_ingest_continues_past_per_film_failure(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=1,
                results=[make_summary(1), make_summary(2), make_summary(3)],
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1)))
    _details(2).mock(return_value=httpx.Response(404))
    _details(3).mock(return_value=httpx.Response(200, json=make_details(3)))

    _, result = await _run(session)

    assert (result.films_processed, result.films_failed) == (2, 1)
    assert {f.tmdb_id for f in await _films(session)} == {1, 3}


@respx.mock
async def test_ingest_aborts_after_consecutive_failures(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=1,
                results=[make_summary(1), make_summary(2), make_summary(3)],
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(404))
    _details(2).mock(return_value=httpx.Response(404))
    details3 = _details(3).mock(return_value=httpx.Response(404))

    run_id, result = await _run(session, failure_threshold=2)

    assert result.films_failed == 2
    assert details3.call_count == 0, "aborts before processing the third film"
    row = (
        await session.execute(
            select(IngestRun).where(IngestRun.id == run_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    assert row.status == "failed"
    assert row.error is not None and "consecutive failures" in row.error


@respx.mock
async def test_ingest_skips_excluded_statuses(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=1,
                results=[make_summary(1), make_summary(2), make_summary(3)],
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(200, json=make_details(1, status="Released")))
    _details(2).mock(return_value=httpx.Response(200, json=make_details(2, status="Planned")))
    _details(3).mock(return_value=httpx.Response(200, json=make_details(3, status="Canceled")))

    _, result = await _run(session, excluded_statuses=frozenset({"Released", "Canceled"}))

    assert result.films_processed == 1
    assert result.films_skipped == 2
    assert {f.tmdb_id for f in await _films(session)} == {2}


@respx.mock
async def test_ingest_skip_resets_consecutive_failure_counter(session):
    # threshold=2: fail (cf=1), skip (cf reset to 0), fail (cf=1), process.
    # If a skip did NOT reset the counter, the second failure would abort before film 4.
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1,
                total_pages=1,
                results=[make_summary(1), make_summary(2), make_summary(3), make_summary(4)],
            ),
        )
    )
    _details(1).mock(return_value=httpx.Response(404))
    _details(2).mock(return_value=httpx.Response(200, json=make_details(2, status="Released")))
    _details(3).mock(return_value=httpx.Response(404))
    _details(4).mock(return_value=httpx.Response(200, json=make_details(4, status="Planned")))

    _, result = await _run(session, failure_threshold=2, excluded_statuses=frozenset({"Released"}))

    assert (result.films_processed, result.films_failed, result.films_skipped) == (1, 2, 1)
    assert {f.tmdb_id for f in await _films(session)} == {4}


@respx.mock
async def test_ingest_skips_shorts(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1, total_pages=1, results=[make_summary(1), make_summary(2)]
            ),
        )
    )
    _details(1).mock(
        return_value=httpx.Response(200, json=make_details(1, runtime=7, status="Planned"))
    )
    _details(2).mock(
        return_value=httpx.Response(200, json=make_details(2, runtime=120, status="Planned"))
    )

    _, result = await _run(session, min_runtime=60)

    assert result.films_processed == 1
    assert result.films_skipped == 1
    assert result.skipped_by_reason == {"short": 1}
    assert {f.tmdb_id for f in await _films(session)} == {2}


@respx.mock
async def test_ingest_keeps_unknown_runtime(session):
    # runtime 0 == unfinished/unknown, must be kept even with the shorts rule on.
    _discover(page="1").mock(
        return_value=httpx.Response(
            200, json=make_discover_page(page=1, total_pages=1, results=[make_summary(1)])
        )
    )
    _details(1).mock(
        return_value=httpx.Response(200, json=make_details(1, runtime=0, status="Planned"))
    )

    _, result = await _run(session, min_runtime=60)

    assert result.films_processed == 1
    assert result.films_skipped == 0
    assert {f.tmdb_id for f in await _films(session)} == {1}


@respx.mock
async def test_ingest_reports_skip_reasons_breakdown(session):
    _discover(page="1").mock(
        return_value=httpx.Response(
            200,
            json=make_discover_page(
                page=1, total_pages=1, results=[make_summary(1), make_summary(2), make_summary(3)]
            ),
        )
    )
    _details(1).mock(
        return_value=httpx.Response(200, json=make_details(1, runtime=7, status="Planned"))
    )
    _details(2).mock(
        return_value=httpx.Response(200, json=make_details(2, runtime=120, status="Released"))
    )
    _details(3).mock(
        return_value=httpx.Response(200, json=make_details(3, runtime=120, status="Planned"))
    )

    _, result = await _run(session, min_runtime=60, excluded_statuses=frozenset({"Released"}))

    assert result.films_processed == 1
    assert result.skipped_by_reason == {"short": 1, "excluded_status": 1}
    assert {f.tmdb_id for f in await _films(session)} == {3}
