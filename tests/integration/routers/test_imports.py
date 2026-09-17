"""`/me/import` (D-15): the upload's synchronous validation and the job poll, behind the
entitlement gate (D-39).

The runner itself is covered in `tests/integration/ingest/imports/` — here the task is
stubbed out, because what these tests are about is what the *route* decides before anything is
spawned."""

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import select

from tests.fixtures.letterboxd import export_zip, ratings_csv, watchlist_csv
from upmovies.app.models import ImportJob
from upmovies.routers.imports import MAX_UPLOAD_BYTES

WATCHLIST = watchlist_csv([("Dune", 2021), ("Arrival", 2016)])
RATINGS = ratings_csv([("Heat", 1995, 5.0)])
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
    return list((await session.execute(select(ImportJob))).scalars().all())


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
    assert job.rows_total == 3  # two watchlist rows plus the one rating
    assert job.user_id == entitled_client.user.id
    spawned.assert_awaited_once()


async def test_a_bare_csv_is_accepted_on_its_own(entitled_client, session, spawned):
    r = await entitled_client.post("/me/import/letterboxd", files=_upload(WATCHLIST, "w.csv"))
    assert r.status_code == 202
    (job,) = await _jobs(session)
    assert job.rows_total == 2


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
    assert body["rows_total"] == 3
    assert (body["rows_done"], body["watchlist_created"], body["follows_created"]) == (0, 0, 0)
    assert body["unmatched"] == []
    assert body["error"] is None
    assert body["finished_at"] is None


async def test_the_unmatched_report_is_rendered_for_the_poller(entitled_client, session, spawned):
    await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    (job,) = await _jobs(session)
    job.unmatched = [{"name": "A Film", "year": 1999, "kind": "watchlist"}]
    job.status = "succeeded"
    await session.commit()

    body = (await entitled_client.get(f"/me/import/{job.id}")).json()
    assert body["unmatched"] == [{"name": "A Film", "year": 1999, "kind": "watchlist"}]


async def test_polling_someone_elses_job_is_404(entitled_client, session, make_user, spawned):
    await entitled_client.post("/me/import/letterboxd", files=_upload(EXPORT))
    (job,) = await _jobs(session)

    other = await make_user(
        email="other@example.com", entitled_until=entitled_client.user.entitled_until
    )
    from tests.fixtures.users import _build_authed_client

    async with await _build_authed_client(session, other) as other_client:
        r = await other_client.get(f"/me/import/{job.id}")

    # 404 rather than 403: confirming the id exists is an answer this route has no reason to
    # give to someone who does not own it.
    assert r.status_code == 404
    assert r.json()["detail"] == "import_job_not_found"


async def test_polling_a_job_that_does_not_exist_is_404(entitled_client):
    r = await entitled_client.get(f"/me/import/{uuid4()}")
    assert r.status_code == 404
