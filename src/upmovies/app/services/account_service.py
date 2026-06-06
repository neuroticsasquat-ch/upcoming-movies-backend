from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.errors import EmailInUse, InvalidCredentials, InvalidInvite
from upmovies.app.models import User
from upmovies.app.passwords import hash_password, verify_password
from upmovies.app.repos import invite_repo, login_attempt_repo, session_repo, user_repo
from upmovies.app.tokens import new_csrf_token, new_session_id


async def signup(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str,
    invite_code: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[User, str, str]:
    """Create a new user, open a session, and return (user, session_id, csrf_token).
    Requires a valid unconsumed invite code; raises InvalidInvite otherwise.
    Raises EmailInUse on duplicate email."""
    invite = await invite_repo.get(db, invite_code)
    if invite is None or invite.consumed_at is not None:
        # Don't differentiate between "unknown" and "consumed" — keeps the
        # signup endpoint from leaking which codes were ever issued.
        raise InvalidInvite()
    if invite.email_hint is not None and invite.email_hint.lower() != email.lower():
        raise InvalidInvite()

    password_hash = hash_password(password)
    try:
        user = await user_repo.create(
            db, email=email, password_hash=password_hash, display_name=display_name
        )
    except IntegrityError as err:
        await db.rollback()
        raise EmailInUse() from err

    await invite_repo.consume(db, invite=invite, user_id=user.id, consumed_at=datetime.now(UTC))

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    await db.refresh(user)
    return user, sess_id, csrf


async def authenticate(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
    lockout_threshold: int = 5,
    lockout_window_minutes: int = 15,
) -> tuple[User, str, str]:
    """Verify credentials, open a new session, return (user, session_id, csrf_token).
    Raises InvalidCredentials on bad email/password OR when the email has hit
    the brute-force threshold (we deliberately return the same error so attackers
    can't tell whether they're locked out)."""
    since = datetime.now(UTC) - timedelta(minutes=lockout_window_minutes)
    failures = await login_attempt_repo.count_since(db, email=email, since=since)
    if failures >= lockout_threshold:
        raise InvalidCredentials()

    user = await user_repo.get_by_email(db, email)
    if user is None or not verify_password(password, user.password_hash):
        await login_attempt_repo.record(db, email=email, ip=ip)
        await db.commit()
        raise InvalidCredentials()

    # Successful login — wipe the slate clean.
    await login_attempt_repo.clear_for_email(db, email=email)

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    return user, sess_id, csrf


async def logout(db: AsyncSession, *, session_id: str) -> None:
    """Delete the session row. No-op if session doesn't exist."""
    await session_repo.delete(db, session_id)
    await db.commit()


async def change_password(
    db: AsyncSession,
    *,
    user: User,
    current_password: str,
    new_password: str,
    ttl_days: int,
    user_agent: str | None,
    ip: str | None,
) -> tuple[str, str]:
    """Verify current password, rotate to new password, invalidate all existing
    sessions, create a new one. Returns (session_id, csrf_token).
    Raises InvalidCredentials if current_password is wrong."""
    if not verify_password(current_password, user.password_hash):
        raise InvalidCredentials()

    await user_repo.update_password_hash(db, user, hash_password(new_password))
    await session_repo.delete_all_for_user(db, user.id)

    sess_id = new_session_id()
    csrf = new_csrf_token()
    await session_repo.create(
        db,
        session_id=sess_id,
        user_id=user.id,
        ttl_days=ttl_days,
        user_agent=user_agent,
        ip=ip,
    )
    await db.commit()
    return sess_id, csrf


async def delete_account(db: AsyncSession, *, user: User, password: str) -> None:
    """Verify password then delete user (cascade handles sessions and watch data).
    Raises InvalidCredentials if password is wrong."""
    if not verify_password(password, user.password_hash):
        raise InvalidCredentials()

    await user_repo.delete_user(db, user.id)
    await db.commit()


async def resolve_session_user(db: AsyncSession, *, session_id: str | None) -> User | None:
    """Given a cookie value, return the User if the session is valid.
    Touches the session and commits. Returns None if invalid."""
    if not session_id:
        return None

    sess = await session_repo.get_active(db, session_id)
    if sess is None:
        return None

    user = await user_repo.get_by_id(db, sess.user_id)
    if user is None:  # pragma: no cover  -- defensive: FK cascade prevents this
        return None

    await session_repo.touch(db, session_id)
    await db.commit()
    return user
