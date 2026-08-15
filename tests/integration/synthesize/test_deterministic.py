"""The deterministic writer and the one hole it punches in write-once selection (ADR-0014)."""

from datetime import UTC, date, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.ingest.models import LLMCall, RunLLMUsage
from upmovies.news.models import Event, EventStory, EventSummary, Story
from upmovies.synthesize.deterministic import (
    DETERMINISTIC_MODEL,
    TEMPLATE_VERSION,
    CreditAttached,
    ReleaseDateChanged,
    write_deterministic_summary,
)
from upmovies.synthesize.pipeline import _select_pending


async def _film(session, *, tmdb_id=1, title="Runner"):
    film = Film(tmdb_id=tmdb_id, title=title)
    session.add(film)
    await session.flush()
    return film


async def _catalog_event(session, film, *, event_type="release_date"):
    event = Event(
        film_id=film.id,
        event_type=event_type,
        confidence="rumored",
        occurred_at=datetime.now(UTC),
        provenance="catalog",
    )
    session.add(event)
    await session.flush()
    return event


async def _attach_story(session, event, *, url="https://trade/1"):
    story = Story(
        source="Deadline",
        url=url,
        title="Headline",
        published_at=datetime.now(UTC),
        raw={"summary": "A dek."},
    )
    session.add(story)
    await session.flush()
    session.add(EventStory(event_id=event.id, story_id=story.id))
    await session.flush()
    return story


async def _summary(session, event_id):
    return (
        await session.execute(
            select(EventSummary).where(EventSummary.event_id == event_id),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()


async def test_writer_persists_the_templated_body_with_the_sentinel_model(session):
    film = await _film(session)
    event = await _catalog_event(session, film)

    body = await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    assert body == "US wide release date set to 14 August 2026."
    row = await _summary(session, event.id)
    assert row.summary == "US wide release date set to 14 August 2026."
    assert row.model == DETERMINISTIC_MODEL
    assert row.prompt_version == TEMPLATE_VERSION
    assert row.source_updated_at == event.updated_at


async def test_writer_is_idempotent_on_the_one_row_per_event(session):
    film = await _film(session)
    event = await _catalog_event(session, film)

    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=CreditAttached(role="director", name="Denis Villeneuve"),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    rows = (
        (
            await session.execute(
                select(EventSummary).where(EventSummary.event_id == event.id),
                execution_options={"populate_existing": True},
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].summary == "Denis Villeneuve attached to direct."


async def test_the_sentinel_never_reaches_the_cost_ledger(session):
    """`ingest.llm_call` and `ingest.run_llm_usage` are the system's cost tables. The writer
    makes no model call, so it must leave no trace in either — a row there would price tokens
    that were never spent (ADR-0014)."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    calls = (await session.execute(select(LLMCall))).scalars().all()
    usage = (await session.execute(select(RunLLMUsage))).scalars().all()
    assert calls == []
    assert usage == []


async def test_a_deterministic_summary_alone_is_not_reselected(session):
    """No stories means nothing for the summarizer to work from — reselecting would hand it a
    field diff to write prose about, the exact fabrication risk ADR-0014 rejected."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    assert await _select_pending(session) == []


async def test_an_attached_story_supersedes_the_deterministic_summary(session):
    """The supersession contract: once a trade story clusters onto a catalog event, the real
    summarizer picks it up and the LLM body replaces the template in place."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await _attach_story(session, event)
    await session.commit()

    pending = await _select_pending(session)

    assert [pe.event_id for pe in pending] == [event.id]
    # Not a new summary — the run counts it as refreshed, so the detail line distinguishes an
    # upgraded card from a first-ever one.
    assert pending[0].is_new is False
    assert [s.dek for s in pending[0].event_input.stories] == ["A dek."]


async def test_an_llm_summary_stays_write_once_when_a_story_attaches(session):
    """Supersession is scoped to the sentinel. An event the summarizer already wrote is never
    rewritten just because another story joined it."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    session.add(
        EventSummary(
            event_id=event.id,
            summary="An LLM body.",
            model="claude-haiku-4-5",
            prompt_version="9",
            source_updated_at=event.updated_at,
        )
    )
    await _attach_story(session, event)
    await session.commit()

    assert await _select_pending(session) == []


async def test_an_edited_deterministic_summary_is_left_alone(session):
    """A human edit outranks supersession — the reset action stays the only way back to a
    machine summary."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()
    row = await _summary(session, event.id)
    row.edited_at = datetime.now(UTC)
    await _attach_story(session, event)
    await session.commit()

    assert await _select_pending(session) == []


async def test_the_writer_never_walks_an_llm_summary_back_to_a_template(session):
    """Supersession is one-directional (ADR-0014). A second catalog change on an event the
    summarizer has already taken over — the status moves again, another credit lands — must not
    replace real, sourced prose with a template."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    session.add(
        EventSummary(
            event_id=event.id,
            summary="An LLM body.",
            model="claude-haiku-4-5",
            prompt_version="9",
            source_updated_at=event.updated_at,
        )
    )
    await session.commit()

    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=CreditAttached(role="director", name="Denis Villeneuve"),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    row = await _summary(session, event.id)
    assert row.summary == "An LLM body."
    assert row.model == "claude-haiku-4-5"


async def test_the_writer_never_overwrites_an_admin_edit(session):
    """`edited_at` survives the upsert, so an unguarded write would leave template text flagged
    as human-authored on the card."""
    film = await _film(session)
    event = await _catalog_event(session, film)
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 8, 14)),
        source_updated_at=event.updated_at,
    )
    await session.commit()
    row = await _summary(session, event.id)
    row.summary = "An admin's wording."
    row.edited_at = datetime.now(UTC)
    await session.commit()

    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=ReleaseDateChanged(region="US", label="wide", new_date=date(2026, 10, 2)),
        source_updated_at=event.updated_at,
    )
    await session.commit()

    assert (await _summary(session, event.id)).summary == "An admin's wording."


async def test_an_event_with_no_stories_is_never_selected_for_the_summarizer(session):
    """A catalog event can lose its summary row — `reset_summary` deletes it — and would then
    look like any un-summarized event. With no stories the summarizer has only a field diff to
    work from, so it must not be handed the event at all (ADR-0014 rejected exactly that)."""
    film = await _film(session)
    await _catalog_event(session, film)
    await session.commit()

    assert await _select_pending(session) == []
