"""The Letterboxd import runner (D-15): what a fixture export turns into, and what a second
run of the same export does not.

The acceptance criteria of `NEU-1356-letterboxd-import.md` in order — the full export, the
re-upload, the dismissal, and the crash — driven through `import_letterboxd` against a
respx-mocked TMDB."""

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow, ImportJob, WatchlistDismissal
from upmovies.app.repos import import_job_repo
from upmovies.catalog.models import Film, Person
from upmovies.config import get_settings
from upmovies.ingest.imports.letterboxd import parse_upload
from upmovies.ingest.imports.runner import import_letterboxd, run_letterboxd_import
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = get_settings().tmdb_base_url.rstrip("/")

# --- the fixture export --------------------------------------------------------------------
#
# Three watchlist rows, one of them a title TMDB has never heard of; six ratings, three of them
# at or above the four-star cut. The ids are grouped so that a film's role in the import is
# readable from its number: 10xx is watchlisted, 20xx is rated, 1xx directs, 2xx acts.

WATCHLIST = [("Dune", 2021), ("Arrival", 2016), ("A Film That Does Not Exist", 1999)]
RATINGS = [
    ("Heat", 1995, 5.0),
    ("The Insider", 1999, 4.5),
    ("Collateral", 2004, 4.0),
    ("Blackhat", 2015, 2.0),
    ("Miami Vice", 2006, 3.5),
    ("Ali", 2001, 3.0),
]

SEARCH_HITS = {
    "Dune": (1001, 2021),
    "Arrival": (1002, 2016),
    "Heat": (2001, 1995),
    "The Insider": (2002, 1999),
    "Collateral": (2003, 2004),
}

WATCHLIST_TMDB_IDS = (1001, 1002)
RATED_TMDB_IDS = (2001, 2002, 2003)

# Director, then billing slots 0, 1 and 2. Slot 2 is present in every payload and must never
# become a follow — it is the line between this cut and `follow_queries`' deeper `lead` one.
RATED_CREDITS = {
    2001: {"director": 100, "cast": [200, 201, 202]},
    2002: {"director": 100, "cast": [210, 211, 212]},
    2003: {"director": 101, "cast": [220, 221, 222]},
}
PROMOTED_PEOPLE = {100, 200, 201, 210, 211, 101, 220, 221}
EXCLUDED_PEOPLE = {202, 212, 222}


def _credits(director_id: int, cast_ids: list[int]) -> dict:
    return {
        "crew": [
            {
                "id": director_id,
                "name": f"Director {director_id}",
                "credit_id": f"crew-{director_id}",
                "department": "Directing",
                "job": "Director",
            }
        ],
        "cast": [
            {
                "id": person_id,
                "name": f"Actor {person_id}",
                "credit_id": f"cast-{person_id}",
                "order": order,
            }
            for order, person_id in enumerate(cast_ids)
        ],
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
    for tmdb_id in WATCHLIST_TMDB_IDS:
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(
                200, json=make_details(tmdb_id, credits=_credits(900 + tmdb_id, [910 + tmdb_id]))
            )
        )
    for tmdb_id, roles in RATED_CREDITS.items():
        respx.get(f"{BASE_URL}/movie/{tmdb_id}").mock(
            return_value=httpx.Response(
                200,
                json=make_details(
                    tmdb_id, credits=_credits(roles["director"], list(roles["cast"]))
                ),
            )
        )


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


# --- the whole export ----------------------------------------------------------------------


@respx.mock
async def test_a_fixture_export_produces_the_expected_follows_watchlist_and_report(
    session, session_factory, user, export
):
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.status == "succeeded"
    assert job.rows_done == job.rows_total == len(WATCHLIST) + len(RATINGS)
    assert job.error is None

    # Two of the three watchlist rows resolved; the third is the whole of the report.
    assert job.watchlist_created == 2
    assert job.unmatched == [
        {"name": "A Film That Does Not Exist", "year": 1999, "kind": "watchlist"}
    ]

    # Two title follows — the watchlist films, counted as `watchlist_created` above because a
    # title follow is what a watchlist row has become (M8) — plus the distinct people of the
    # three promoted ratings. `Heat` and `The Insider` share a director, which is counted once:
    # the follow is idempotent.
    follows = await _rows(session, Follow)
    assert job.follows_created == len(PROMOTED_PEOPLE)
    assert len(follows) == 2 + len(PROMOTED_PEOPLE)
    assert {f.source for f in follows} == {"letterboxd_import"}
    assert {f.entity_id for f in follows if f.entity_type == "person"} == {
        str(p) for p in PROMOTED_PEOPLE
    }


@respx.mock
async def test_a_rated_film_contributes_people_but_never_a_catalog_row(
    session, session_factory, user, export
):
    # The catalog is the upcoming-film spine: a film rated years ago has nothing left to
    # announce, so it is fetched for its credits and discarded (spec §3).
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    films = {f.tmdb_id for f in await _rows(session, Film)}
    assert films == set(WATCHLIST_TMDB_IDS)
    assert films.isdisjoint(RATED_TMDB_IDS)

    people = {p.id for p in await _rows(session, Person)}
    assert PROMOTED_PEOPLE <= people


@respx.mock
async def test_billing_below_the_top_two_is_not_followed(session, session_factory, user, export):
    # Slot 2 is in every rated payload. A third-billed role in a film somebody enjoyed is not
    # evidence they want that actor's next project.
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    followed = {f.entity_id for f in await _rows(session, Follow) if f.entity_type == "person"}
    assert followed.isdisjoint({str(p) for p in EXCLUDED_PEOPLE})


@respx.mock
async def test_a_rating_below_the_cut_costs_no_request_and_is_not_reported(
    session, session_factory, user, export
):
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    searched = {
        call.request.url.params.get("query")
        for call in respx.calls
        if "/search/movie" in str(call.request.url)
    }
    assert searched.isdisjoint({"Blackhat", "Miami Vice", "Ali"})
    # They are skipped, not unmatched: reporting them would bury the one title the user has to
    # act on under their whole three-star history.
    assert [row["name"] for row in job.unmatched] == ["A Film That Does Not Exist"]


@respx.mock
async def test_a_watchlisted_film_becomes_a_title_follow_carrying_the_import_source(
    session, session_factory, user, export
):
    """One row per watchlist film, not two (D-1414.9). The store preference it used to carry is
    one setting per user now (D-44), so there is nothing else for the import to write."""
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    titles = [f for f in await _rows(session, Follow) if f.entity_type == "title"]
    assert len(titles) == 2
    assert {f.source for f in titles} == {"letterboxd_import"}
    assert {f.coverage for f in titles} == {"lead"}


# --- running it twice ----------------------------------------------------------------------


@respx.mock
async def test_re_uploading_the_same_export_creates_nothing_new(
    session, session_factory, user, export
):
    _mock_tmdb()
    first = await _run(session, session_factory, user, export)
    before = len(await _rows(session, Follow))

    second = await _run(session, session_factory, user, export)

    assert second.status == "succeeded"
    assert (second.follows_created, second.watchlist_created) == (0, 0)
    assert len(await _rows(session, Follow)) == before
    # The report is not idempotency-dependent: the title is still unplaceable.
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
        if any(f"/movie/{tmdb_id}" in str(call.request.url) for tmdb_id in WATCHLIST_TMDB_IDS)
    ]
    assert details_calls == []


# --- the mute ------------------------------------------------------------------------------


@respx.mock
async def test_an_import_follows_a_muted_film_and_leaves_the_mute_alone(
    session, session_factory, user, export
):
    """Both facts are the user's and the import overrules neither (D-1414.9): they listed the
    film, so the follow is created; they silenced it, so it stays silent and shows on
    `/me/watchlist` as `muted: true` for them to undo."""
    film = await add_film(session, tmdb_id=1001, title="Dune", slug="dune")
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()

    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.watchlist_created == 2
    assert str(film.id) in {f.entity_id for f in await _rows(session, Follow)}
    assert [m.film_id for m in await _rows(session, WatchlistDismissal)] == [film.id]


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
    assert len(await _rows(session, Follow)) == 1
