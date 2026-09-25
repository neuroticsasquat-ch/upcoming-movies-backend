"""`/admin/digest`: render any user's next digest, or mail it to yourself (DC-11, M3).

Human-facing, so `require_current_admin`, beside `/admin/users` whose picker the page resolves
users through. Both routes call `digest_sender.render_digest`, the one function the nightly send
renders through too, so what an admin sees here cannot drift from what the slot mails.

Neither route marks a row or commits: the queue is the send's to drain, and looking at a mail
must not change what the next slot carries. Both ignore the entitlement gate — an admin may
want to see a lapsed user's mail, and whether it would *go out* is the slot's question."""

import logging
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import DigestTestOut, DigestTestRequest
from upmovies.app.models import User
from upmovies.app.services.digest_sender import DigestCadence, render_digest, send_test_digest
from upmovies.config import Settings, get_settings
from upmovies.deps import get_mailer, get_session, require_csrf, require_current_admin
from upmovies.mail import MailConfigurationError, Mailer, MailError, MissingCredentialError

log = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/digest",
    tags=["admin"],
    dependencies=[Depends(require_current_admin)],
)

NOTHING_TO_SEND = "Nothing to send."
"""The preview's body when the slot would send no mail — a 200, so the page can show it in
the same frame a real preview goes in (DC-11)."""

_MEDIA_TYPES = {"html": "text/html", "text": "text/plain"}


def _today(today: date | None) -> date:
    return today if today is not None else datetime.now(UTC).date()


def _user_not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user_not_found")


@router.get("/preview")
async def preview_digest(
    user_id: UUID,
    cadence: DigestCadence,
    format: Literal["html", "text"] = "html",
    today: date | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    """The part the user would read, as the mail's own media type. `today` drives the slate
    window and the slate-day rule, so a daily preview dated on `SLATE_WEEKDAY` shows the slate.
    The empty state answers in the requested media type too, so the page's frame needs no
    second branch."""
    try:
        envelope = await render_digest(db, user_id, cadence, _today(today), settings)
    except LookupError:
        raise _user_not_found() from None
    if envelope is None:
        body = NOTHING_TO_SEND
    else:
        body = envelope.html if format == "html" else envelope.text
    return Response(content=body, media_type=_MEDIA_TYPES[format])


@router.post(
    "/test",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=DigestTestOut,
    dependencies=[Depends(require_csrf)],
)
async def send_test(
    payload: DigestTestRequest,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    mailer: Mailer = Depends(get_mailer),
    admin: User = Depends(require_current_admin),
) -> DigestTestOut:
    """Mail the digest the preview shows to the calling admin — never to the user, whatever
    the body says. `409 nothing_to_send` when the preview would read "Nothing to send.": there
    is no mail to hand the provider, and a 202 with no message id would claim one went out.
    A provider refusal is a `502 mail_failed`, so the page can say the send failed rather than
    that the server broke."""
    try:
        message_id = await send_test_digest(
            db,
            user_id=payload.user_id,
            cadence=payload.cadence,
            today=_today(payload.today),
            to=admin.email,
            mailer=mailer,
            settings=settings,
        )
    except LookupError:
        raise _user_not_found() from None
    except (MailError, MailConfigurationError, MissingCredentialError, httpx.HTTPError):
        log.exception("test digest for user_id=%s to the admin failed", payload.user_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="mail_failed") from None
    if message_id is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="nothing_to_send")
    return DigestTestOut(message_id=message_id)
