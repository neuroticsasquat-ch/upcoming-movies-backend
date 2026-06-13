from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from httpx import Request as HRequest
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app import passwords, tokens
from upmovies.app.models import User
from upmovies.app.repos import invite_repo, session_repo
from upmovies.main import app


@pytest.fixture
async def make_invite(session: AsyncSession):
    async def _make(*, email_hint: str | None = None) -> str:
        code = tokens.new_session_id()
        await invite_repo.create(session, code=code, email_hint=email_hint)
        await session.commit()
        return code

    return _make


@pytest.fixture
async def make_user(session: AsyncSession):
    async def _make(
        email: str = "user@example.com",
        password: str = "hunter2hunter2",
        display_name: str = "Test User",
        is_admin: bool = False,
    ) -> User:
        user = User(
            email=email,
            password_hash=passwords.hash_password(password),
            display_name=display_name,
            is_admin=is_admin,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user

    return _make


async def _build_authed_client(session: AsyncSession, user: User) -> AsyncClient:
    sess_id = tokens.new_session_id()
    await session_repo.create(
        session, session_id=sess_id, user_id=user.id, ttl_days=30, user_agent=None, ip=None
    )
    csrf = tokens.new_csrf_token()
    await session.commit()

    async def _inject_cookies(request: HRequest) -> None:
        request.headers["cookie"] = f"upmovies_session={sess_id}; csrf_token={csrf}"

    client = AsyncClient(
        transport=ASGITransport(app=app),
        base_url="https://test",
        headers={"X-CSRF-Token": csrf},
        event_hooks={"request": [_inject_cookies]},
    )
    client.user = user  # type: ignore[attr-defined]
    return client


@pytest.fixture
async def authed_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    user = await make_user()
    async with await _build_authed_client(session, user) as c:
        yield c


@pytest.fixture
async def admin_authed_client(session: AsyncSession, make_user) -> AsyncIterator[AsyncClient]:
    user = await make_user(email="admin@example.com", is_admin=True)
    async with await _build_authed_client(session, user) as c:
        yield c
