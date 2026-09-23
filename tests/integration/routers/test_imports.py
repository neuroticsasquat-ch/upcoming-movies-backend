"""`/me/import` (D-15): the upload's synchronous validation, the job poll and the review
list's confirm (EF-22), behind the entitlement gate (D-39).

The runner itself is covered in `tests/integration/ingest/imports/` — here the task is
stubbed out, because what these tests are about is what the *route* decides before anything is
spawned."""

from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from tests.fixtures.users import _build_authed_client
from upmovies.app.models import Follow, ImportCandidate, ImportJob
from upmovies.app.repos import import_candidate_repo, import_job_repo
from upmovies.catalog.models import Film
from upmovies.routers.imports import MAX_UPLOAD_BYTES

WATCHLIST = watchlist_csv([("Dune", 2021), ("Arrival", 2016)])
RATINGS = ratings_csv([("Heat", 1995, 5.0)])
# A real export carries both. Only the watchlist is read (EF-20), so `rows_total` counts two.
EXPORT = export_zip({"watchlist.csv": WATCHLIST, "ratings.csv": RATINGS})


def _upload(data: bytes, filename: str = "letterboxd.zip") -> dict:
    return {"file": (filename, data, "application/octet-stream")}


@pytest.fixture
def spawned():
    """The background task, stubbed. Nothing awaits it in production either, so letting the
    real one loose here would race the assertions and hit TMDB."""
    with patch("upmovies.routers.imports.run_letterboxd_import", new=AsyncMock()) as task:
        yield task


async def _jobs(session) -> list[ImportJob]:
    return list(
        (await session.execute(select(ImportJob).execution_options(populate_existing=True)))
        .scalars()
        .all()
    )


async def _follows(session) -> list[Follow]:
    return list((await session.execute(select(Follow))).scalars().all())


async def _candidates(session) -> list[ImportCandidate]:
    return list((await session.execute(select(ImportCandidate))).scalars().all())


async def _awaiting_review(
    session, user, *, source: str = "letterboxd"
) -> tuple[ImportJob, dict[str, Film]]:
    """A job the runner has finished with, its list as the runner writes it: two films inside
    the alert window, ticked, and one outside it, unticked with its reason."""
    job = await import_job_repo.create(session, user_id=user.id, source=source, rows_total=3)
    films = {
        "zodiac": await add_film(session, tmdb_id=3001, title="Zodiac", slug="zodiac"),
        "arrival": await add_film(session, tmdb_id=3002, title="Arrival", slug="arrival"),
        "gone": await add_film(session, tmdb_id=3003, title="Long Gone", slug="long-gone"),
    }
    for key, film in films.items():
        await import_candidate_repo.add(
            session,
            job_id=job.id,
            film_id=film.id,
            tmdb_id=film.tmdb_id,
            title=film.title,
            headline_release=None,
            skip_reason="outside_window" if key == "gone" else None,
        )
    job.status = "awaiting_review"
    job.watchlist_created = 2
    await session.commit()
    return job, films


# --- the gate ------------------------------------------------------------------------------


async def test_upload_requires_auth(client):
    r = await client.post("/me/import/letterboxd", files=_upload(EXPORT))
    assert r.status_code == 401


async def test_upload_is_403_for_an_unentitled_user(authed_client, session, spawned):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in.
    # Importing is subscriber functionality and closed by default (D-37).
    r = await authed_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
    # The gate sits in front of the enqueue, so no quota is spent and no job exists (D-39).
    assert await _jobs(session) == []
    spawned.assert_not_called()


async def test_polling_is_403_for_an_unentitled_user(authed_client):
    r = await authed_client.get(f"/me/import/{uuid4()}")
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


# --- the upload ----------------------------------------------------------------------------


async def test_a_valid_export_is_accepted_and_queued(entitled_client, session, spawned):
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))

    assert r.status_code == 202
    (job,) = await _jobs(session)
    assert r.json() == {"job_id": str(job.id)}
    assert job.status == "queued"
    assert job.source == "letterboxd"
    assert job.rows_total == 2  # the two watchlist rows; the rating is not a row any more
    assert job.user_id == entitled_client.user.id
    spawned.assert_awaited_once()


async def test_a_bare_csv_is_accepted_on_its_own(entitled_client, session, spawned):
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(WATCHLIST, "w.csv"))
    assert r.status_code == 202
    (job,) = await _jobs(session)
    assert job.rows_total == 2


async def test_a_bare_ratings_csv_is_422_rather_than_imported_as_a_watchlist(
    entitled_client, session, spawned
):
    """EF-20 at the route. `ratings.csv` has the same `Name` and `Year` columns a watchlist
    does, so this is the difference between a 422 and importing somebody's entire viewing
    history as films they mean to see."""
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(RATINGS, "r.csv"))
    assert r.status_code == 422
    assert "already watched" in r.json()["detail"]
    assert await _jobs(session) == []
    spawned.assert_not_called()


async def test_a_file_with_the_wrong_header_is_422_with_a_reason(entitled_client, session, spawned):
    r = await entitled_client.post(
        "/me/import/letterboxd", files=_upload(b"a,b,c\n1,2,3\n", "notes.csv")
    )
    assert r.status_code == 422
    assert "Name" in r.json()["detail"]
    # Refused before the enqueue: the uploader learns what is wrong while still looking at it.
    assert await _jobs(session) == []
    spawned.assert_not_called()


async def test_a_request_with_no_file_field_is_422(entitled_client, spawned):
    r = await entitled_client.post("/me/import/letterboxd", data={"not_a_file": "x"})
    assert r.status_code == 422
    spawned.assert_not_called()


async def test_an_upload_past_the_cap_is_413(entitled_client, session, spawned):
    # Starlette's own part cap is 1 MB, well under the 5 MB export this accepts, which is why
    # the route reads the form itself rather than declaring an `UploadFile`.
    oversized = b"Date,Name,Year\n" + b"x" * (MAX_UPLOAD_BYTES + 1)
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(oversized, "big.csv"))
    assert r.status_code == 413
    assert r.json()["detail"] == "import_file_too_large"
    assert await _jobs(session) == []
    spawned.assert_not_called()


async def test_a_second_upload_while_one_runs_is_409(entitled_client, session, spawned):
    assert (
        await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    ).status_code == 202
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))

    assert r.status_code == 409
    assert r.json()["detail"] == "import_in_progress"
    assert len(await _jobs(session)) == 1


async def test_a_second_upload_after_the_first_finished_is_accepted(
    entitled_client, session, spawned
):
    assert (
        await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    ).status_code == 202
    (job,) = await _jobs(session)
    job.status = "succeeded"
    await session.commit()

    r = await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    assert r.status_code == 202
    assert len(await _jobs(session)) == 2


# --- the poll ------------------------------------------------------------------------------


async def test_polling_returns_the_job_row(entitled_client, session, spawned):
    started = await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    job_id = started.json()["job_id"]

    r = await entitled_client.get(f"/me/import/{job_id}")

    assert r.status_code == 200
    body = r.json()
    assert body["id"] == job_id
    assert body["status"] == "queued"
    assert body["rows_total"] == 2
    assert (body["rows_done"], body["watchlist_created"], body["follows_created"]) == (0, 0, 0)
    assert body["unmatched"] == []
    assert body["candidates"] == []
    assert body["error"] is None
    assert body["finished_at"] is None


@pytest.mark.parametrize("kind", ["watchlist", "tmdb_missing", "rating"])
async def test_the_unmatched_report_is_rendered_for_the_poller(
    entitled_client, session, spawned, kind
):
    """Every failure kind survives the round trip, `rating` included: nothing writes it any
    more (EF-20), but `app.import_job.unmatched` is JSONB and jobs that ran before M5 still
    hold rows carrying it — polling one of those must not 500 on its own report."""
    await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    (job,) = await _jobs(session)
    job.unmatched = [{"name": "A Film", "year": 1999, "kind": kind}]
    job.status = "succeeded"
    await session.commit()

    body = (await entitled_client.get(f"/me/import/{job.id}")).json()
    assert body["unmatched"] == [{"name": "A Film", "year": 1999, "kind": kind}]


async def test_a_historical_outside_window_row_is_not_reported_as_unmatched(
    entitled_client, session, spawned
):
    """Jobs that ran between NEU-1448 and EF-22 stored a skipped film in `unmatched` with
    `kind=outside_window`. Matched films are candidates now, so the row is dropped on the way
    out rather than 500-ing the poll or telling the user a matched film could not be found."""
    await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    (job,) = await _jobs(session)
    job.unmatched = [
        {"name": "Long Gone", "year": 2019, "kind": "outside_window"},
        {"name": "A Film", "year": 1999, "kind": "watchlist"},
    ]
    job.status = "succeeded"
    await session.commit()

    body = (await entitled_client.get(f"/me/import/{job.id}")).json()

    assert body["unmatched"] == [{"name": "A Film", "year": 1999, "kind": "watchlist"}]
    assert "skipped" not in body


async def test_polling_someone_elses_job_is_404(entitled_client, session, make_user, spawned):
    await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    (job,) = await _jobs(session)

    other = await make_user(
        email="other@example.com", entitled_until=entitled_client.user.entitled_until
    )

    async with await _build_authed_client(session, other) as other_client:
        r = await other_client.get(f"/me/import/{job.id}")

    # 404 rather than 403: confirming the id exists is an answer this route has no reason to
    # give to someone who does not own it.
    assert r.status_code == 404
    assert r.json()["detail"] == "import_job_not_found"


async def test_polling_a_job_that_does_not_exist_is_404(entitled_client):
    r = await entitled_client.get(f"/me/import/{uuid4()}")
    assert r.status_code == 404


# --- the review list (EF-22) ---------------------------------------------------------------


async def test_polling_a_job_awaiting_review_returns_its_candidates(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user)

    body = (await entitled_client.get(f"/me/import/{job.id}")).json()

    assert body["status"] == "awaiting_review"
    assert (body["watchlist_created"], body["follows_created"]) == (2, 0)
    # Ticked rows first, then the greyed ones, each group by title.
    assert body["candidates"] == [
        {
            "film_id": str(films["arrival"].id),
            "tmdb_id": 3002,
            "title": "Arrival",
            "headline_release": None,
            "selected": True,
            "skip_reason": None,
        },
        {
            "film_id": str(films["zodiac"].id),
            "tmdb_id": 3001,
            "title": "Zodiac",
            "headline_release": None,
            "selected": True,
            "skip_reason": None,
        },
        {
            "film_id": str(films["gone"].id),
            "tmdb_id": 3003,
            "title": "Long Gone",
            "headline_release": None,
            "selected": False,
            "skip_reason": "outside_window",
        },
    ]


async def test_a_candidates_headline_release_is_rendered(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user)
    candidate = (
        await session.execute(
            select(ImportCandidate).where(ImportCandidate.film_id == films["zodiac"].id)
        )
    ).scalar_one()
    candidate.headline_release = {
        "date": "2099-06-01",
        "kind": "upcoming",
        "country": "US",
        "bucket": "wide",
    }
    await session.commit()

    body = (await entitled_client.get(f"/me/import/{job.id}")).json()

    (zodiac,) = [c for c in body["candidates"] if c["title"] == "Zodiac"]
    assert zodiac["headline_release"] == {
        "date": "2099-06-01",
        "kind": "upcoming",
        "country": "US",
        "bucket": "wide",
    }


async def test_confirm_follows_only_the_films_the_user_kept(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm",
        # Zodiac kept, Arrival unticked, the skipped film sent anyway, and an id that is on no
        # list at all: only Zodiac is a selectable candidate among them.
        json={"film_ids": [str(films["zodiac"].id), str(films["gone"].id), str(uuid4())]},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["follows_created"] == 1
    assert body["finished_at"] is not None
    # The list is answered; the follows are the record now.
    assert body["candidates"] == []
    assert await _candidates(session) == []

    (follow,) = await _follows(session)
    assert (follow.user_id, follow.entity_type) == (entitled_client.user.id, "title")
    assert follow.entity_id == str(films["zodiac"].id)
    assert follow.source == "letterboxd_import"


async def test_confirm_writes_the_tmdb_import_source_for_a_tmdb_job(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user, source="tmdb")

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm", json={"film_ids": [str(films["arrival"].id)]}
    )

    assert r.status_code == 200
    (follow,) = await _follows(session)
    assert follow.source == "tmdb_import"


async def test_confirm_counts_a_kept_film_the_user_already_followed(entitled_client, session):
    """`follows_created` counts the confirmed rows (EF-22) — the user now follows both — and the
    existing follow is left as it was, `source` included (D-15)."""
    job, films = await _awaiting_review(session, entitled_client.user)
    session.add(
        Follow(
            user_id=entitled_client.user.id,
            entity_type="title",
            entity_id=str(films["zodiac"].id),
            source="manual",
        )
    )
    await session.commit()

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm",
        json={"film_ids": [str(films["zodiac"].id), str(films["arrival"].id)]},
    )

    assert r.json()["follows_created"] == 2
    follows = {f.entity_id: f.source for f in await _follows(session)}
    assert follows == {
        str(films["zodiac"].id): "manual",
        str(films["arrival"].id): "letterboxd_import",
    }


async def test_confirming_nothing_finishes_the_job_with_no_follows(entitled_client, session):
    job, _ = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.post(f"/me/import/{job.id}/confirm", json={"film_ids": []})

    assert (r.status_code, r.json()["status"], r.json()["follows_created"]) == (
        200,
        "succeeded",
        0,
    )
    assert await _follows(session) == []


async def test_confirming_twice_is_409(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user)
    body = {"film_ids": [str(films["zodiac"].id)]}
    assert (
        await entitled_client.post(f"/me/import/{job.id}/confirm", json=body)
    ).status_code == 200

    r = await entitled_client.post(f"/me/import/{job.id}/confirm", json=body)

    assert r.status_code == 409
    assert r.json()["detail"] == "import_not_awaiting_review"
    assert len(await _follows(session)) == 1


@pytest.mark.parametrize("job_status", ["queued", "running", "succeeded", "failed"])
async def test_confirming_a_job_not_awaiting_review_is_409(entitled_client, session, job_status):
    job, films = await _awaiting_review(session, entitled_client.user)
    job.status = job_status
    await session.commit()

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm", json={"film_ids": [str(films["zodiac"].id)]}
    )

    assert r.status_code == 409
    assert await _follows(session) == []


async def test_confirming_someone_elses_job_is_404(entitled_client, session, make_user):
    """404, as the poll answers — not the 409 the ticket words it as, which would tell a
    caller holding another user's job id that the job exists."""
    other = await make_user(
        email="other@example.com", entitled_until=entitled_client.user.entitled_until
    )
    job, films = await _awaiting_review(session, other)

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm", json={"film_ids": [str(films["zodiac"].id)]}
    )

    assert r.status_code == 404
    assert r.json()["detail"] == "import_job_not_found"
    assert await _follows(session) == []
    (still,) = await _jobs(session)
    assert still.status == "awaiting_review"


async def test_confirming_a_job_that_does_not_exist_is_404(entitled_client):
    r = await entitled_client.post(f"/me/import/{uuid4()}/confirm", json={"film_ids": []})
    assert r.status_code == 404


async def test_confirm_is_403_for_an_unentitled_user(authed_client, session):
    job, films = await _awaiting_review(session, authed_client.user)

    r = await authed_client.post(
        f"/me/import/{job.id}/confirm", json={"film_ids": [str(films["zodiac"].id)]}
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
    assert await _follows(session) == []


async def test_confirm_requires_the_csrf_header(entitled_client, session):
    job, films = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.post(
        f"/me/import/{job.id}/confirm",
        json={"film_ids": [str(films["zodiac"].id)]},
        headers={"X-CSRF-Token": "wrong"},
    )

    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"
    assert await _follows(session) == []


# --- a new import discards the list (EF-22) ------------------------------------------------


async def test_an_upload_while_a_list_awaits_review_supersedes_it(
    entitled_client, session, spawned
):
    old, _ = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))

    assert r.status_code == 202
    jobs = {j.id: j for j in await _jobs(session)}
    assert (jobs[old.id].status, jobs[old.id].error) == ("failed", "superseded")
    assert jobs[old.id].finished_at is not None
    assert jobs[UUID(r.json()["job_id"])].status == "queued"
    assert await _candidates(session) == []
    spawned.assert_awaited_once()

    # The superseded list cannot be confirmed afterwards.
    confirm = await entitled_client.post(f"/me/import/{old.id}/confirm", json={"film_ids": []})
    assert confirm.status_code == 409


async def test_a_bad_upload_does_not_cost_the_user_their_list(entitled_client, session, spawned):
    # The file is read before anything is discarded, so a 422 leaves the review list alone.
    old, _ = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.post("/me/import/letterboxd", files=_upload(RATINGS, "r.csv"))

    assert r.status_code == 422
    (job,) = await _jobs(session)
    assert (job.id, job.status) == (old.id, "awaiting_review")
    assert len(await _candidates(session)) == 3


async def test_another_users_list_is_not_superseded_by_an_upload(
    entitled_client, session, make_user, spawned
):
    other = await make_user(
        email="other@example.com", entitled_until=entitled_client.user.entitled_until
    )
    theirs, _ = await _awaiting_review(session, other)

    assert (
        await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    ).status_code == 202

    jobs = {j.id: j for j in await _jobs(session)}
    assert jobs[theirs.id].status == "awaiting_review"
    assert len(await _candidates(session)) == 3


# --- the open import (NEU-1453) ------------------------------------------------------------


async def test_the_open_import_is_204_when_the_user_never_imported(entitled_client):
    r = await entitled_client.get("/me/import/active")

    # Not 422: `active` reached its own route rather than the by-id read's UUID parser.
    assert r.status_code == 204
    assert r.content == b""


async def test_the_open_import_returns_a_list_awaiting_review_with_its_candidates(
    entitled_client, session
):
    job, films = await _awaiting_review(session, entitled_client.user)

    r = await entitled_client.get("/me/import/active")

    assert r.status_code == 200
    body = r.json()
    assert (body["id"], body["status"]) == (str(job.id), "awaiting_review")
    # Ticked and greyed rows alike, exactly as the by-id read renders them.
    assert [(c["film_id"], c["selected"]) for c in body["candidates"]] == [
        (str(films["arrival"].id), True),
        (str(films["zodiac"].id), True),
        (str(films["gone"].id), False),
    ]
    assert body == (await entitled_client.get(f"/me/import/{job.id}")).json()


async def test_the_open_import_returns_a_running_job_without_candidates(entitled_client, session):
    # Rows already written, so the empty list below is the status gate and not an empty table.
    job, _ = await _awaiting_review(session, entitled_client.user)
    job.status = "running"
    await session.commit()

    r = await entitled_client.get("/me/import/active")

    assert r.status_code == 200
    body = r.json()
    assert (body["id"], body["status"]) == (str(job.id), "running")
    assert body["candidates"] == []


@pytest.mark.parametrize(
    ("job_status", "error"),
    [("succeeded", None), ("failed", "import_failed"), ("failed", "superseded")],
)
async def test_a_finished_import_is_not_open(entitled_client, session, job_status, error):
    job, _ = await _awaiting_review(session, entitled_client.user)
    job.status = job_status
    job.error = error
    await session.commit()

    r = await entitled_client.get("/me/import/active")

    assert r.status_code == 204


async def test_the_open_import_is_never_another_users(entitled_client, session, make_user):
    other = await make_user(
        email="other@example.com", entitled_until=entitled_client.user.entitled_until
    )
    await _awaiting_review(session, other)

    r = await entitled_client.get("/me/import/active")

    assert r.status_code == 204


async def test_the_open_import_does_not_touch_the_job(entitled_client, session):
    job, _ = await _awaiting_review(session, entitled_client.user)

    for _ in range(2):
        assert (await entitled_client.get("/me/import/active")).status_code == 200

    (after,) = await _jobs(session)
    assert (after.id, after.status, after.finished_at) == (job.id, "awaiting_review", None)
    assert len(await _candidates(session)) == 3


async def test_the_open_import_requires_auth(client):
    r = await client.get("/me/import/active")
    assert r.status_code == 401


async def test_the_open_import_is_403_for_an_unentitled_user(authed_client):
    r = await authed_client.get("/me/import/active")
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"
