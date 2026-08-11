"""Persistence for `news.event_summary`.

One event has exactly one summary row (the event id is the PK), and two writers produce it: the
LLM summarizer and the deterministic writer for catalog-sourced events. Both go through
`upsert_summary` so supersession — a trade story clustering onto a catalog event, after which
the LLM body replaces the deterministic one — is an ordinary upsert on the same row rather than
a second code path.

Caller owns the commit.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.news.models import EventSummary


async def upsert_summary(
    session: AsyncSession,
    *,
    event_id: UUID,
    summary: str,
    model: str,
    prompt_version: str,
    source_updated_at: datetime,
) -> None:
    """Insert or replace the one summary row for an event. Refreshes `generated_at` on update.
    Leaves the human-edit marker (`edited_at`/`edited_by`) alone — an edited row is never
    selected for rewriting, so reaching here means there is no edit to preserve."""
    stmt = pg_insert(EventSummary).values(
        event_id=event_id,
        summary=summary,
        model=model,
        prompt_version=prompt_version,
        source_updated_at=source_updated_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id"],
        set_={
            "summary": summary,
            "model": model,
            "prompt_version": prompt_version,
            "source_updated_at": source_updated_at,
            "generated_at": func.now(),
        },
    )
    await session.execute(stmt)
