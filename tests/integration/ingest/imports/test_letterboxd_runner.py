"""The Letterboxd import runner (D-15): what a fixture export turns into, and what a second
run of the same export does not.

The acceptance criteria of `NEU-1356-letterboxd-import.md`, as EF-20, EF-21 and EF-22 leave
them — the full export, the alert window, the review list, the re-upload, and the crash —
driven through `import_letterboxd` against a respx-mocked TMDB.

**The run writes no follow at all** (EF-22): it stops at `awaiting_review` with a candidate per
matched film, and the follows are the confirm's (`ingest.imports.review`), which the re-upload
cases drive to show what a second import adds. **And no person follow, ever** (EF-20): the
ratings path that wrote them is gone, and the assertion is here so that nothing quietly grows a
second one back."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from tests.fixtures.tmdb import make_details
from upmovies.app.models import Follow, ImportCandidate, ImportJob
from upmovies.app.repos import import_job_repo
from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.ingest.imports.letterboxd import parse_upload
from upmovies.ingest.imports.review import confirm, discard_unconfirmed
from upmovies.ingest.imports.runner import import_letterboxd, run_letterboxd_import
from upmovies.ingest.tmdb.client import TMDBClient

BASE_URL = get_settings().tmdb_base_url.rstrip("/")

# --- the fixture export --------------------------------------------------------------------
#
# Five watchlist rows: two films still inside the alert window, two outside it (EF-21) — one
# whose year alone says so, one only its search hit's date gives away (NEU-1505) — and one
# title TMDB has never heard of. The ratings file rides along in the zip and must contribute
# nothing at all.
#
# The years are relative to today, because the window is: `NEXT_YEAR` is certainly inside it,
# and `RECENT_YEAR` is the year before the window starts — close enough that the export's year
# cannot rule it out (the festival slack reaches into the window's first year), far enough that
# a hit dated June of it is always past the 365-day ceiling.

WINDOW_START = datetime.now(UTC).date() - timedelta(days=get_settings().provider_poll_max_age_days)
NEXT_YEAR = datetime.now(UTC).year + 1
RECENT_YEAR = WINDOW_START.year - 1

WATCHLIST = [
    ("Dune", NEXT_YEAR),
    ("Arrival", NEXT_YEAR),
    ("Long Gone", 2019),
    ("Recently Gone", RECENT_YEAR),
    ("A Film That Does Not Exist", NEXT_YEAR),
]
RATINGS = [("Heat", 1995, 5.0), ("The Insider", 1999, 4.5), ("Blackhat", 2015, 2.0)]

SEARCH_HITS = {
    "Dune": (1001, NEXT_YEAR),
    "Arrival": (1002, NEXT_YEAR),
    "Long Gone": (1003, 2019),
    "Recently Gone": (1004, RECENT_YEAR),
    # The rated titles resolve too, so that a request spent on one would be a *visible*
    # failure rather than a silent unmatched row.
    "Heat": (2001, 1995),
    "The Insider": (2002, 1999),
}

IN_WINDOW_TMDB_IDS = (1001, 1002)
OUTSIDE_WINDOW_TMDB_ID = 1003
RECENTLY_GONE_TMDB_ID = 1004
RATED_TMDB_IDS = (2001, 2002)

OUTSIDE_WINDOW_REPORT = [
    {"name": "Long Gone", "year": 2019, "kind": "outside_window"},
    {"name": "Recently Gone", "year": RECENT_YEAR, "kind": "outside_window"},
]

# Every film TMDB answers for, and what it says about the two things the window reads. An
# undated, `Post Production` film is inside the window; a 2019 `Released` one is past the
# 365-day ceiling and outside it.
DETAILS: dict[int, tuple[str, str]] = {
    1001: ("2099-06-01", "Post Production"),
    1002: ("2099-09-01", "Post Production"),
    1003: ("2019-06-01", "Released"),
    1004: (f"{RECENT_YEAR}-06-01", "Released"),
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


def _requested(path: str) -> list:
    return [call for call in respx.calls if str(call.request.url).startswith(f"{BASE_URL}{path}")]


def _searched() -> set[str | None]:
    return {call.request.url.params.get("query") for call in _requested("/search/movie")}


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
    # What the upload route does before it opens a job: a list still waiting on review is
    # superseded, or the one-active-import index would refuse the second run of these tests.
    await discard_unconfirmed(session, user_id=user.id)
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


async def _candidates(session, job: ImportJob) -> dict[int, ImportCandidate]:
    rows = await session.execute(
        select(ImportCandidate)
        .where(ImportCandidate.job_id == job.id)
        .execution_options(populate_existing=True)
    )
    return {c.tmdb_id: c for c in rows.scalars().all()}


async def _confirm_all(session, user, job: ImportJob) -> ImportJob:
    """Confirm every selectable row, the way the review list opens: everything ticked."""
    ids = [c.film_id for c in (await _candidates(session, job)).values() if c.selected]
    return await confirm(session, user_id=user.id, job_id=job.id, film_ids=ids)


# --- the whole export ----------------------------------------------------------------------


@respx.mock
async def test_a_fixture_export_stops_at_review_with_a_candidate_per_matched_film(
    session, session_factory, user, export
):
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.status == "awaiting_review"
    # Not finished from the user's side: the confirm stamps it.
    assert job.finished_at is None
    # The ratings are not rows the runner steps through any more: `rows_total` is the watchlist.
    assert job.rows_done == job.rows_total == len(WATCHLIST)
    assert job.error is None

    # The two films inside the window are the list, both ticked. The report holds the two the
    # import declined as too old and the one it could not place, in export order.
    candidates = await _candidates(session, job)
    assert {t: c.selected for t, c in candidates.items()} == {1001: True, 1002: True}
    assert job.unmatched == [
        *OUTSIDE_WINDOW_REPORT,
        {"name": "A Film That Does Not Exist", "year": NEXT_YEAR, "kind": "watchlist"},
    ]
    # `watchlist_created` is what the list offers; `follows_created` waits for the confirm.
    assert (job.watchlist_created, job.follows_created) == (2, 0)


@respx.mock
async def test_the_run_writes_no_follow_until_the_list_is_confirmed(
    session, session_factory, user, export
):
    """EF-22: nothing is followed until the user has seen the list."""
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)
    assert await _follows(session) == []

    confirmed = await _confirm_all(session, user, job)

    assert confirmed.status == "succeeded"
    assert confirmed.follows_created == 2
    follows = await _follows(session)
    assert {f.entity_type for f in follows} == {"title"}
    assert {f.source for f in follows} == {"letterboxd_import"}
    assert len(follows) == 2


@respx.mock
async def test_an_import_never_writes_a_person_follow(session, session_factory, user, export):
    """EF-20, and the whole of why NEU-1448 exists. A follow is binary (EF-1), so a person
    follow inferred from a four-star rating would push every credit change of somebody the user
    once enjoyed at them. Asserted after the confirm, which is where follows are written now."""
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)
    await _confirm_all(session, user, job)

    assert [f for f in await _follows(session) if f.entity_type != "title"] == []


@respx.mock
async def test_the_ratings_cost_no_requests_and_produce_no_report_rows(
    session, session_factory, user, export
):
    # Every rated title in the fixture resolves, so a request spent on one would show up here
    # rather than passing as a search that found nothing.
    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert _searched().isdisjoint({name for name, _, _ in RATINGS})
    assert {row["name"] for row in job.unmatched}.isdisjoint({name for name, _, _ in RATINGS})
    assert {f.tmdb_id for f in await _rows(session, Film)}.isdisjoint(RATED_TMDB_IDS)
    assert set(await _candidates(session, job)).isdisjoint(RATED_TMDB_IDS)


# --- the candidate row ---------------------------------------------------------------------


@respx.mock
async def test_a_candidate_carries_the_catalogs_title_and_headline_release(
    session, session_factory, user
):
    """The catalog's title rather than the export's — the fixture TMDB calls 1001 `Movie 1001`
    while the export says `Dune` — because a search rule placed this row, and the catalog's
    title is how the user catches a wrong match before it becomes a follow."""
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR)]))
    _mock_tmdb()

    job = await _run(session, session_factory, user, export)

    film = (await session.execute(select(Film).where(Film.tmdb_id == 1001))).scalar_one()
    (candidate,) = (await _candidates(session, job)).values()
    assert (candidate.film_id, candidate.tmdb_id) == (film.id, 1001)
    assert candidate.title == "Movie 1001"
    # No release rows in the payload, so the primary date is the headline, unconfirmed.
    assert candidate.headline_release == {
        "date": "2099-06-01",
        "kind": "primary",
        "country": None,
        "bucket": None,
    }
    assert candidate.selected
    assert (job.watchlist_created, job.follows_created) == (1, 0)
    assert job.unmatched == []


@respx.mock
async def test_two_rows_that_resolve_to_one_film_are_one_candidate(session, session_factory, user):
    # Two titles in the export, one film at TMDB: one proposal, counted once.
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR), ("Dune Part One", NEXT_YEAR)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/search/movie", params={"query": "Dune Part One"}).mock(
        return_value=httpx.Response(
            200,
            json={
                "page": 1,
                "total_pages": 1,
                "total_results": 1,
                "results": [
                    {
                        "id": 1001,
                        "title": "Dune Part One",
                        "release_date": f"{NEXT_YEAR}-06-01",
                        "popularity": 10.0,
                    }
                ],
            },
        )
    )

    job = await _run(session, session_factory, user, export)

    assert list(await _candidates(session, job)) == [1001]
    assert job.watchlist_created == 1


# --- the alert window ----------------------------------------------------------------------


@respx.mock
async def test_a_row_whose_year_is_certainly_outside_the_window_makes_no_request(
    session, session_factory, user
):
    """NEU-1505, D-1505.6. A 2019 row can only match a film dated 2020 at the latest (the
    festival slack), which is past the 365-day `PROVIDER_POLL_MAX_AGE_DAYS` ceiling, so the
    search would be spent to learn nothing. Reported by the export's name and year."""
    export = parse_upload(watchlist_csv([("Long Gone", 2019)]))
    _mock_tmdb()

    job = await _run(session, session_factory, user, export)

    assert respx.calls.call_count == 0
    assert job.unmatched == [{"name": "Long Gone", "year": 2019, "kind": "outside_window"}]
    assert await _candidates(session, job) == {}


@respx.mock
async def test_a_hit_dated_outside_the_window_is_declined_before_its_details_request(
    session, session_factory, user
):
    # The year leaves room for a film inside the window, so the row is searched; the hit's
    # own date then rules it out, and no `/movie/{id}` is spent on it.
    export = parse_upload(watchlist_csv([("Recently Gone", RECENT_YEAR)]))
    _mock_tmdb()

    job = await _run(session, session_factory, user, export)

    assert _searched() == {"Recently Gone"}
    assert _requested(f"/movie/{RECENTLY_GONE_TMDB_ID}") == []
    assert job.unmatched == [
        {"name": "Recently Gone", "year": RECENT_YEAR, "kind": "outside_window"}
    ]
    assert await _candidates(session, job) == {}


@respx.mock
async def test_a_film_outside_the_window_is_not_upserted(session, session_factory, user, export):
    """Inverted by NEU-1505: a film nobody can follow from here is not worth a catalog row, and
    holding one is what let its admission card a followed director."""
    _mock_tmdb()
    await _run(session, session_factory, user, export)

    stored = {f.tmdb_id for f in await _rows(session, Film)}
    assert stored.isdisjoint({OUTSIDE_WINDOW_TMDB_ID, RECENTLY_GONE_TMDB_ID})


@respx.mock
async def test_a_released_film_already_in_the_catalog_is_declined_too(
    session, session_factory, user
):
    """D-1505.6's "already in the catalog" half, which NEU-1508's purge relies on: the hit's
    date declines the row before anything looks at the catalog, so a stored released film is
    not proposed, not re-fetched and not offered — and a purged one could not come back."""
    await add_film(
        session,
        RECENTLY_GONE_TMDB_ID,
        release_date=datetime.fromisoformat(DETAILS[RECENTLY_GONE_TMDB_ID][0]).date(),
        status="Released",
    )
    await session.commit()
    export = parse_upload(watchlist_csv([("Recently Gone", RECENT_YEAR)]))
    _mock_tmdb()

    job = await _run(session, session_factory, user, export)

    assert _searched() == {"Recently Gone"}
    assert _requested(f"/movie/{RECENTLY_GONE_TMDB_ID}") == []
    assert job.unmatched == [
        {"name": "Recently Gone", "year": RECENT_YEAR, "kind": "outside_window"}
    ]
    assert await _candidates(session, job) == {}


@respx.mock
async def test_a_canceled_film_is_skipped_however_recent_its_date(session, session_factory, user):
    # The window's status term is `Canceled` alone (NEU-1417) — a film called off next year
    # has a date well inside the ceiling and still nothing to say.
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR)]))
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

    # The date let it through to the fetch; the fetched status declines it, the same way, and
    # before the film is written (NEU-1510).
    assert 1001 not in {f.tmdb_id for f in await _rows(session, Film)}
    assert await _candidates(session, job) == {}
    assert job.unmatched == [{"name": "Dune", "year": NEXT_YEAR, "kind": "outside_window"}]
    assert job.watchlist_created == 0


@respx.mock
async def test_a_hit_whose_details_disagree_across_the_window_line_is_not_written(
    session, session_factory, user
):
    """NEU-1510: the hit's date is inside the window, so the row is fetched, but `/movie/{id}`
    puts the film's primary date years back. The details decide, before the upsert: nothing
    enters the catalog, and the row is reported by the export's name and year."""
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(
        return_value=httpx.Response(
            200,
            json=make_details(
                1001, release_date="2019-06-01", status="Released", credits=_credits(1001)
            ),
        )
    )

    job = await _run(session, session_factory, user, export)

    assert len(_requested("/movie/1001")) == 1
    assert 1001 not in {f.tmdb_id for f in await _rows(session, Film)}
    assert await _candidates(session, job) == {}
    assert job.unmatched == [{"name": "Dune", "year": NEXT_YEAR, "kind": "outside_window"}]


@respx.mock
async def test_a_film_with_no_release_date_is_inside_the_window(session, session_factory, user):
    # The NULL guards in `alert_window_clause` are the point: an undated film is the most
    # upcoming thing there is, and dropping it would be exactly backwards.
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1001").mock(
        return_value=httpx.Response(
            200,
            json=make_details(1001, release_date=None, status="Rumored", credits=_credits(1001)),
        )
    )

    job = await _run(session, session_factory, user, export)

    (candidate,) = (await _candidates(session, job)).values()
    assert candidate.selected
    # Nothing to lead with at all, and the row says so rather than inventing a date.
    assert candidate.headline_release is None
    assert job.unmatched == []


# --- running it twice ----------------------------------------------------------------------


@respx.mock
async def test_re_uploading_the_same_export_creates_nothing_new(
    session, session_factory, user, export
):
    _mock_tmdb()
    first = await _run(session, session_factory, user, export)
    await _confirm_all(session, user, first)
    before = len(await _follows(session))

    second = await _run(session, session_factory, user, export)

    assert second.status == "awaiting_review"
    # The same list, offered again: an already-followed film is still a film on the watchlist.
    assert set(await _candidates(session, second)) == set(IN_WINDOW_TMDB_IDS)
    await _confirm_all(session, user, second)
    assert len(await _follows(session)) == before
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
        if any(f"/movie/{tmdb_id}" in str(call.request.url) for tmdb_id in IN_WINDOW_TMDB_IDS)
    ]
    assert details_calls == []


@respx.mock
async def test_a_second_run_supersedes_the_first_list(session, session_factory, user, export):
    """EF-22: starting another import discards an unconfirmed one — `failed`, `superseded`, and
    its candidates gone — rather than being refused."""
    _mock_tmdb()
    first = await _run(session, session_factory, user, export)

    second = await _run(session, session_factory, user, export)

    first = await session.get(ImportJob, first.id, populate_existing=True)
    assert (first.status, first.error) == ("failed", "superseded")
    assert first.finished_at is not None
    assert await _candidates(session, first) == {}
    assert second.status == "awaiting_review"
    assert len(await _candidates(session, second)) == 2


# --- the mute ------------------------------------------------------------------------------


@respx.mock
async def test_an_import_proposes_a_film_the_catalog_already_holds(
    session, session_factory, user, export
):
    """The follow is the whole of what a confirmed row writes (EF-14): the mute it used to have
    to leave alone (D-1414.9) went with the watchlist it corrected."""
    film = await add_film(session, tmdb_id=1001, title="Dune", slug="dune")
    await session.commit()

    _mock_tmdb()
    job = await _run(session, session_factory, user, export)

    assert job.watchlist_created == 2
    assert (await _candidates(session, job))[1001].film_id == film.id


# --- the crash -----------------------------------------------------------------------------


@respx.mock
async def test_each_row_is_committed_as_it_is_done(session, session_factory, user):
    """The commit-per-row contract, candidates included: a crash on row two leaves row one's
    film *and* its candidate committed, visible to a session that is not the runner's."""
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR), ("Arrival", NEXT_YEAR)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1002").mock(return_value=httpx.Response(500))

    job = await _queue(session, user, export)
    with pytest.raises(Exception):  # noqa: B017 — any propagated failure is the crash
        async with _client() as client:
            await import_letterboxd(
                session_factory=session_factory, client=client, job_id=job.id, export=export
            )

    assert {f.tmdb_id for f in await _rows(session, Film)} == {1001}
    assert list(await _candidates(session, job)) == [1001]


@respx.mock
async def test_a_crash_mid_run_fails_the_job_and_keeps_the_rows_already_done(
    session, session_factory, user
):
    # `run_letterboxd_import` rather than `import_letterboxd`: the wrapper's `except` is the
    # thing under test. Nothing awaits the task in production, so an exception escaping it
    # would leave the job polling `running` forever.
    export = parse_upload(watchlist_csv([("Dune", NEXT_YEAR), ("Arrival", NEXT_YEAR)]))
    _mock_tmdb()
    respx.get(f"{BASE_URL}/movie/1002").mock(return_value=httpx.Response(500))

    job = await _queue(session, user, export)
    await run_letterboxd_import(
        job.id, export, get_settings().model_copy(update={"tmdb_retry_max_attempts": 1})
    )

    finished = await session.get(ImportJob, job.id, populate_existing=True)
    assert finished.status == "failed"
    assert finished.error
    # The first row's film survived whole: commit-per-row is what makes a partial import worth
    # keeping, and the alternative — one transaction for the job — would discard it.
    assert {f.tmdb_id for f in await _rows(session, Film)} == {1001}
    # Its candidate did not: a failed job is never confirmed, and half a library offered as a
    # list would look like the whole of one.
    assert await _candidates(session, job) == {}
    assert await _follows(session) == []
