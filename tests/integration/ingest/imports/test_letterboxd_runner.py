"""The Letterboxd import runner (D-15): what a fixture export turns into, and what a second
run of the same export does not.

The acceptance criteria of `NEU-1356-letterboxd-import.md`, as EF-20 and EF-21 leave them —
the full export, the alert window, the re-upload, and the crash — driven through
`import_letterboxd` against a respx-mocked TMDB.

**No person follow is ever written by an import any more** (EF-20), which is asserted for the
whole run rather than per case: the ratings path that wrote them is gone, and the assertion is
here so that nothing quietly grows a second one back."""

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow, ImportJob
from upmovies.app.repos import import_job_repo
from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.ingest.imports.letterboxd import parse_upload
from upmovies.ingest.imports.runner import import_letterboxd, run_letterboxd_import
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = get_settings().tmdb_base_url.rstrip("/")

# --- the fixture export --------------------------------------------------------------------
#
# Four watchlist rows: two films still inside the alert window, one released in 2019 and so
# outside it (EF-21), and one title TMDB has never heard of. The ratings file rides along in
# the zip and must contribute nothing at all.

WATCHLIST = [
    ("Dune", 2021),
    ("Arrival", 2016),
    ("Long Gone", 2019),
    ("A Film That Does Not Exist", 1999),
]
RATINGS = [("Heat", 1995, 5.0), ("The Insider", 1999, 4.5), ("Blackhat", 2015, 2.0)]

SEARCH_HITS = {
    "Dune": (1001, 2021),
    "Arrival": (1002, 2016),
    "Long Gone": (1003, 2019),
    # The rated titles resolve too, so that a request spent on one would be a *visible*
    # failure rather than a silent unmatched row.
    "Heat": (2001, 1995),
    "The Insider": (2002, 1999),
}

IN_WINDOW_TMDB_IDS = (1001, 1002)
OUTSIDE_WINDOW_TMDB_ID = 1003
RATED_TMDB_IDS = (2001, 2002)

# Every film TMDB answers for, and what it says about the two things the window reads. An
# undated, `Post Production` film is inside the window; a 2019 `Released` one is past the
# 365-day ceiling and outside it.
DETAILS: dict[int, tuple[str, str]] = {
    1001: ("2099-06-01", "Post Production"),
    1002: ("2099-09-01", "Post Production"),
    1003: ("2019-06-01", "Released"),
    2001: ("1995-12-15", "Released"),
    2002: ("1999-11-05", "Released"),
}


def _mock_tmdb() -> None:
    """Every request the fixture export can make. A title with no route answers zero hits,
    which is how `A Film That Does Not Exist` becomes the unmatched row."""
    respx.get(f"{BASE_URL}/search/movie").mock(
        side_effect=lambda request: httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 1,
                "total_results": 0,
                "results": _hits_for(request.url.params.get("query")),
            },
        )
    )
    for tmdb_id, (release_date, status) in DETAILS.items():
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(
                200,
                json=make_details(
                    tmdb_id,
                    release_date=release_date,
                    status=status,
                    credits=_credits(tmdb_id),
                ),
            )
        )


def _credits(tmdb_id: int) -> dict:
    """A director and three billed cast for one film, ids derived from its own.

    Present in every payload on purpose. The deleted people path followed a film's director
    and top-2 billing, so a payload with no credits in it would make
    `test_an_import_never_writes_a_person_follow` pass for the wrong reason — there would be
    nobody to follow. It also gives the film a `credits_observed_at`, which is what the
    freshness short-circuit in `film_id_for` reads on a second run."""
    director = tmdb_id * 10
    return {
        "crew": [
            {
                "id": director,
                "name": f"Director {director}",
                "credit_id": f"crew-{director}",
                "department": "Directing",
                "job": "Director",
            }
        ],
        "cast": [
            {
                "id": director + 1 + order,
                "name": f"Actor {director + 1 + order}",
                "credit_id": f"cast-{director + 1 + order}",
                "order": order,
            }
            for order in range(3)
        ],
    }


def _hits_for(query: str | None) -> list[dict]:
    match = SEARCH_HITS.get(query or "")
    if match is None:
        return []
    tmdb_id, year = match
    return [{"id": tmdb_id, "title": query, "release_date": f"{year}-06-01", "popularity": 10.0}]


def _client() -> TMDBClient:
    settings = get_settings()
    return TMDBClient(
        base_url=settings.tmdb_base_url,
        api_key="test-key",
        rate_calls=1000,
        rate_window=1,
        retry_max_attempts=1,
        retry_base_delay=0.01,
    )


@pytest.fixture
def export():
    return parse_upload(
        export_zip({"watchlist.csv": watchlist_csv(WATCHLIST), "ratings.csv": ratings_csv(RATINGS)})
    )


@pytest.fixture
async def user(make_user):
    return await make_user(email="importer@example.com")


async def _queue(session, user, export) -> ImportJob:
    job = await import_job_repo.create(
        session, user_id=user.id, source="letterboxd", rows_total=export.row_count
    )
    await session.commit()
    return job


async def _run(session, session_factory, user, export) -> ImportJob:
    job = await _queue(session, user, export)
    async with _client() as client:
        await import_letterboxd(
            session_factory=session_factory, client=client, job_id=job.id, export=export
        )
    return await session.get(ImportJob, job.id, populate_existing=True)


async def _rows(session, model) -> list:
    return list((await session.execute(select(model))).scalars().all())


async def _follows(session) -> list[Follow]:
    return await _rows(session, Follow)


# --- the whole export ----------------------------------------------------------------------


@respx.mock
async def test_a_fixture_export_produces_title_follows_and_a_report(
    session, session_factory, user, export
):
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.status == "succeeded"
    # The ratings are not rows the runner steps through any more: `rows_total` is the watchlist.
    assert job.rows_done == job.rows_total == len(WATCHLIST)
    assert job.error is None

    # Two of the four rows became follows; the other two are the whole of the report, and each
    # names why it is there rather than only that it is.
    assert (job.watchlist_created, job.follows_created) == (2, 2)
    assert job.unmatched == [
        {"name": "Long Gone", "year": 2019, "kind": "outside_window"},
        {"name": "A Film That Does Not Exist", "year": 1999, "kind": "watchlist"},
    ]

    follows = await _follows(session)
    assert len(follows) == 2
    assert {f.entity_type for f in follows} == {"title"}
    assert {f.source for f in follows} == {"letterboxd_import"}


@respx.mock
async def test_an_import_never_writes_a_person_follow(session, session_factory, user, export):
    """EF-20, and the whole of why this ticket exists. A follow is binary (EF-1), so a person
    follow inferred from a four-star rating would push every credit change of somebody the user
    once enjoyed at them."""
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    assert [f for f in await _follows(session) if f.entity_type != "title"] == []


@respx.mock
async def test_the_ratings_cost_no_requests_and_produce_no_report_rows(
    session, session_factory, user, export
):
    # Every rated title in the fixture resolves, so a request spent on one would show up here
    # rather than passing as a search that found nothing.
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    searched = {
        call.request.url.params.get("query")
        for call in respx.calls
        if "/search/movie" in str(call.request.url)
    }
    assert searched.isdisjoint({name for name, _, _ in RATINGS})
    assert {row["name"] for row in job.unmatched}.isdisjoint({name for name, _, _ in RATINGS})
    assert {f.tmdb_id for f in await _rows(session, Film)}.isdisjoint(RATED_TMDB_IDS)


# --- the alert window ----------------------------------------------------------------------


@respx.mock
async def test_a_film_released_in_2019_is_skipped_as_outside_window(
    session, session_factory, user, export
):
    """EF-21: the import proposes only films that can still deliver something. 2019 is past the
    365-day `PROVIDER_POLL_MAX_AGE_DAYS` ceiling, so there is no beat left for a follow on it
    to carry."""
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    film = (
        await session.execute(select(Film).where(Film.tmdb_id == OUTSIDE_WINDOW_TMDB_ID))
    ).scalar_one()
    assert str(film.id) not in {f.entity_id for f in await _follows(session)}
    assert {"name": "Long Gone", "year": 2019, "kind": "outside_window"} in job.unmatched


@respx.mock
async def test_a_film_outside_the_window_is_still_upserted(session, session_factory, user, export):
    """The window decides the *follow*, not the catalog row. The film is worth holding: the
    next import, or a manual follow from the film page, finds it already there."""
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    assert OUTSIDE_WINDOW_TMDB_ID in {f.tmdb_id for f in await _rows(session, Film)}


@respx.mock
async def test_a_canceled_film_is_skipped_however_recent_its_date(session, session_factory, user):
    # The window's status term is `Canceled` alone (NEU-1417) — a film called off next year
    # has a date well inside the ceiling and still nothing to say.
    export = parse_upload(watchlist_csv([("Dune", 2021)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(
        return_value=httpx.Response(
            200,
            json=make_details(
                1001, release_date="2099-06-01", status="Canceled", credits=_credits(1001)
            ),
        )
    )

    job = await _run(session, session_factory, user, export)

    assert await _follows(session) == []
    assert job.unmatched == [{"name": "Dune", "year": 2021, "kind": "outside_window"}]


@respx.mock
async def test_an_upcoming_film_becomes_a_title_follow_carrying_the_import_source(
    session, session_factory, user
):
    export = parse_upload(watchlist_csv([("Dune", 2021)]))
    _mock_tmdb()

    job = await _run(session, session_factory, user, export)

    film = (await session.execute(select(Film).where(Film.tmdb_id == 1001))).scalar_one()
    (follow,) = await _follows(session)
    assert (follow.entity_type, follow.entity_id) == ("title", str(film.id))
    assert follow.source == "letterboxd_import"
    assert (job.watchlist_created, job.follows_created) == (1, 1)
    assert job.unmatched == []


@respx.mock
async def test_a_film_with_no_release_date_is_inside_the_window(session, session_factory, user):
    # The NULL guards in `alert_window_clause` are the point: an undated film is the most
    # upcoming thing there is, and dropping it would be exactly backwards.
    export = parse_upload(watchlist_csv([("Dune", 2021)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(
        return_value=httpx.Response(
            200,
            json=make_details(1001, release_date=None, status="Rumored", credits=_credits(1001)),
        )
    )

    job = await _run(session, session_factory, user, export)

    assert len(await _follows(session)) == 1
    assert job.unmatched == []


# --- running it twice ----------------------------------------------------------------------


@respx.mock
async def test_re_uploading_the_same_export_creates_nothing_new(
    session, session_factory, user, export
):
    _mock_tmdb()
    first = await _run(session, session_factory, user, export)
    before = len(await _follows(session))

    second = await _run(session, session_factory, user, export)

    assert second.status == "succeeded"
    assert (second.follows_created, second.watchlist_created) == (0, 0)
    assert len(await _follows(session)) == before
    # The report is not idempotency-dependent: the title is still unplaceable, and the 2019
    # film is still outside the window.
    assert second.unmatched == first.unmatched


@respx.mock
async def test_a_second_run_reads_a_fresh_films_credits_out_of_the_catalog(
    session, session_factory, user, export
):
    # The watchlist path re-fetches only when `credits_observed_at` is stale (> 7 days).
    _mock_tmdb()
    await _run(session, session_factory, user, export)
    fetched_first = len(respx.calls)
    await _run(session, session_factory, user, export)

    details_calls = [
        call
        for call in list(respx.calls)[fetched_first:]
        if any(f"/movie/{tmdb_id}" in str(call.request.url) for tmdb_id in IN_WINDOW_TMDB_IDS)
    ]
    assert details_calls == []


# --- the mute ------------------------------------------------------------------------------


@respx.mock
async def test_an_import_follows_a_film_the_catalog_already_holds(
    session, session_factory, user, export
):
    """The follow is the whole of what an import writes for a listed film (EF-14): the mute it
    used to have to leave alone (D-1414.9) went with the watchlist it corrected."""
    film = await add_film(session, tmdb_id=1001, title="Dune", slug="dune")
    await session.commit()

    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.watchlist_created == 2
    assert str(film.id) in {f.entity_id for f in await _follows(session)}


# --- the crash -----------------------------------------------------------------------------


@respx.mock
async def test_a_crash_mid_run_fails_the_job_and_keeps_the_rows_already_done(
    session, session_factory, user
):
    # `run_letterboxd_import` rather than `import_letterboxd`: the wrapper's `except` is the
    # thing under test. Nothing awaits the task in production, so an exception escaping it
    # would leave the job polling `running` forever.
    export = parse_upload(watchlist_csv([("Dune", 2021), ("Arrival", 2016)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1002").mock(return_value=httpx.Response(500))

    job = await _queue(session, user, export)
    await run_letterboxd_import(
        job.id, export, get_settings().model_copy(update={"tmdb_retry_max_attempts": 1})
    )

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "failed"
    assert finished.error
    # The first row survived whole: commit-per-row is what makes a partial import worth
    # keeping, and the alternative — one transaction for the job — would discard it.
    assert {f.tmdb_id for f in await _rows(session, Film)} == {1001}
    assert len(await _follows(session)) == 1
