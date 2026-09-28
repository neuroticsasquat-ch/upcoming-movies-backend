"""The one-off purge of films pre-NEU-1505 imports admitted already outside the alert window
(NEU-1508).

What matters is the blast radius: it runs against production once and is not reversible, so it
must take an import's long-released film, its follows, its events and their digest rows, keep
its stories (unlinked), and leave every film that was upcoming when first observed — however
long ago it has opened since, and whatever TMDB has re-dated it to — exactly where it is.
"""

from datetime import UTC, date, datetime, timedelta
from xml.etree import ElementTree

from sqlalchemy import func, select

from scripts.purge_imported_released_films import DEFAULT_CUTOFF, IMPORTS_SHIPPED, purge
from tests.fixtures.catalog import add_film
from upmovies.app.models import Follow, Notification, User
from upmovies.catalog.models import Film, FilmFieldChange
from upmovies.catalog.ref import film_ref
from upmovies.config import get_settings
from upmovies.news.models import Event, EventSummary, Story

# The night of the incident import. Fixture rows are stamped with it explicitly: every column
# involved defaults to `now()`, and a fixture left to the default drifts out of the window the
# day the wall clock passes the cutoff (the NEU-1121 test learned this the hard way).
NOW = datetime(2026, 9, 23, 21, 0, tzinfo=UTC)
BUTTON = date(2008, 12, 25)
MAX_AGE_DAYS = get_settings().provider_poll_max_age_days


async def _count(session, model, *where) -> int:
    stmt = select(func.count()).select_from(model).where(*where)
    return (await session.execute(stmt)).scalar_one()


async def _film(session, tmdb_id: int, *, release_date: date | None, created_at=NOW) -> Film:
    assert IMPORTS_SHIPPED <= NOW < DEFAULT_CUTOFF
    return await add_film(
        session,
        tmdb_id,
        release_date=release_date,
        status="Released",
        slug=f"film-{tmdb_id}",
        created_at=created_at,
    )


async def _redated(session, film: Film, *, old: str | None, new: str) -> None:
    """TMDB moved the film's date after we admitted it: the trigger's row, stamped by hand so
    it lands after `NOW` and the current column says `new`."""
    film.release_date = date.fromisoformat(new)
    await session.flush()
    # The trigger wrote a row stamped with the wall clock; replace it with the one we mean.
    for row in (
        await session.execute(select(FilmFieldChange).where(FilmFieldChange.film_id == film.id))
    ).scalars():
        await session.delete(row)
    session.add(
        FilmFieldChange(
            film_id=film.id,
            field="release_date",
            old_value=old,
            new_value=new,
            changed_at=NOW + timedelta(days=2),
        )
    )
    await session.flush()


async def _candidate_ids(session) -> set[int]:
    return {f.tmdb_id for f in (await purge(session, apply=False)).films}


async def _button(session, make_user) -> tuple[Film, Story, Event, User]:
    """A pre-fix import film with everything the purge must account for: a title follow, a
    linked story, and a catalog card with a summary and a digest row."""
    user = await make_user(email="tom@example.com")
    film = await _film(session, 4922, release_date=BUTTON)
    story = Story(
        source="google_news",
        url="https://example.com/button",
        title="Fincher looks back at Benjamin Button",
        film_id=film.id,
        link_status="linked",
    )
    event = Event(
        film_id=film.id,
        event_type="crew_attached",
        confidence="rumored",
        provenance="catalog",
        occurred_at=NOW,
    )
    session.add_all([story, event])
    session.add(
        Follow(
            user_id=user.id, entity_type="title", entity_id=str(film.id), source="letterboxd_import"
        )
    )
    await session.flush()
    session.add(
        EventSummary(
            event_id=event.id,
            summary="David Fincher attached to direct.",
            model="deterministic",
            prompt_version="v1",
            source_updated_at=NOW,
        )
    )
    session.add(
        Notification(
            user_id=user.id, event_id=event.id, kind="digest", channel="email", status="sent"
        )
    )
    await session.commit()
    return film, story, event, user


async def test_a_dry_run_reports_the_film_its_follow_story_and_event_and_deletes_nothing(
    session, make_user
):
    film, story, event, user = await _button(session, make_user)

    (found,) = (await purge(session, apply=False)).films

    assert (found.film_id, found.release_date_at_creation) == (film.id, BUTTON)
    assert found.ref == film_ref(4922, film.title)
    # By user id: the dry run's output goes on the PR, and emails do not.
    assert found.follows == [(user.id, "letterboxd_import")]
    assert found.stories == [(story.id, story.title)]
    ((event_id, event_type, notifications),) = [
        (e.event_id, e.event_type, e.notifications) for e in found.events
    ]
    assert (event_id, event_type) == (event.id, "crew_attached")
    assert [(uid, status) for uid, status, _ in notifications] == [(user.id, "sent")]
    assert await _count(session, Film) == 1
    assert await _count(session, Follow) == 1
    assert await _count(session, Event) == 1


async def test_apply_removes_the_film_follow_and_card_and_unlinks_the_story(session, make_user):
    film, story, event, _ = await _button(session, make_user)
    film_id, story_id, event_id = film.id, story.id, event.id

    await purge(session, apply=True)
    session.expire_all()

    assert await _count(session, Film, Film.id == film_id) == 0
    assert await _count(session, Follow) == 0
    assert await _count(session, Event, Event.id == event_id) == 0
    assert await _count(session, EventSummary, EventSummary.event_id == event_id) == 0
    assert await _count(session, Notification, Notification.event_id == event_id) == 0
    # The article is fetched, not ours to lose; it just no longer claims a link (D-1508.3).
    kept = await session.get(Story, story_id)
    assert kept is not None
    assert (kept.film_id, kept.link_status) == (None, "rejected")


async def test_a_film_upcoming_when_first_observed_is_untouched(session):
    # Admitted a week before it opened, and TMDB has since pulled the date in: the film is
    # released now, but it was upcoming on the day it arrived — the ticket's acceptance case.
    opened = await _film(session, 101, release_date=date(2026, 9, 30))
    await _redated(session, opened, old="2026-09-30", new="2026-09-24")
    # The sweep case seen locally: admitted undated, then back-dated by years.
    undated = await _film(session, 102, release_date=None)
    await _redated(session, undated, old=None, new="2017-03-11")
    await session.commit()

    assert await _candidate_ids(session) == set()


async def test_the_window_not_release_day_is_the_line(session):
    # EF-21 still admits a film that opened inside the window; an import today would offer it.
    today = NOW.date()
    await _film(session, 201, release_date=today - timedelta(days=MAX_AGE_DAYS - 65))
    await _film(session, 202, release_date=today - timedelta(days=MAX_AGE_DAYS + 1))
    await session.commit()

    assert await _candidate_ids(session) == {202}


async def test_the_creation_bounds_hold(session):
    await _film(session, 301, release_date=BUTTON, created_at=IMPORTS_SHIPPED - timedelta(days=1))
    await _film(session, 302, release_date=BUTTON, created_at=DEFAULT_CUTOFF)
    await _film(session, 303, release_date=BUTTON, created_at=DEFAULT_CUTOFF - timedelta(seconds=1))
    await session.commit()

    assert await _candidate_ids(session) == {303}


async def test_a_purged_film_leaves_its_page_the_sitemap_and_search(session, client, add_event):
    film = await _film(session, 4922, release_date=BUTTON)
    film.title = "The Curious Case of Benjamin Button"
    await session.commit()
    await add_event(film=film, summary="Fincher's 2008 film.")
    old_ref = film_ref(film.tmdb_id, film.title)

    async def visible() -> tuple[int, bool, bool]:
        page = await client.get(f"/films/{old_ref}")
        sitemap = ElementTree.fromstring((await client.get("/sitemap.xml")).text)
        in_sitemap = any(old_ref in (el.text or "") for el in sitemap.iter())
        search = await client.get("/films/search", params={"q": "benjamin button"})
        in_search = old_ref in [i["ref"] for i in search.json()["items"]]
        return page.status_code, in_sitemap, in_search

    assert await visible() == (200, True, True)

    await purge(session, apply=True)

    assert await visible() == (404, False, False)
