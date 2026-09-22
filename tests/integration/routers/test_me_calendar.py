"""`GET /me/calendar` — the my films calendar, narrowed to the caller's title follows (D-34,
D-39, EF-14).

The shape, paging and ordering are the public `/calendar`'s exactly, so the frontend can render
both tabs through one component; the only thing that differs is which films. That set is the
`.ics` feed's — a reader whose subscribed calendar and on-screen calendar disagreed would be
right to file it as a bug — so the tests that matter most are the ones pinning the two together
and the ones pinning this route's set apart from the public listing's.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import delete as sa_delete

from tests.fixtures.ical import events, prop_dt, prop_text
from tests.fixtures.public import ref
from tests.fixtures.users import ENTITLED_UNTIL, _build_authed_client
from upmovies.app.models import Follow

_FUTURE = datetime(2099, 7, 4, 0, 0, tzinfo=UTC)
_LATER = datetime(2099, 9, 1, 0, 0, tzinfo=UTC)
_LATEST = datetime(2099, 11, 2, 0, 0, tzinfo=UTC)
_PAST = datetime(2000, 1, 1, 0, 0, tzinfo=UTC)


@pytest.fixture
def follow_film(session):
    """Follow a film by title — the whole of what puts it on this calendar (EF-14).

    Writes the row rather than posting to `/me/follows` because what is under test here is the
    calendar's film set, and a fixture that went through the route would be testing the follow
    verb twice."""

    async def _add(*, user, film, source: str = "manual") -> None:
        session.add(
            Follow(
                user_id=user.id,
                entity_type="title",
                entity_id=str(film.id),
                source=source,
            )
        )
        await session.commit()

    return _add


@pytest.fixture
async def make_entitled_client(session, make_user):
    """A second (third, …) signed-in subscriber, for the leakage tests. `entitled_client`
    gives one; these tests need two users to tell apart."""
    clients: list[AsyncClient] = []

    async def _make(*, email: str) -> AsyncClient:
        user = await make_user(email=email, entitled_until=ENTITLED_UNTIL)
        client = await _build_authed_client(session, user)
        clients.append(client)
        return client

    yield _make

    for client in clients:
        await client.aclose()


def _refs(body: dict) -> list[str]:
    return [item["film_ref"] for item in body["items"]]


# --- the gate ------------------------------------------------------------------------------


async def test_me_calendar_requires_auth(client):
    r = await client.get("/me/calendar")
    assert r.status_code == 401


async def test_me_calendar_is_403_for_an_unentitled_user(
    authed_client, make_film, add_release_date, follow_film
):
    # A followed film, so the refusal is the gate and not an empty set: 403 rather than
    # an empty collection is what lets the client render the D-41 locked panel (D-39).
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await follow_film(user=authed_client.user, film=film)

    r = await authed_client.get("/me/calendar")

    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


# --- the envelope --------------------------------------------------------------------------


async def test_following_no_films_is_an_empty_page_not_an_error(entitled_client):
    r = await entitled_client.get("/me/calendar")

    assert r.status_code == 200
    assert r.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}


# --- which films ---------------------------------------------------------------------------


async def test_only_the_callers_own_follows_reach_their_calendar(
    entitled_client, make_entitled_client, make_film, add_release_date, follow_film
):
    other = await make_entitled_client(email="other@example.com")
    mine = await make_film(slug="mine", title="My Film")
    theirs = await make_film(slug="theirs", title="Their Film")
    unwatched = await make_film(slug="nobodys", title="Nobody's Film")
    for film in (mine, theirs, unwatched):
        await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await follow_film(user=entitled_client.user, film=mine)
    await follow_film(user=other.user, film=theirs)

    ours = (await entitled_client.get("/me/calendar")).json()
    yours = (await other.get("/me/calendar")).json()

    assert _refs(ours) == [ref(mine)]
    assert _refs(yours) == [ref(theirs)]


async def test_a_film_reached_only_through_a_director_follow_is_not_on_the_calendar(
    entitled_client, session, make_film, add_release_date, attach_credits
):
    # EF-14, the cutover: an entity follow delivers that entity's attachment cards, not a place
    # on a date list (EF-3). This is the assertion that used to say the opposite, twice over.
    film = await make_film(slug="followed", title="Followed Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await attach_credits(film, crew=[{"id": 900, "name": "A Director", "job": "Director"}])
    session.add(
        Follow(
            user_id=entitled_client.user.id,
            entity_type="person",
            entity_id="900",
            source="manual",
        )
    )
    await session.commit()

    assert _refs((await entitled_client.get("/me/calendar")).json()) == []


async def test_a_released_film_a_company_follow_reaches_is_not_on_the_calendar(
    entitled_client, session, make_film, add_release_date, attach_companies
):
    # The same rule at the other end of the alert window: the window bounded what an *indirect*
    # follow covered, and there is no indirect coverage left for it to bound (EF-14).
    film = await make_film(
        slug="opened",
        title="Opened Film",
        status="Released",
        release_date=date.today() - timedelta(days=300),
    )
    await add_release_date(film=film, release_date=_FUTURE, release_type=4)
    await attach_companies(film, [(711, "A Studio")])
    session.add(
        Follow(
            user_id=entitled_client.user.id,
            entity_type="company",
            entity_id="711",
            source="manual",
        )
    )
    await session.commit()

    assert _refs((await entitled_client.get("/me/calendar")).json()) == []


async def test_a_title_follow_puts_a_long_released_film_on_the_calendar(
    entitled_client, make_film, add_release_date, follow_film
):
    # The other half of EF-14: a title follow carries no window and no status term at all, so
    # the film the user named years ago keeps its place for the home-release date still to come.
    film = await make_film(
        slug="opened",
        title="Opened Film",
        status="Released",
        release_date=date.today() - timedelta(days=300),
    )
    await add_release_date(film=film, release_date=_FUTURE, release_type=4)
    await follow_film(user=entitled_client.user, film=film)

    assert _refs((await entitled_client.get("/me/calendar")).json()) == [ref(film)]


async def test_unfollowing_takes_a_film_off_the_calendar(
    entitled_client, session, make_film, add_release_date, follow_film
):
    # EF-14: unfollowing is the only way a film leaves this page now — the mute that used to do
    # it (D-45) went with the watchlist it corrected.
    film = await make_film(slug="dropped", title="Dropped Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await follow_film(user=entitled_client.user, film=film)
    assert _refs((await entitled_client.get("/me/calendar")).json()) == [ref(film)]

    await session.execute(sa_delete(Follow).where(Follow.user_id == entitled_client.user.id))
    await session.commit()

    assert _refs((await entitled_client.get("/me/calendar")).json()) == []


async def test_a_followed_film_with_no_slug_is_absent(
    entitled_client, make_film, add_release_date, follow_film
):
    # No page to link to, as on every other public surface.
    film = await make_film(slug=None, title="No Page Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await follow_film(user=entitled_client.user, film=film)

    assert _refs((await entitled_client.get("/me/calendar")).json()) == []


async def test_the_public_calendars_noise_cuts_do_not_apply(
    entitled_client, client, make_film, add_release_date, follow_film
):
    # The popularity, runtime and adult filters keep noise off a public listing. A film the
    # user followed by name is not noise to them (D-1411.2) — and applying the cuts
    # here would make this page disagree with the same user's subscribed `.ics`.
    #
    # One film per cut rather than one film tripping all three: a single film would be held off
    # the public route by the popularity floor alone, which would let a cut still being applied
    # here hide behind the other two.
    unpopular = await make_film(slug="unpopular", title="Unpopular Film", popularity=0.1)
    short = await make_film(slug="short", title="Short Film", popularity=10.0, runtime=40)
    adult = await make_film(slug="adult", title="Adult Film", popularity=10.0, adult=True)
    for film in (unpopular, short, adult):
        await add_release_date(film=film, release_date=_FUTURE, release_type=3)
        await follow_film(user=entitled_client.user, film=film)

    assert set(_refs((await entitled_client.get("/me/calendar")).json())) == {
        ref(unpopular),
        ref(short),
        ref(adult),
    }
    assert _refs((await client.get("/calendar")).json()) == []


# --- the window ----------------------------------------------------------------------------


async def test_a_followed_film_whose_only_date_is_past_is_absent(
    entitled_client, make_film, add_release_date, follow_film
):
    # Upcoming-only, like the public calendar and unlike the `.ics` feed's past window
    # (D-1411.1): the page opens on what is coming, not on last year.
    film = await make_film(slug="released", title="Released Film")
    await add_release_date(film=film, release_date=_PAST, release_type=3)
    await follow_film(user=entitled_client.user, film=film)

    assert _refs((await entitled_client.get("/me/calendar")).json()) == []


async def test_a_past_limited_date_drops_while_the_future_wide_date_stays(
    entitled_client, make_film, add_release_date, follow_film
):
    # The buckets are independent subjects: one being past does not take the other with it.
    film = await make_film(slug="two-buckets", title="Two Buckets")
    await add_release_date(film=film, release_date=_PAST, release_type=2)
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await follow_film(user=entitled_client.user, film=film)

    body = (await entitled_client.get("/me/calendar")).json()

    assert [(i["film_ref"], i["release_type"]) for i in body["items"]] == [(ref(film), "wide")]


# --- agreement with the subscribed feed ------------------------------------------------------


async def test_the_dates_are_the_ics_feeds_dates(
    entitled_client, client, make_film, add_release_date, follow_film
):
    """The claim the ticket exists to protect: the on-screen calendar and the subscribed one
    carry the same subjects on the same days, including the governing-date collapse."""
    film = await make_film(slug="four-ways", title="Four Ways")
    # `limited` and `wide` share a day on purpose: the subject is (film, bucket), so a
    # comparison that dropped the bucket would let the two surfaces disagree about which
    # bucket landed on that date and still pass.
    for release_type, when in ((2, _FUTURE), (3, _FUTURE), (4, _LATER)):
        await add_release_date(film=film, release_date=when, release_type=release_type)
    # A second row in the physical bucket, earlier than the first: the governing date is the
    # earliest in the subject (NEU-1206), and both surfaces must collapse it the same way.
    await add_release_date(film=film, release_date=_LATEST, release_type=5)
    await add_release_date(film=film, release_date=_LATEST - timedelta(days=7), release_type=5)
    await follow_film(user=entitled_client.user, film=film)

    token = (await entitled_client.get("/me/settings")).json()["ical_token"]
    feed = (await client.get(f"/calendar/{token}.ics")).text
    body = (await entitled_client.get("/me/calendar")).json()

    # The feed carries the ref in DESCRIPTION's last path segment and the bucket as the UID's
    # second half (`{film_id}-{bucket}@backlotter`) — the same two halves the JSON row names
    # outright.
    from_feed = {
        (
            prop_text(v, "description").rsplit("/", 1)[-1],
            prop_text(v, "uid").split("@", 1)[0].rsplit("-", 1)[-1],
            prop_dt(v, "dtstart"),
        )
        for v in events(feed)
    }
    from_json = {
        (item["film_ref"], item["release_type"], date.fromisoformat(item["release_date"]))
        for item in body["items"]
    }
    assert len(from_json) == 4
    assert from_json == from_feed


# --- paging and ordering ---------------------------------------------------------------------


async def test_paging_counts_dates_not_film_rows(
    entitled_client, make_film, add_release_date, follow_film
):
    soonest = await make_film(slug="soonest", title="Soonest")
    alongside = await make_film(slug="alongside", title="Alongside")
    middle = await make_film(slug="middle", title="Middle")
    last = await make_film(slug="last", title="Last")
    for film, when in (
        (soonest, _FUTURE),
        (alongside, _FUTURE),
        (middle, _LATER),
        (last, _LATEST),
    ):
        await add_release_date(film=film, release_date=when, release_type=3)
        await follow_film(user=entitled_client.user, film=film)

    page_one = (await entitled_client.get("/me/calendar?limit=2")).json()
    page_two = (await entitled_client.get("/me/calendar?limit=2&offset=2")).json()

    # Two films share the soonest date and are one page unit, so `limit=2` is three rows.
    assert page_one["total"] == 3
    assert set(_refs(page_one)) == {ref(soonest), ref(alongside), ref(middle)}
    assert _refs(page_two) == [ref(last)]


async def test_ordering_within_a_date_is_the_public_routes(
    entitled_client, make_film, add_release_date, follow_film
):
    # wide, limited, digital, physical; within a bucket, more popular first.
    wide = await make_film(slug="wide", title="Wide", popularity=1.0)
    limited_popular = await make_film(slug="limited-hit", title="Limited Hit", popularity=9.0)
    limited_quiet = await make_film(slug="limited-quiet", title="Limited Quiet", popularity=2.0)
    digital = await make_film(slug="digital", title="Digital", popularity=50.0)
    physical = await make_film(slug="physical", title="Physical", popularity=99.0)
    for film, release_type in (
        (wide, 3),
        (limited_popular, 2),
        (limited_quiet, 2),
        (digital, 4),
        (physical, 5),
    ):
        await add_release_date(film=film, release_date=_FUTURE, release_type=release_type)
        await follow_film(user=entitled_client.user, film=film)

    body = (await entitled_client.get("/me/calendar")).json()

    assert _refs(body) == [
        ref(wide),
        ref(limited_popular),
        ref(limited_quiet),
        ref(digital),
        ref(physical),
    ]


# --- the item ---------------------------------------------------------------------------------


async def test_an_item_carries_the_public_routes_decoration(
    entitled_client,
    client,
    make_film,
    add_release_date,
    follow_film,
    attach_credits,
    attach_genres,
):
    film = await make_film(
        slug="decorated",
        title="Decorated Film",
        popularity=10.0,
        release_date=date(2099, 7, 4),
        poster_path="/decorated.jpg",
    )
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await attach_credits(
        film,
        cast=[
            {"id": 1, "name": "First Billed", "credit_order": 0},
            {"id": 2, "name": "Second Billed", "credit_order": 1},
        ],
        crew=[{"id": 3, "name": "The Director", "job": "Director"}],
    )
    await attach_genres(film, [(18, "Drama"), (35, "Comedy")])
    await follow_film(user=entitled_client.user, film=film)

    (mine,) = (await entitled_client.get("/me/calendar")).json()["items"]
    (public,) = (await client.get("/calendar")).json()["items"]

    assert mine == public
    assert mine["director"] == "The Director"
    assert mine["stars"] == ["First Billed", "Second Billed"]
    assert mine["genres"] == ["Comedy", "Drama"]
    assert mine["release_year"] == 2099
    assert mine["poster_path"] == "/decorated.jpg"
