from sqlalchemy import func, select

from upmovies.news.models import Event, EventStory, Story


async def _event_with_source(make_film, add_event, *, slug, url):
    film = await make_film(slug=slug)
    return await add_event(
        film=film,
        summary="Bogus recast.",
        sources=({"url": url, "source": "ScreenRant", "title": "the next Batman"},),
    )


# --- gating ---


async def test_delink_requires_auth(client, make_film, add_event):
    event = await _event_with_source(make_film, add_event, slug="g1", url="https://x.test/a")
    r = await client.post(f"/admin/events/{event.id}/delink", json={"url": "https://x.test/a"})
    assert r.status_code == 401


async def test_delink_forbidden_for_non_admin(authed_client, make_film, add_event):
    event = await _event_with_source(make_film, add_event, slug="g2", url="https://x.test/a")
    r = await authed_client.post(
        f"/admin/events/{event.id}/delink", json={"url": "https://x.test/a"}
    )
    assert r.status_code == 403


# --- behavior ---


async def test_admin_delink_removes_empty_event(admin_authed_client, session, make_film, add_event):
    event = await _event_with_source(make_film, add_event, slug="b1", url="https://x.test/a")
    r = await admin_authed_client.post(
        f"/admin/events/{event.id}/delink", json={"url": "https://x.test/a"}
    )
    assert r.status_code == 200
    assert r.json() == {"delinked": 1, "event_removed": True, "resummarize_queued": False}
    assert (await session.get(Event, event.id, populate_existing=True)) is None
    story = (
        await session.execute(select(Story).where(Story.url == "https://x.test/a"))
    ).scalar_one()
    assert story.link_status == "rejected" and story.film_id is None


async def test_admin_delete_event(admin_authed_client, session, make_film, add_event):
    film = await make_film(slug="b2")
    event = await add_event(
        film=film,
        summary="junk",
        sources=(
            {"url": "https://x.test/a", "source": "A", "title": "ta"},
            {"url": "https://x.test/b", "source": "B", "title": "tb"},
        ),
    )
    r = await admin_authed_client.delete(f"/admin/events/{event.id}")
    assert r.status_code == 200
    assert r.json()["delinked"] == 2 and r.json()["event_removed"] is True
    assert (await session.get(Event, event.id, populate_existing=True)) is None
    assert (
        await session.execute(
            select(func.count()).select_from(EventStory).where(EventStory.event_id == event.id)
        )
    ).scalar_one() == 0


async def test_admin_delink_unknown_event_404(admin_authed_client):
    from uuid import uuid4

    r = await admin_authed_client.post(
        f"/admin/events/{uuid4()}/delink", json={"url": "https://x.test/a"}
    )
    assert r.status_code == 404


async def test_admin_delink_url_not_in_event_404(admin_authed_client, make_film, add_event):
    event = await _event_with_source(make_film, add_event, slug="b3", url="https://x.test/a")
    r = await admin_authed_client.post(
        f"/admin/events/{event.id}/delink", json={"url": "https://x.test/missing"}
    )
    assert r.status_code == 404
