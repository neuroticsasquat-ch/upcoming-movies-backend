"""`/me/import`: uploading a library from another service, and polling the job it starts
(D-15), subscriber-only (D-39).

Three responsibilities and no more. The upload **validates synchronously and enqueues** — a
file this cannot read is a 422 the uploader can act on, and everything that survives that is
handed to a background task with a 202, because resolving a library against TMDB is minutes of
rate-limited requests (`ingest.imports.runner`). The poll — by id, or the caller's open import
without one (NEU-1453) — returns the job row, and its review list once the job reaches
`awaiting_review`. The confirm answers that list (EF-22) — for a TMDB account import as much as
a Letterboxd one, since the job is the same row either way.

The multipart body is read by hand rather than declared as `UploadFile`, which is the one
unusual thing here and is deliberate. The spec's amendment requires the entitlement gate in
front of the upload — "403 on the first touch" — and FastAPI reads the form *before* it solves
a route's dependencies, so a declared file parameter is buffered for an unentitled caller
before `require_entitled` ever runs. Reading it inside the handler puts the whole body behind
every dependency on the route, and is also the only way to set `max_part_size`: Starlette's
default part cap is 1 MB, well under the 5 MB export this is specified to accept. The request
body is described to OpenAPI through `openapi_extra` so the onboarding UI (NEU-1358) still
generates against a real contract."""

import asyncio
import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException

from upmovies.app.dto import (
    ImportCandidateOut,
    ImportConfirmIn,
    ImportJobOut,
    ImportJobStartedOut,
)
from upmovies.app.entitlements import require_entitled
from upmovies.app.models import ImportJob, User
from upmovies.app.rate_limit import rate_limit
from upmovies.app.repos import import_candidate_repo, import_job_repo
from upmovies.config import Settings, get_settings
from upmovies.deps import get_session, require_csrf
from upmovies.ingest.imports.letterboxd import (
    InvalidImportFile,
    LetterboxdExport,
    parse_upload,
)
from upmovies.ingest.imports.review import (
    ImportJobNotFound,
    ImportNotAwaitingReview,
    confirm,
    discard_unconfirmed,
)
from upmovies.ingest.imports.runner import SOURCE, run_letterboxd_import

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
"""Spec §1. A Letterboxd export of a very large library is a few hundred kilobytes of zipped
CSV, so this is generous by an order of magnitude and still small enough that an unentitled
caller who somehow reaches the parser cannot spend meaningful bandwidth."""

UPLOAD_FIELD = "file"

entitled = require_entitled()

router = APIRouter(prefix="/me/import", tags=["me"], dependencies=[Depends(entitled)])

_UPLOAD_BODY: dict[str, Any] = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": [UPLOAD_FIELD],
                    "properties": {
                        UPLOAD_FIELD: {
                            "type": "string",
                            "format": "binary",
                            "description": (
                                "A Letterboxd export zip, or watchlist.csv on its own. "
                                "Ratings are not imported (EF-20). At most "
                                f"{MAX_UPLOAD_BYTES} bytes."
                            ),
                        }
                    },
                }
            }
        },
    }
}


@router.post(
    "/letterboxd",
    response_model=ImportJobStartedOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf), Depends(rate_limit("import"))],
    openapi_extra=_UPLOAD_BODY,
)
async def start_letterboxd_import(
    request: Request,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ImportJobStartedOut:
    """Accept a Letterboxd export and start importing it. 202 with the id to poll.

    Rejects before it enqueues, so the uploader learns what is wrong with their file while they
    are still looking at it: 413 for an upload past the cap, 422 for anything `parse_upload`
    cannot read — which since EF-20 includes a `ratings.csv` sent on its own — and 409 while one
    of this user's imports is still going, one at a time, because two would race each other for
    the same rate-limited TMDB budget and for the same follow rows.

    A job waiting on review is not "still going": it is discarded, and this one starts (EF-22).
    After the file is read, so a bad upload does not cost the user the list they have."""
    export = await _parsed_upload(request)

    await discard_unconfirmed(db, user_id=user.id)
    if await import_job_repo.active_for_user(db, user.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="import_in_progress")
    job = await import_job_repo.create(
        db, user_id=user.id, source=SOURCE, rows_total=export.row_count
    )
    await db.commit()

    # Spawned rather than awaited, and the reference is deliberately not held: this mirrors
    # `routers/ingest_admin.py`, and the task's own wrapper is what guarantees the job row
    # reaches a terminal status whatever happens inside it.
    asyncio.create_task(run_letterboxd_import(job.id, export, settings))  # noqa: RUF006
    log.info("letterboxd import queued", extra={"job_id": str(job.id), "rows": export.row_count})
    return ImportJobStartedOut(job_id=job.id)


@router.get(
    "/active",
    response_model=ImportJobOut,
    responses={status.HTTP_204_NO_CONTENT: {"description": "The user has no open import."}},
)
async def get_open_import_job(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> ImportJobOut | Response:
    """This user's open import — queued, running or awaiting review — or 204 when there is
    none. This is how a page that did not start the job finds it again: a reload, another
    device, or a browser that lost the id would otherwise strand a list waiting on review
    until the user's next upload discarded it (NEU-1452).

    204 rather than 404: having no open import is the ordinary state of an account, not a
    lookup miss. Declared before `/{job_id}`, which would otherwise hand `active` to the UUID
    parser and answer 422. Off the `import` bucket for the by-id poll's reason — this is read
    on every mount of the pages that restore a job — and read-only: it never discards or
    finishes the job it returns."""
    job = await import_job_repo.active_for_user(db, user.id)
    if job is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return await _job_out(db, job)


@router.get("/{job_id}", response_model=ImportJobOut)
async def get_import_job(
    job_id: UUID,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> ImportJobOut:
    """One of this user's import jobs. 404 for anyone else's — the id is a UUID nobody guesses,
    but confirming one exists is still an answer this route has no reason to give.

    Deliberately not rate-limited on the `import` bucket the upload uses. That bucket is six an
    hour (D-19, NEU-1344) and this is polled every two seconds while a job runs; sharing it
    would make the upload's own status unreadable."""
    job = await import_job_repo.get_for_user(db, job_id=job_id, user_id=user.id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="import_job_not_found")
    return await _job_out(db, job)


@router.post("/{job_id}/confirm", response_model=ImportJobOut, dependencies=[Depends(require_csrf)])
async def confirm_import_job(
    job_id: UUID,
    body: ImportConfirmIn,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> ImportJobOut:
    """Follow the films the user kept from the review list, and finish the job (EF-22). 200
    with the job, now `succeeded`.

    409 unless the job is `awaiting_review` — still running, already confirmed, or superseded
    by a newer import. Another user's job is a 404, not the 409 the ticket words it as: the
    poll answers 404 for it, and a confirm that said 409 instead would tell a caller holding
    somebody else's job id that it exists.

    Not on the `import` bucket, like the poll: it spends no TMDB quota, and one upload is
    already one token from that bucket."""
    try:
        job = await confirm(db, user_id=user.id, job_id=job_id, film_ids=body.film_ids)
    except ImportJobNotFound:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="import_job_not_found"
        ) from None
    except ImportNotAwaitingReview:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="import_not_awaiting_review"
        ) from None
    return await _job_out(db, job)


async def _job_out(db: AsyncSession, job: ImportJob) -> ImportJobOut:
    """The job as its owner reads it, with the review list while there is one to review."""
    out = ImportJobOut.model_validate(job)
    if job.status == "awaiting_review":
        out.candidates = [
            ImportCandidateOut.model_validate(c)
            for c in await import_candidate_repo.list_for_job(db, job.id)
        ]
    return out


async def _parsed_upload(request: Request) -> LetterboxdExport:
    """The uploaded file, read and parsed, or the HTTP error explaining why it could not be."""
    try:
        form = await request.form(max_files=2, max_fields=2, max_part_size=MAX_UPLOAD_BYTES)
    except MultiPartException:
        # Starlette raises this when a part runs past `max_part_size`, which is the only limit
        # it is given here — so the message is about size and nothing else.
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="import_file_too_large"
        ) from None
    try:
        upload = form.get(UPLOAD_FIELD)
        if not isinstance(upload, UploadFile):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"expected a file in the {UPLOAD_FIELD!r} field",
            )
        data = await upload.read()
    finally:
        await form.close()

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE, detail="import_file_too_large"
        )
    try:
        return parse_upload(data)
    except InvalidImportFile as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)
        ) from None
