from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import (
    AuthedUserOut,
    LoginRequest,
    PasswordChangeRequest,
    SignupRequest,
)
from upmovies.app.errors import EmailInUse, InvalidCredentials, InvalidInvite
from upmovies.app.models import User
from upmovies.app.services import account_service
from upmovies.config import Settings, get_settings
from upmovies.deps import get_current_user, get_session, require_csrf

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_auth_cookies(
    response: Response,
    *,
    session_id: str,
    csrf: str,
    settings: Settings,
) -> None:
    max_age = settings.session_ttl_days * 86400
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_id,
        max_age=max_age,
        httponly=True,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
        domain=settings.cookie_domain,
    )
    response.set_cookie(
        key=settings.csrf_cookie_name,
        value=csrf,
        max_age=max_age,
        httponly=False,
        secure=settings.cookie_secure,
        samesite=settings.cookie_samesite,  # type: ignore[arg-type]
        path="/",
        domain=settings.cookie_domain,
    )


def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    for name in (settings.session_cookie_name, settings.csrf_cookie_name):
        response.delete_cookie(
            key=name,
            path="/",
            secure=settings.cookie_secure,
            samesite=settings.cookie_samesite,  # type: ignore[arg-type]
            httponly=name == settings.session_cookie_name,
            domain=settings.cookie_domain,
        )


@router.post("/signup", status_code=status.HTTP_201_CREATED, response_model=AuthedUserOut)
async def signup(
    payload: SignupRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    try:
        user, sess_id, csrf = await account_service.signup(
            db,
            email=str(payload.email),
            password=payload.password,
            display_name=payload.display_name,
            invite_code=payload.invite_code,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except InvalidInvite as err:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="invalid_invite") from err
    except EmailInUse as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        created_at=user.created_at,
        csrf_token=csrf,
    )


@router.post("/login", response_model=AuthedUserOut)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    try:
        user, sess_id, csrf = await account_service.authenticate(
            db,
            email=str(payload.email),
            password=payload.password,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
            lockout_threshold=settings.login_lockout_threshold,
            lockout_window_minutes=settings.login_lockout_window_minutes,
        )
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        created_at=user.created_at,
        csrf_token=csrf,
    )


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Response:
    sess_id = request.cookies.get(settings.session_cookie_name)
    if sess_id:
        await account_service.logout(db, session_id=sess_id)
    _clear_auth_cookies(response, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post(
    "/password",
    response_model=AuthedUserOut,
    dependencies=[Depends(require_csrf)],
)
async def change_password(
    payload: PasswordChangeRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    try:
        sess_id, csrf = await account_service.change_password(
            db,
            user=user,
            current_password=payload.current_password,
            new_password=payload.new_password,
            ttl_days=settings.session_ttl_days,
            user_agent=request.headers.get("user-agent"),
            ip=request.client.host if request.client else None,
        )
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        created_at=user.created_at,
        csrf_token=csrf,
    )
