from upmovies.news.models import Story


async def test_story_resolution_defaults(session):
    story = Story(
        source="Google News: per-film",
        url="https://news.google.com/rss/articles/X",
        title="Some headline",
    )
    session.add(story)
    await session.commit()
    await session.refresh(story)
    assert story.resolved_url is None
    assert story.resolve_state == "none"
    assert story.resolve_attempts == 0
