"""Whether a user may reach subscriber functionality — the one place that reads
`User.entitled_until`.

Beside `app/verification.py` and for the same reason: the callers are not here yet. The follow
graph, timeline, imports, settings and the calendar feed all land in M3 and M7, and each
should import a named rule rather than re-deriving `entitled_until > now()`. Unlike verification,
this one *is* an access gate (D-37) — hence the dependency.

Three entry points because the gate has two kinds of checkpoint and one of them cannot use a
dependency (D-39):

- `is_entitled(user)` — the rule itself, over a loaded row.
- `require_entitled()` — the request-time checkpoint, a FastAPI dependency for the `/me/*` routes.
- `entitled_user_clause()` — the batch-time checkpoint, a SQL predicate for the `notify`, `digest`
  and sweep passes, which fan out over every user instead of answering a request and so get no
  protection from a route dependency at all. That is the half that gets missed.

Note what is deliberately absent: any way to open access globally. There is no settings flag and
no trial grant, because until *bl: Subscription & Billing* ships the only way in is a row-level
grant made by hand (D-38)."""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy import ColumnElement, and_, func

from upmovies.app.models import User
from upmovies.deps import get_current_user


def is_entitled(user: User) -> bool:
    """True while this user holds a live grant (D-37).

    NULL — the default for every signup, and the state of every account that existed before
    this column did — is not entitled. A past `entitled_until` is not entitled either: that is
    how a grant is ended, since rows are never deleted (D-38)."""
    return user.entitled_until is not None and user.entitled_until > datetime.now(UTC)


def require_entitled() -> Callable[[User], Awaitable[User]]:
    """The request-time gate: `Depends(require_entitled())` on a subscriber-only route.

    A factory returning the dependency, rather than a bare dependency function, so the call
    site reads as the gate being *applied* here — matching `rate_limit("bucket")` next door —
    and so a future grant scope can be passed without touching every route.

    403 rather than 404: the caller is authenticated (`get_current_user` already answered 401
    if not), so hiding the route's existence from them buys nothing and costs them the reason.
    The one surface that does hide is `/calendar/{token}.ics`, whose token is unauthenticated
    and must not confirm that it is valid — that route answers 404 and therefore does not use
    this dependency (D-39)."""

    async def dependency(user: User = Depends(get_current_user)) -> User:
        if not is_entitled(user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="entitlement_required"
            )
        return user

    return dependency


def entitled_user_clause() -> ColumnElement[bool]:
    """The batch-time gate: a predicate over `app.user` for a query that selects recipients.

    `func.now()` rather than a Python timestamp so the rule is spelled once, in SQL, exactly as
    D-37 states it — a pass that filtered on a timestamp it computed itself would be a second
    copy of the rule, free to drift from `is_entitled`.

    Note what it does *not* buy, since the name invites the assumption: Postgres `now()` is
    `transaction_timestamp()`, fixed when the transaction began, not a fresh reading per row.
    Every row a long pass examines inside one transaction is therefore judged against the same
    instant. That is the behaviour to want here — a digest run should not post to half its
    recipients under one clock and half under another — but a pass that needs the wall clock as
    it advances must commit between batches rather than reach for `clock_timestamp()`."""
    return and_(User.entitled_until.is_not(None), User.entitled_until > func.now())
