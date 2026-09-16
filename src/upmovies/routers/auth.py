from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import (
    AuthedUserOut,
    EmailChangeConfirmRequest,
    EmailChangeRequest,
    LoginRequest,
    PasswordChangeRequest,
    PasswordResetConsumeRequest,
    PasswordResetRequest,
    SignupRequest,
    VerificationConsumeRequest,
    VerificationRequest,
)
from upmovies.app.errors import EmailInUse, InvalidCredentials, InvalidInvite, InvalidToken
from upmovies.app.models import User
from upmovies.app.services import (
    account_service,
    email_change_service,
    reset_service,
    verification_service,
)
from upmovies.app.verification import is_verified
from upmovies.config import Settings, get_settings
from upmovies.deps import get_current_user, get_mailer, get_session, require_csrf
from upmovies.mail import Mailer

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
    mailer: Mailer = Depends(get_mailer),
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
    # After the account exists, and deliberately not fatal to the signup if the provider is
    # having a bad minute (`verification_service` module docstring): the user is signed in
    # either way — verification gates outbound mail, not access (D-18) — and the recovery is
    # `POST /auth/verify/request` below.
    await verification_service.issue_and_send(db, user=user, mailer=mailer, settings=settings)
    _set_auth_cookies(response, session_id=sess_id, csrf=csrf, settings=settings)
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_admin=user.is_admin,
        email_verified=is_verified(user),
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
        email_verified=is_verified(user),
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
        email_verified=is_verified(user),
        created_at=user.created_at,
        csrf_token=csrf,
    )


@router.post("/verify/request", status_code=status.HTTP_202_ACCEPTED)
async def request_verification(
    payload: VerificationRequest,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    mailer: Mailer = Depends(get_mailer),
) -> Response:
    """Send (or re-send) the verification mail for an address.

    Always 202, whether the address is unknown, already verified, or was just mailed: the
    route takes a bare email address and needs no session, so a response that varied would
    tell an anonymous caller which addresses have accounts. One address, one route, one
    outcome — which is also the shape the M1 rate limiter buckets (its own ticket; this route
    does no throttling of its own yet)."""
    await verification_service.request(
        db, email=str(payload.email), mailer=mailer, settings=settings
    )
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify_email(
    payload: VerificationConsumeRequest,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Spend a verification token and mark its owner verified.

    No session required and no CSRF: the mail can be opened on a device that has never signed
    in, and the token in the body *is* the credential — there is no ambient authority for a
    cross-site post to borrow.

    Answers with no body, deliberately. Returning the user would be convenient for a signed-in
    frontend and would also hand the account record — address, display name, admin flag — to
    whoever holds a forwarded or scanner-followed link, which is the disclosure the sibling
    route above refuses to make. A signed-in client re-reads `GET /me`; a signed-out one has
    nothing to update."""
    try:
        await verification_service.consume(db, token=payload.token)
    except InvalidToken as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/reset/request", status_code=status.HTTP_202_ACCEPTED)
async def request_password_reset(
    payload: PasswordResetRequest,
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    mailer: Mailer = Depends(get_mailer),
) -> Response:
    """Mail a password-reset link to an address.

    Always 202, with the same empty body, whether the address is unknown or was just mailed —
    the sibling `/verify/request` reasoning applies with more force here, because this is the
    route someone probes when they want to know which of a list of addresses is worth
    attacking. That includes provider failures: reporting one would report that there was
    something to send. Throttling is the M1 rate limiter's job (D-19, its own ticket); this
    route does none of its own yet."""
    await reset_service.request(db, email=str(payload.email), mailer=mailer, settings=settings)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/reset", status_code=status.HTTP_204_NO_CONTENT)
async def reset_password(
    payload: PasswordResetConsumeRequest,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Spend a reset token, set the new password, and drop every session the account has.

    No session required and no CSRF, for the reason `/auth/verify` needs neither: the mail is
    opened on a device that by definition could not sign in, and the token in the body *is*
    the credential, so there is no ambient authority for a cross-site post to borrow.

    Answers with no body and opens no session. Not signing the caller in is the conservative
    half of a route whose whole job is to invalidate credentials, and it costs the legitimate
    user nothing — they now hold a password that works.

    It also leaves this browser's cookies alone, which is the same thing `/auth/verify` does
    and is not an oversight. The tempting move is to clear them, on the theory that this
    browser may hold one of the sessions just deleted. But the route is reached without a
    session, so there is nothing tying the cookie in the request to the account in the token:
    a reset link opened in a browser already signed in as *somebody else* would sign that
    person out of their own live session. A cookie whose session row is gone already resolves
    to `None` and answers 401 on the next request, which is the state the clearing was
    supposed to produce anyway."""
    try:
        await reset_service.consume(db, token=payload.token, new_password=payload.new_password)
    except InvalidToken as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/email-change/request",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_csrf)],
)
async def request_email_change(
    payload: EmailChangeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
    settings: Settings = Depends(get_settings),
    mailer: Mailer = Depends(get_mailer),
) -> Response:
    """Start moving this account to a new address: mail the new one a confirmation link and
    the old one a notice that the move was asked for.

    202 rather than 204 because that is what this route honestly is — the change is accepted,
    not applied, and nothing about the account has moved when it answers.

    Unlike its unauthenticated siblings, this one *does* distinguish its failures: 401 for the
    wrong password, 409 for an address someone already holds. `email_change_service.request`
    is where that departure is argued; the short version is that a route costing an account
    and its password is a far weaker enumeration oracle than one taking a bare address, and
    silence would strand a user whose typo'd address happens to belong to a stranger."""
    try:
        await email_change_service.request(
            db,
            user=user,
            new_email=str(payload.new_email),
            current_password=payload.current_password,
            mailer=mailer,
            settings=settings,
        )
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    except EmailInUse as err:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.post("/email-change/confirm", status_code=status.HTTP_204_NO_CONTENT)
async def confirm_email_change(
    payload: EmailChangeConfirmRequest,
    db: AsyncSession = Depends(get_session),
) -> Response:
    """Spend an email-change token and move the account to the address it carries.

    No session and no CSRF, for the reason `/auth/verify` and `/auth/reset` need neither: the
    link lands in an inbox that by definition cannot yet sign in to this account, and the token
    in the body *is* the credential, so there is no ambient authority to borrow. The
    authorisation for the change was taken at request time, from a session and a password.

    Answers with no body, as its two siblings do: handing back the account record would give
    it to whoever opens a forwarded or scanner-followed link. A signed-in client re-reads
    `GET /me` — and its session is still good, because a change of address is not a reason to
    sign the owner out of the browser they started it in."""
    try:
        await email_change_service.confirm(db, token=payload.token)
    except InvalidToken as err:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid_token"
        ) from err
    except EmailInUse as err:
        # The one failure worth distinguishing from `invalid_token`: the address was free when
        # the mail went out and is not now. The user can act on that by picking another one.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="email_in_use") from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)
