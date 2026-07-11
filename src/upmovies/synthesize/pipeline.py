"""The `synthesize` pipeline: select events with no summary yet (write-once — an existing summary
is never reselected); summarize each (sequential Messages path or batched Batches path); and
upsert news.event_summary. Idempotent — a re-run with nothing pending is a no-op. One event's
failure never rolls back others. Mirrors link/pipeline.py structurally."""

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, nulls_last, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.runs import finalize_run, record_llm_usage, record_progress
from upmovies.llm.client import Usage
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.resolve import resolve_google_news_url
from upmovies.synthesize.summarizer import (
    EventInput,
    StoryInput,
    SummaryClient,
    SummaryResult,
    build_summary_batch_request,
    parse_summary,
    summarize_event,
)
from upmovies.synthesize.url_resolution import Resolver, ResolveResult, run_url_resolution

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]


@dataclass
class SynthesizeResult:
    new: int
    refreshed: int
    failed: int


@dataclass
class _PendingEvent:
    event_id: UUID
    is_new: bool
    event_input: EventInput


@asynccontextmanager
async def _owned_session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as s:
        yield s


async def _select_pending(session: AsyncSession) -> list[_PendingEvent]:
    """Events with no summary row yet — write-once: an event whose summary already exists is
    never reselected, even if the event was later updated or the prompt version has moved on.
    Returns each mapped to an EventInput (plain dataclasses — safe to use after the session
    closes), with is_new = no prior summary existed (always True here)."""
    rows = (
        await session.execute(
            select(Event, Film.title, EventSummary.event_id)
            .join(Film, Film.id == Event.film_id)
            .outerjoin(EventSummary, EventSummary.event_id == Event.id)
            .where(EventSummary.event_id.is_(None))
        )
    ).all()
    if not rows:
        return []

    event_ids = [event.id for event, _title, _existing in rows]
    stories_by_event: dict[UUID, list[Story]] = defaultdict(list)
    story_rows = (
        await session.execute(
            select(EventStory.event_id, Story)
            .join(Story, Story.id == EventStory.story_id)
            .where(EventStory.event_id.in_(event_ids))
            .order_by(nulls_last(Story.published_at.asc()), Story.id.asc())
        )
    ).all()
    for event_id, story in story_rows:
        stories_by_event[event_id].append(story)

    pending: list[_PendingEvent] = []
    for event, film_title, existing in rows:
        story_inputs = [
            StoryInput(
                title=s.title,
                dek=str((s.raw or {}).get("summary", "")),
                source=s.source,
            )
            for s in stories_by_event.get(event.id, [])
        ]
        pending.append(
            _PendingEvent(
                event_id=event.id,
                is_new=existing is None,
                event_input=EventInput(
                    event_type=event.event_type,
                    film_title=film_title,
                    source_updated_at=event.updated_at,
                    stories=story_inputs,
                    subjects=event.subject_key,
                ),
            )
        )
    return pending


async def _upsert_summary(session: AsyncSession, event_id: UUID, result: SummaryResult) -> None:
    """Insert or update the one summary row for an event (PK event_id). Refreshes generated_at
    on update. Caller owns the commit."""
    stmt = pg_insert(EventSummary).values(
        event_id=event_id,
        summary=result.summary,
        model=result.model,
        prompt_version=result.prompt_version,
        source_updated_at=result.source_updated_at,
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=["event_id"],
        set_={
            "summary": result.summary,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "source_updated_at": result.source_updated_at,
            "generated_at": func.now(),
        },
    )
    await session.execute(stmt)


async def _summary_stage_sequential(
    *,
    session_factory: SessionFactory,
    client: SummaryClient,
    run_id: UUID,
    model: str,
    prompt_version: str,
    pending: list[_PendingEvent],
    run_date: date,
) -> tuple[int, int, int, Usage]:
    new = refreshed = failed = 0
    total_usage = Usage()
    for pe in pending:
        try:
            result, usage = await summarize_event(
                client=client,
                model=model,
                prompt_version=prompt_version,
                event=pe.event_input,
                run_date=run_date,
            )
            async with _owned_session(session_factory) as s:
                await _upsert_summary(s, pe.event_id, result)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            total_usage += usage
            if pe.is_new:
                new += 1
            else:
                refreshed += 1
        except Exception:
            log.exception("summarize failed for event %s", pe.event_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            failed += 1
    return new, refreshed, failed, total_usage


async def _summary_stage_batched(
    *,
    session_factory: SessionFactory,
    client: SummaryClient,
    run_id: UUID,
    model: str,
    prompt_version: str,
    pending: list[_PendingEvent],
    run_date: date,
) -> tuple[int, int, int, Usage]:
    if not pending:
        return 0, 0, 0, Usage()

    by_id = {str(pe.event_id): pe for pe in pending}
    requests = [
        build_summary_batch_request(
            custom_id=str(pe.event_id), model=model, event=pe.event_input, run_date=run_date
        )
        for pe in pending
    ]

    try:
        results = await client.complete_batch(requests)
    except Exception:
        log.exception("summary batch submit of %d events failed", len(requests))
        async with _owned_session(session_factory) as s:
            await record_progress(s, run_id, failed_delta=len(requests))
            await s.commit()
        return 0, 0, len(requests), Usage()

    new = refreshed = failed = 0
    total_usage = Usage()
    for custom_id, pe in by_id.items():
        result = results.get(custom_id)
        try:
            if result is None or not result.ok:
                detail = result.error_type if result else "missing"
                raise RuntimeError(f"summary for event {custom_id} unavailable: {detail}")
            summary_result = SummaryResult(
                summary=parse_summary(result.text),
                model=model,
                prompt_version=prompt_version,
                source_updated_at=pe.event_input.source_updated_at,
            )
            async with _owned_session(session_factory) as s:
                await _upsert_summary(s, pe.event_id, summary_result)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            if result.usage is not None:
                total_usage += result.usage
            if pe.is_new:
                new += 1
            else:
                refreshed += 1
        except Exception:
            log.exception("summary apply failed for event %s", custom_id)
            async with _owned_session(session_factory) as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            failed += 1
    return new, refreshed, failed, total_usage


async def run_synthesize_ingest(
    *,
    session_factory: SessionFactory,
    client: SummaryClient,
    run_id: UUID,
    model: str,
    prompt_version: str,
    use_batches: bool = True,
    url_resolve_per_run: int = 500,
    url_resolve_max_attempts: int = 3,
    url_resolve_delay_seconds: float = 1.0,
    url_resolve_resolver: Resolver = resolve_google_news_url,
) -> SynthesizeResult:
    async with _owned_session(session_factory) as s:
        pending = await _select_pending(s)

    run_date = datetime.now(UTC).date()
    stage = _summary_stage_batched if use_batches else _summary_stage_sequential
    new, refreshed, failed, summary_usage = await stage(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        model=model,
        prompt_version=prompt_version,
        pending=pending,
        run_date=run_date,
    )
    async with _owned_session(session_factory) as s:
        await record_llm_usage(
            s, run_id, stage="summarize", model=model, batched=use_batches, usage=summary_usage
        )
        await s.commit()

    try:
        resolve_result = await run_url_resolution(
            session_factory=session_factory,
            resolver=url_resolve_resolver,
            per_run=url_resolve_per_run,
            max_attempts=url_resolve_max_attempts,
            delay_seconds=url_resolve_delay_seconds,
        )
    except Exception:
        log.exception("url-resolution stage failed")
        resolve_result = ResolveResult(marked=0, resolved=0, failed=0, pending=0)

    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="succeeded",
            detail=(
                f"summarized {new + refreshed} ({new} new, {refreshed} refreshed); {failed} failed"
                f"; urls marked {resolve_result.marked}, resolved {resolve_result.resolved},"
                f" failed {resolve_result.failed}, pending {resolve_result.pending}"
            ),
        )
        await s.commit()
    return SynthesizeResult(new=new, refreshed=refreshed, failed=failed)
