"""`/me/push`: registering a browser for notifications, and the key it needs to do so (D-36),
subscriber-only (D-39).

Same shape as `/me/settings` next door — `require_entitled()` applied once at the router so a
route added later cannot forget it, cookie session plus CSRF on the writes.

**Two of the three routes also require the deployment to hold a VAPID keypair.** Subscribing
without one would take a registration this process can never push to, and then fail the *next*
boot on the check that notices (`push.validate_push_configuration`) — so an unconfigured
deployment says `push_unavailable` up front instead. `DELETE` is deliberately outside that
guard: a user must always be able to unregister a browser, including from a deployment whose
keys have been removed since they subscribed.
"""

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import PushSubscribeRequest, PushUnsubscribeRequest, VapidPublicKeyOut
from upmovies.app.entitlements import require_entitled
from upmovies.app.models import User
from upmovies.app.services import push_service
from upmovies.config import Settings, get_settings
from upmovies.deps import get_session, require_csrf
from upmovies.push import push_configuration_problems

entitled = require_entitled()

router = APIRouter(prefix="/me/push", tags=["me"], dependencies=[Depends(entitled)])

USER_AGENT_MAX = 512
"""What is kept of a `User-Agent`. It is a label for a device list, not evidence — and the
header is caller-controlled, so it is truncated rather than trusted to be short."""


def require_push_configured(settings: Settings = Depends(get_settings)) -> Settings:
    """503 unless this deployment can actually sign a push.

    503 rather than 404 or 501: the route exists and the capability is expected to come back
    when the deployment is configured, which is what a client polling a feature it just saw
    fail should be told.

    It asks `push_configuration_problems` — the same rule the notify slot boots on — rather
    than checking the two keys it happens to need here. A deployment with a keypair and a
    malformed `VAPID_SUBJECT` would otherwise take subscriptions all day and then refuse to
    send to any of them, which is the exact gap between the two ends this guard exists to
    close."""
    if push_configuration_problems(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="push_unavailable"
        )
    return settings


@router.get(
    "/vapid-public-key",
    response_model=VapidPublicKeyOut,
    dependencies=[Depends(require_push_configured)],
)
async def vapid_public_key(settings: Settings = Depends(get_settings)) -> VapidPublicKeyOut:
    """The application server key the browser subscribes with (D-36)."""
    return VapidPublicKeyOut(public_key=settings.vapid_public_key)


@router.post(
    "",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf), Depends(require_push_configured)],
)
async def subscribe(
    payload: PushSubscribeRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
    user_agent: str | None = Header(default=None),
) -> Response:
    """Register this browser, or refresh the registration it already holds.

    204 rather than the stored row: the client already has every field it sent, and the row's
    id is bookkeeping it has no use for. Idempotent, so a client that posts its subscription on
    every page load is doing nothing wrong."""
    await push_service.subscribe(
        db,
        user=user,
        endpoint=payload.endpoint,
        p256dh=payload.keys.p256dh,
        auth=payload.keys.auth,
        user_agent=user_agent[:USER_AGENT_MAX] if user_agent else None,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_csrf)])
async def unsubscribe(
    payload: PushUnsubscribeRequest,
    user: User = Depends(entitled),
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Unregister this browser. 204 whether or not we still held the registration — see
    `push_service.unsubscribe`."""
    await push_service.unsubscribe(db, user=user, endpoint=payload.endpoint)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
