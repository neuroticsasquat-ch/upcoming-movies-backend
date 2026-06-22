from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from scripts.measure_link_cost import select_corpus
from upmovies.news.models import Story


async def _story(url, *, offset_days, status="pending"):
    now = datetime.now(UTC)
    return Story(
        source="X",
        url=url,
        title="Runner news",
        published_at=now - timedelta(days=offset_days),
        link_status=status,
        raw={"summary": ""},
    )


async def test_select_corpus_returns_pending_in_window_only(session):
    session.add_all(
        [
            await _story("https://e/in", offset_days=1),  # pending, in window
            await _story("https://e/old", offset_days=10),  # pending, out of window
            await _story("https://e/linked", offset_days=1, status="linked"),  # not pending
        ]
    )
    await session.commit()

    ids = await select_corpus(session, recency_days=4, limit=None)

    rows = {s.url: s.id for s in (await session.execute(select(Story))).scalars().all()}
    assert ids == [rows["https://e/in"]]


async def test_select_corpus_respects_limit(session):
    session.add_all(
        [
            await _story("https://e/a", offset_days=1),
            await _story("https://e/b", offset_days=1),
            await _story("https://e/c", offset_days=1),
        ]
    )
    await session.commit()
    ids = await select_corpus(session, recency_days=4, limit=2)
    assert len(ids) == 2


async def test_select_corpus_uses_fetched_at_when_published_at_is_null(session):
    now = datetime.now(UTC)
    session.add(
        Story(
            source="X",
            url="https://e/no-pub-date",
            title="Runner news",
            published_at=None,
            fetched_at=now - timedelta(days=1),  # in window
            link_status="pending",
            raw={"summary": ""},
        )
    )
    await session.commit()

    ids = await select_corpus(session, recency_days=4, limit=None)
    assert len(ids) == 1
