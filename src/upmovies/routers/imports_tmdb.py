"""`/me/import/tmdb`: TMDB's v3 approve flow, and the one-shot import it starts (D-16),
subscriber-only (D-39).

Two routes, and between them the whole of the credential's life. `start` asks TMDB for a
request token, records who it was issued to, and sends the user to themoviedb.org to approve
it. `callback` takes the approved token back, exchanges it for a session id, and hands that
session id to a background task that reads the user's watchlist and then **deletes it**.
The scopes TMDB grants are unchanged by EF-20 — a session is one approval, not one per list —
but the import no longer touches the favorites half of what it could read.

Nothing is stored: there is no `app.tmdb_link`, no encrypted credential and no unlink route,
because a one-shot import whose last act is a delete needs none of them, and re-importing is
the same one click as importing (see `docs/specs/NEU-1357-tmdb-account-import.md`).

`app.tmdb_auth_request` is the only state either route keeps, and it exists for one reason:
TMDB's redirect carries the request token and nothing else. Without a row saying who was sent
which token, `callback` would have to believe whoever posts one, and an approved token observed
in a redirect URL could be spent against a different account. So the token is bound to the user
at `start` and the binding is checked — and deleted — at `callback`.

The gate (D-39) sits on both. On `start` it is in front of the request token, so an unentitled
user never reaches TMDB's approve screen at all. On `callback` it is in front of the exchange
for a different reason: entitlement can lapse in the seconds between approving and coming back,
and the one-shot design means a session created for a user we then refuse is a live credential
nothing would ever clean up — so that path deletes it before answering 403."""

import asyncio
import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import ImportJobStartedOut, TMDBCallbackIn
from upmovies.app.entitlements import is_entitled, require_entitled
from upmovies.app.models import ImportJob, User
from upmovies.app.rate_limit import rate_limit
from upmovies.app.repos import import_job_repo, tmdb_auth_repo
from upmovies.config import Settings, get_settings
from upmovies.deps import get_current_user, get_session, require_csrf
from upmovies.ingest.imports.review import discard_unconfirmed
from upmovies.ingest.imports.tmdb_account import SOURCE, delete_session, run_tmdb_import
from upmovies.ingest.tmdb.client import TMDBAuthRejected, TMDBClient

log = logging.getLogger(__name__)

APPROVE_URL = "https://www.themoviedb.org/authenticate/{token}"
"""Where the user approves the token. TMDB's own page, on TMDB's domain — the point of the v3
flow is that the password is never typed into anything of ours."""

entitled = require_entitled()

router = APIRouter(prefix="/me/import/tmdb", tags=["me"])


@router.get(
    "/start",
    status_code=status.HTTP_302_FOUND,
    dependencies=[Depends(entitled), Depends(rate_limit("import"))],
    response_class=RedirectResponse,
)
async def start_tmdb_import(
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Begin the approve flow: a fresh request token, bound to this user, and a redirect to
    TMDB.

    A 302 rather than a JSON body holding the URL, because the caller is a link the user
    clicked and the next thing that has to happen is their browser being on themoviedb.org.
    `redirect_to` is where TMDB sends them afterwards — a frontend page, which reads the
    `request_token` and `approved` params TMDB appends and posts them to the callback below
    with the cookie and CSRF header it requires.

    Rate-limited on the `import` bucket the upload uses: this is the other way to spend the
    same TMDB budget, and six an hour is more approve screens than anyone needs."""
    # Swept here rather than on a schedule — this is the one route that grows the table, so it
    # is the natural place to pay for it, and a user who never came back leaves a row the next
    # user's start clears.
    await tmdb_auth_repo.prune_expired(db)

    async with TMDBClient.from_settings(settings) as client:
        request_token = await client.create_request_token()
    await tmdb_auth_repo.create(db, request_token=request_token, user_id=user.id)
    await db.commit()

    log.info("tmdb import approve flow started", extra={"user_id": str(user.id)})
    return RedirectResponse(
        url=str(
            httpx.URL(
                APPROVE_URL.format(token=request_token),
                params={"redirect_to": settings.tmdb_redirect_url},
            )
        ),
        status_code=status.HTTP_302_FOUND,
    )


@router.post(
    "/callback",
    response_model=ImportJobStartedOut,
    status_code=status.HTTP_202_ACCEPTED,
    # `get_current_user` ahead of `require_csrf` so an unauthenticated caller gets 401 rather
    # than 403 for the CSRF header they were never issued — the order `routers/imports.py` gets
    # from its router-level gate, and the one the frontend's error handling reads.
    dependencies=[Depends(get_current_user), Depends(require_csrf)],
)
async def tmdb_import_callback(
    body: TMDBCallbackIn,
    # `get_current_user` rather than the gate, because this route has to *undo* something
    # before it can refuse: see `_require_still_entitled`. Authentication is still required.
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> ImportJobStartedOut:
    """Finish the approve flow and start the import. 202 with the id to poll.

    The order is load-bearing. The token is checked against this user *before* anything is
    exchanged, so a token lifted from someone else's redirect buys nothing; it is deleted as
    soon as it is accepted, so it cannot be replayed even by its owner; and the session id it
    becomes is handed straight to the task and never written down.

    403 for another user's token, 400 for one that is expired, refused, or that TMDB will not
    exchange, 409 while one of this user's imports is still going — one at a time, for the same
    reasons the upload is.

    Not rate-limited, unlike `start` and the upload, and not because it is cheap: it is the
    route that actually spends TMDB quota. It cannot be reached without a request token, and a
    token can only come from `start`, which *is* limited — so the flow already costs one token
    from the `import` bucket, the same as one Letterboxd upload. Metering both halves would
    make the same bucket mean three imports an hour here and six there."""
    pending = await tmdb_auth_repo.get(db, body.request_token)
    if pending is None or pending.user_id != user.id:
        # Deliberately the same answer for "no such token" and "not yours": distinguishing them
        # would tell a caller holding a token whether it is live.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tmdb_token_not_yours")

    await tmdb_auth_repo.delete_token(db, body.request_token)
    await db.commit()

    if tmdb_auth_repo.is_expired(pending):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="tmdb_token_expired")
    if not body.approved:
        # TMDB sends the user back whether or not they approved, so a refusal arrives here as a
        # normal callback. Nothing to exchange and nothing to clean up — the unapproved token
        # expires at TMDB on its own, and it has just been deleted at ours.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="tmdb_token_not_approved"
        )

    async with TMDBClient.from_settings(settings) as client:
        try:
            session_id = await client.create_session(body.request_token)
        except TMDBAuthRejected:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="tmdb_token_not_approved"
            ) from None

        # From here on there is a live credential, so every exit deletes it.
        try:
            _require_still_entitled(user)
            account = await client.account(session_id)
            job = await _queue(db, user=user, username=account.username)
        except Exception:
            await delete_session(client, session_id)
            raise

    # Spawned rather than awaited, and the reference deliberately not held, exactly as the
    # upload route does it: the task's own wrapper is what guarantees the job row reaches a
    # terminal status and the session is deleted, whatever happens inside it.
    asyncio.create_task(run_tmdb_import(job.id, session_id, account.id, settings))  # noqa: RUF006
    log.info(
        "tmdb import queued",
        extra={"job_id": str(job.id), "user_id": str(user.id), "tmdb_account": account.id},
    )
    return ImportJobStartedOut(job_id=job.id)


def _require_still_entitled(user: User) -> None:
    """403 if the grant lapsed between the approve screen and the return (D-39, spec amendment).

    Checked by hand rather than through `Depends(require_entitled())` because by this point a
    session id exists at TMDB, and a dependency raising 403 before the handler runs would leave
    it live with nothing left holding a reference to it. The caller's `except` deletes it; this
    only has to refuse."""
    if not is_entitled(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="entitlement_required")


async def _queue(db: AsyncSession, *, user: User, username: str) -> ImportJob:
    """Open the job row, or 409 if this user already has one running. A job waiting on review
    is discarded rather than refused (EF-22), in the same transaction as the new row.

    `rows_total=0` because nothing knows it yet: unlike an upload, which counts its rows while
    validating the file, the library this will read is behind a credential the job has not used
    yet. The runner sets it once it has both lists."""
    await discard_unconfirmed(db, user_id=user.id)
    if await import_job_repo.active_for_user(db, user.id) is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="import_in_progress")
    job = await import_job_repo.create(
        db, user_id=user.id, source=SOURCE, rows_total=0, tmdb_username=username
    )
    await db.commit()
    return job
