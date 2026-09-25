"""`/digest/unsubscribe/{token}`: the digest's one-click unsubscribe (DC-10, RFC 8058).

Public on purpose — no session, no CSRF, no entitlement. The caller is either a mailbox provider
acting on the `List-Unsubscribe-Post` header, which POSTs from its own servers with no cookie,
or a reader who opened the header's (or the footer's) link in a browser that may not be
signed in. The token is the whole credential, and all it can do is turn one digest off, so
there is nothing for a session or a CSRF check to protect.

Both methods write. RFC 8058 wants the POST; the GET is there for clients that open the link
instead of posting it, and doing the same thing there is what makes "unsubscribe" in the footer
work in one click. Mailbox link scanners that prefetch the GET can therefore unsubscribe a
reader, and that is accepted: the cost is a digest that stops, which the reader can turn back
on from the settings page the GET lands them on."""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.rate_limit import rate_limit
from upmovies.app.services import settings_service
from upmovies.config import Settings, get_settings
from upmovies.deps import get_session

router = APIRouter(prefix="/digest", tags=["digest"])

_unsubscribe_limit = Depends(rate_limit("digest_unsubscribe"))


async def _unsubscribe(db: AsyncSession, token: str) -> None:
    # 404 rather than a quiet 204 for a token that names nobody: this is a lookup by bearer
    # credential, like `/calendar/{token}.ics`, and answering both the same way would make a
    # typo'd link look like it worked.
    if not await settings_service.unsubscribe_digest(db, token=token):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unsubscribe_not_found")


@router.post(
    "/unsubscribe/{token}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[_unsubscribe_limit],
)
async def unsubscribe(token: str, db: AsyncSession = Depends(get_session)) -> Response:
    """The one-click POST a mailbox provider makes (RFC 8058). Sets the cadence to `off`;
    idempotent. The `List-Unsubscribe=One-Click` form body it carries is not read — the URL
    says everything."""
    await _unsubscribe(db, token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/unsubscribe/{token}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    dependencies=[_unsubscribe_limit],
)
async def unsubscribe_via_link(
    token: str,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """The same unsubscribe for a client that opens the link, then a redirect to the settings
    page, which confirms it (`?digest=off`) and offers the way back."""
    await _unsubscribe(db, token)
    return RedirectResponse(
        url=f"{settings.public_base_url.rstrip('/')}/me/settings?digest=off",
        status_code=status.HTTP_302_FOUND,
    )
