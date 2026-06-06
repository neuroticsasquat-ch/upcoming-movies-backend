from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.dto import AccountDeleteRequest, AuthedUserOut
from upmovies.app.errors import InvalidCredentials
from upmovies.app.models import User
from upmovies.app.services import account_service
from upmovies.config import Settings, get_settings
from upmovies.deps import get_current_user, get_session, require_csrf

router = APIRouter(tags=["me"])


@router.get("/me", response_model=AuthedUserOut)
async def me(
    request: Request,
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> AuthedUserOut:
    csrf = request.cookies.get(settings.csrf_cookie_name, "")
    return AuthedUserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        created_at=user.created_at,
        csrf_token=csrf,
    )


@router.delete(
    "/me",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_csrf)],
)
async def delete_me(
    payload: AccountDeleteRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_session),
) -> Response:
    try:
        await account_service.delete_account(db, user=user, password=payload.password)
    except InvalidCredentials as err:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_credentials"
        ) from err
    return Response(status_code=status.HTTP_204_NO_CONTENT)
