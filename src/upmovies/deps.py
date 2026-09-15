from collections.abc import AsyncIterator

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.models import User
from upmovies.app.services import account_service
from upmovies.config import Settings, get_settings
from upmovies.db import SessionLocal
from upmovies.mail import Mailer


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


def require_admin(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.admin_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


async def require_csrf(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    cookie = request.cookies.get(settings.csrf_cookie_name)
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or cookie != header:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf_invalid")


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> User:
    sess_id = request.cookies.get(settings.session_cookie_name)
    user = await account_service.resolve_session_user(db, session_id=sess_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth_required")
    return user


async def require_current_admin(user: User = Depends(get_current_user)) -> User:
    """Session auth + admin gate for the human-facing admin UI endpoints. Distinct from
    `require_admin`, which gates the machine-facing ADMIN_TOKEN endpoints."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin_required")
    return user


def get_mailer(request: Request) -> Mailer:
    """The process-wide `Mailer`, put on `app.state` by the lifespan.

    Typed as the `Mailer` Protocol rather than as `MailGateway` so a route declares what it
    needs — a template send — and not which implementation provides it; a test overrides this
    dependency with a `NoopTransport`-backed gateway or its own stub and the route is none the
    wiser.

    A route reached through a TestClient that bypasses the lifespan has no mailer, and a
    `500` naming the reason beats an `AttributeError` on `app.state`: the fix is to run the
    lifespan or override this dependency, and neither is guessable from the raw traceback."""
    mailer: Mailer | None = getattr(request.app.state, "mailer", None)
    if mailer is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="mail_unavailable",
        )
    return mailer
