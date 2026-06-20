import pytest
from sqlalchemy.exc import IntegrityError

from upmovies.ingest.models import IngestRun
from upmovies.news.models import Story


async def test_story_link_state_defaults(session):
    story = Story(source="Deadline", url="https://example.com/a", title="A")
    session.add(story)
    await session.commit()
    await session.refresh(story)
    assert story.link_status == "pending"
    assert story.link_confidence is None
    assert story.linked_at is None
    assert story.link_note is None


async def test_story_link_status_rejects_invalid_value(session):
    story = Story(source="Deadline", url="https://example.com/b", title="B", link_status="bogus")
    session.add(story)
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_ingest_run_accepts_link_kind(session):
    run = IngestRun(kind="link", status="running")
    session.add(run)
    await session.commit()
    await session.refresh(run)
    assert run.kind == "link"


async def test_ingest_run_rejects_unknown_kind(session):
    run = IngestRun(kind="bogus", status="running")
    session.add(run)
    with pytest.raises(IntegrityError):
        await session.commit()
