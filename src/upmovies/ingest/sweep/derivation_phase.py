"""The sweep's derived-watchlist phase: D-13's maintenance half.

The follow-creation half of the derivation answers a request and sees only the follow just
made. This is the other half, and it exists because the *catalog* moves too: a film the user's
followed director attaches to today qualified for nobody yesterday, and no request will ever run
for that user. So once the credits pass has recorded the day's attachments, this pass asks D-13's
question again for every entitled user who follows anything.

It holds no rule about which film qualifies — that is `app.services.derivation_service`, shared
with the request-time half so the two cannot disagree about what a follow reaches. What is this
module's is *who* it runs for, which is the half of the access gate a route dependency cannot
protect (D-39): the selection filters unentitled users in SQL, where the users are being
selected. A lapsed grant therefore stops maintenance and nothing else — items derived while it
was live stay exactly where they are (D-40), because nothing in this pass deletes.

Contract with the pipeline conventions, matching the other phases: one session per user so a
failure never rolls back the others, `record_progress` against the run id, abort after N
consecutive failures, and **no `finalize_run`** — all phases share one `ingest_run` row.
"""

import logging
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.app.entitlements import entitled_user_clause
from upmovies.app.models import Follow, User
from upmovies.app.services.derivation_service import derive_for_user
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory

log = logging.getLogger(__name__)


@dataclass
class DerivationResult:
    """What one derivation pass considered and wrote."""

    users_considered: int = 0
    """Entitled users holding at least one follow — the pass's working set, not the user table.
    Reported because `items_created: 0` is the healthy steady state and says nothing on its own
    about whether the pass had anybody to run for."""
    items_created: int = 0
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


async def load_derivation_user_ids(session: AsyncSession) -> list[UUID]:
    """Every entitled user who follows something, oldest account first.

    Both filters earn their place. The entitlement clause is D-39's batch checkpoint. The
    follows `EXISTS` is what keeps the pass proportional to the follow graph rather than to
    signups: an account that has followed nothing derives nothing by definition, and selecting
    it would buy one statement per signup on every sweep.
    """
    rows = await session.execute(
        select(User.id)
        .where(entitled_user_clause(), exists().where(Follow.user_id == User.id))
        .order_by(User.created_at, User.id)
    )
    return list(rows.scalars().all())


async def run_watchlist_derivation(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    failure_threshold: int = 10,
) -> DerivationResult:
    """Derive the missing watchlist items of every entitled user with follows (D-13)."""
    result = DerivationResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        user_ids = await load_derivation_user_ids(s)
    result.users_considered = len(user_ids)
    log.info("watchlist derivation: %d entitled users with follows", result.users_considered)

    for user_id in user_ids:
        await heartbeat.tick()
        try:
            async with owned_session(session_factory) as s:
                added = await derive_for_user(
                    s, user_id=user_id, today=today, excluded_statuses=excluded_statuses
                )
                if added:
                    # One unit of work is one user, as it is for every other per-item loop in
                    # the sweep; the item count is the phase's own counter and reaches
                    # `/admin/runs` through the detail line.
                    await record_progress(s, run_id, processed_delta=1)
                await s.commit()
        except Exception:
            # One user's derivation must not cost the rest of the pass.
            log.exception("deriving watchlist items for user %s failed", user_id)
            result.failures += 1
            if await guard.failed():
                result.aborted = True
                result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
                log.error("watchlist derivation: %s", result.abort_error)
                return result
            continue
        guard.succeeded()
        result.items_created += added

    log.info(
        "watchlist derivation: %d items for %d users, %d failed",
        result.items_created,
        result.users_considered,
        result.failures,
    )
    return result
