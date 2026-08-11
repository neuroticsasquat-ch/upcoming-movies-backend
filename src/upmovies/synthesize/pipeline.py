"""The `synthesize` pipeline: select events needing a summary (write-once — an existing LLM
summary is never reselected; the one exception is a superseded deterministic body, see
`_superseded_deterministic`); summarize each via the Messages API; and upsert
news.event_summary. Idempotent — a re-run with nothing pending is a no-op. One event's failure
never rolls back others. Mirrors link/pipeline.py structurally."""

import logging
from collections import defaultdict
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import ColumnElement, and_, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.runs import (
    StageCounts,
    finalize_run,
    record_llm_calls,
    record_llm_usage,
    record_progress,
    total_failure_error,
)
from upmovies.llm.types import CallLog, StageGateway, Usage
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.news.resolve import resolve_google_news_url
from upmovies.news.visibility import visible_events
from upmovies.synthesize.deterministic import DETERMINISTIC_MODEL
from upmovies.synthesize.store import upsert_summary
from upmovies.synthesize.summarizer import (
    EventInput,
    StoryInput,
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


def _has_a_story() -> ColumnElement[bool]:
    """The summarizer paraphrases stories; with none attached it would be inventing prose from
    an event type and a film title, which is exactly the fabrication risk ADR-0014 rejected an
    LLM body over for catalog-sourced events. Story-triggered events always satisfy this — it
    exists for the story-less ones catalog events introduced."""
    return select(1).where(EventStory.event_id == Event.id).exists()


def _superseded_deterministic() -> ColumnElement[bool]:
    """The one exception to write-once: a catalog-sourced event whose deterministic body has
    since acquired stories (ADR-0014 §Presentation). Selecting it lets the real summarizer
    replace the template in place — `EventSummary` is keyed on the event, so the card upgrades
    with no special path.

    Both guards are load-bearing: `model` is the sentinel, so an LLM summary is still
    write-once; `edited_at` is NULL, so an admin's wording is not silently overwritten and the
    reset action remains the only way back to a machine summary. (The story that does the
    superseding is required of every selected event by `_has_a_story`.)"""
    return and_(
        EventSummary.model == DETERMINISTIC_MODEL,
        EventSummary.edited_at.is_(None),
    )


async def _select_pending(session: AsyncSession) -> list[_PendingEvent]:
    """User-facing events with at least one story to summarize (`_has_a_story`) that need one:
    those with no summary row yet, plus catalog-sourced events whose deterministic body is now
    superseded by attached stories (`_superseded_deterministic`). Otherwise write-once — an
    event whose LLM summary already exists is never reselected, even if the event was later
    updated or the prompt version has moved on. Hidden types are skipped outright (NEU-969):
    summarizing an event the public API
    filters out spends tokens on text nobody reads, and `event_type` is immutable once set, so
    a skipped event can never later become visible and need the summary it was denied.
    Returns each mapped to an EventInput (plain dataclasses — safe to use after the session
    closes), with is_new = no prior summary existed."""
    rows = (
        await session.execute(
            select(Event, Film.title, EventSummary.event_id)
            .join(Film, Film.id == Event.film_id)
            .outerjoin(EventSummary, EventSummary.event_id == Event.id)
            .where(
                or_(EventSummary.event_id.is_(None), _superseded_deterministic()),
                _has_a_story(),
                visible_events(),
            )
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


async def _summary_stage_sequential(
    *,
    session_factory: SessionFactory,
    gateway: StageGateway,
    run_id: UUID,
    model: str,
    prompt_version: str,
    pending: list[_PendingEvent],
    run_date: date,
) -> tuple[int, int, int, Usage]:
    # One resolution for the stage, not one per event: the provider is a property of the
    # stage, and this is the pipeline's only model-facing step.
    client = gateway.for_stage("summarize")
    provider = gateway.provider_for("summarize")
    new = refreshed = failed = 0
    total_usage = Usage()
    for pe in pending:
        # Owned outside the try so an event that fails *after* its call — a runaway or
        # unparseable summary, say — still records what that call cost (NEU-975).
        calls = CallLog()
        try:
            result = await summarize_event(
                client=client,
                model=model,
                prompt_version=prompt_version,
                event=pe.event_input,
                run_date=run_date,
                calls=calls,
            )
            async with _owned_session(session_factory) as s:
                await upsert_summary(
                    s,
                    event_id=pe.event_id,
                    summary=result.summary,
                    model=result.model,
                    prompt_version=result.prompt_version,
                    source_updated_at=result.source_updated_at,
                )
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
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
        finally:
            total_usage += calls.usage
            if calls.results:
                async with _owned_session(session_factory) as s:
                    await record_llm_calls(
                        s,
                        run_id,
                        stage="summarize",
                        provider=provider,
                        model=model,
                        results=calls.results,
                    )
                    await s.commit()
    return new, refreshed, failed, total_usage


async def run_synthesize_ingest(
    *,
    session_factory: SessionFactory,
    gateway: StageGateway,
    run_id: UUID,
    model: str,
    prompt_version: str,
    url_resolve_per_run: int = 500,
    url_resolve_max_attempts: int = 3,
    url_resolve_delay_seconds: float = 1.0,
    url_resolve_resolver: Resolver = resolve_google_news_url,
) -> SynthesizeResult:
    async with _owned_session(session_factory) as s:
        pending = await _select_pending(s)

    run_date = datetime.now(UTC).date()
    new, refreshed, failed, summary_usage = await _summary_stage_sequential(
        session_factory=session_factory,
        gateway=gateway,
        run_id=run_id,
        model=model,
        prompt_version=prompt_version,
        pending=pending,
        run_date=run_date,
    )
    async with _owned_session(session_factory) as s:
        await record_llm_usage(
            s,
            run_id,
            stage="summarize",
            provider=gateway.provider_for("summarize"),
            model=model,
            usage=summary_usage,
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

    error = total_failure_error(
        summarize=StageCounts(processed=new + refreshed, failed=failed),
    )
    async with _owned_session(session_factory) as s:
        await finalize_run(
            s,
            run_id,
            status="failed" if error else "succeeded",
            error=error,
            detail=(
                f"summarized {new + refreshed} ({new} new, {refreshed} refreshed); {failed} failed"
                f"; urls marked {resolve_result.marked}, resolved {resolve_result.resolved},"
                f" failed {resolve_result.failed}, pending {resolve_result.pending}"
            ),
        )
        await s.commit()
    return SynthesizeResult(new=new, refreshed=refreshed, failed=failed)
