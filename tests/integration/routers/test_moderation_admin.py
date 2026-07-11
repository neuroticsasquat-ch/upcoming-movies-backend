from sqlalchemy import func, select

from upmovies.news.models import Event, EventStory, EventSummary, Story


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


# --- PATCH /{event_id}/summary (edit) ---


async def test_edit_summary_requires_auth(client, make_film, add_event):
    film = await make_film(slug="es-auth")
    event = await add_event(film=film, summary="AI text.")
    r = await client.patch(f"/admin/events/{event.id}/summary", json={"summary": "Human."})
    assert r.status_code == 401


async def test_edit_summary_forbidden_for_non_admin(authed_client, make_film, add_event):
    film = await make_film(slug="es-forbid")
    event = await add_event(film=film, summary="AI text.")
    r = await authed_client.patch(f"/admin/events/{event.id}/summary", json={"summary": "Human."})
    assert r.status_code == 403


async def test_edit_summary_sets_text_and_marker(
    admin_authed_client, session, make_film, add_event
):
    film = await make_film(slug="es-ok")
    event = await add_event(film=film, summary="AI text.")
    r = await admin_authed_client.patch(
        f"/admin/events/{event.id}/summary", json={"summary": "  Human-authored text.  "}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["summary"] == "Human-authored text."  # stripped
    assert body["edited"] is True and body["edited_at"] is not None

    row = await session.get(EventSummary, event.id, execution_options={"populate_existing": True})
    assert row is not None
    assert row.summary == "Human-authored text."
    assert row.edited_at is not None
    assert row.edited_by == admin_authed_client.user.id


async def test_edit_summary_rejects_empty(admin_authed_client, make_film, add_event):
    film = await make_film(slug="es-empty")
    event = await add_event(film=film, summary="AI text.")
    r = await admin_authed_client.patch(
        f"/admin/events/{event.id}/summary", json={"summary": "   "}
    )
    assert r.status_code == 422


async def test_edit_summary_rejects_over_length(admin_authed_client, make_film, add_event):
    film = await make_film(slug="es-long")
    event = await add_event(film=film, summary="AI text.")
    r = await admin_authed_client.patch(
        f"/admin/events/{event.id}/summary", json={"summary": "x" * 501}
    )
    assert r.status_code == 422


async def test_edit_summary_accepts_max_length(admin_authed_client, make_film, add_event):
    film = await make_film(slug="es-max")
    event = await add_event(film=film, summary="AI text.")
    r = await admin_authed_client.patch(
        f"/admin/events/{event.id}/summary", json={"summary": "x" * 500}
    )
    assert r.status_code == 200


async def test_edit_summary_unknown_event_404(admin_authed_client):
    from uuid import uuid4

    r = await admin_authed_client.patch(
        f"/admin/events/{uuid4()}/summary", json={"summary": "Human."}
    )
    assert r.status_code == 404


# --- DELETE /{event_id}/summary (reset-to-AI) ---


async def test_reset_summary_deletes_edited_row(admin_authed_client, session, make_film, add_event):
    film = await make_film(slug="rs-ok")
    event = await add_event(film=film, summary="AI text.")
    await admin_authed_client.patch(f"/admin/events/{event.id}/summary", json={"summary": "Human."})

    r = await admin_authed_client.delete(f"/admin/events/{event.id}/summary")
    assert r.status_code == 204
    # Row gone → event re-enters _select_pending's no-summary branch next synthesize run.
    assert (
        await session.get(EventSummary, event.id, execution_options={"populate_existing": True})
    ) is None


async def test_reset_summary_not_edited_404(admin_authed_client, session, make_film, add_event):
    film = await make_film(slug="rs-noedit")
    event = await add_event(film=film, summary="AI text.")  # machine summary, edited_at IS NULL
    r = await admin_authed_client.delete(f"/admin/events/{event.id}/summary")
    assert r.status_code == 404
    assert r.json()["detail"] == "summary_not_edited"
    # untouched
    assert (await session.get(EventSummary, event.id)) is not None


async def test_reset_summary_unknown_event_404(admin_authed_client):
    from uuid import uuid4

    r = await admin_authed_client.delete(f"/admin/events/{uuid4()}/summary")
    assert r.status_code == 404


async def test_reset_summary_requires_auth(client, make_film, add_event):
    film = await make_film(slug="rs-auth")
    event = await add_event(film=film, summary="AI text.")
    r = await client.delete(f"/admin/events/{event.id}/summary")
    assert r.status_code == 401
