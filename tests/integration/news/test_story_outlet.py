from sqlalchemy import select

from upmovies.news.models import Story


async def test_story_outlet_column_roundtrips(session):
    session.add(
        Story(
            source="Google News: per-film",
            url="https://news.example/x",
            title="Headline - Deadline",
            outlet="Deadline",
        )
    )
    await session.commit()
    loaded = (
        await session.execute(select(Story).where(Story.url == "https://news.example/x"))
    ).scalar_one()
    assert loaded.outlet == "Deadline"


async def test_story_outlet_defaults_to_none(session):
    session.add(Story(source="Deadline", url="https://deadline.com/y", title="Trade story"))
    await session.commit()
    loaded = (
        await session.execute(select(Story).where(Story.url == "https://deadline.com/y"))
    ).scalar_one()
    assert loaded.outlet is None
