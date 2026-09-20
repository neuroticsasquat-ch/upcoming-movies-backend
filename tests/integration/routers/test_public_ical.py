"""`GET /calendar/{token}.ics` — the tokenised watchlist calendar (D-34, D-39, D-40).

The route has no cookie: the token in the path is the whole credential, and the entitlement gate
is applied to the token's *owner*. So the cases that matter most are the refusals, and that they
are all the same refusal — a caller who can tell "wrong token" from "real token, lapsed grant"
can enumerate accounts.
"""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.ical import events, prop_dt, prop_text
from tests.fixtures.public import ref
from tests.fixtures.users import ENTITLED_UNTIL, _build_authed_client
from upmovies.app import tokens
from upmovies.app.models import Follow, UserSettings, WatchlistDismissal
from upmovies.catalog.models import FilmReleaseDateChange
from upmovies.config import get_settings
from upmovies.public.service import ICAL_PAST_WINDOW_DAYS

_FUTURE = datetime(2026, 12, 5, 0, 0, tzinfo=UTC)
_LATER = datetime(2027, 2, 9, 0, 0, tzinfo=UTC)


@pytest.fixture
def subscriber(session, make_user):
    """An entitled user with a settings row, and their calendar token."""

    async def _make(*, email: str = "sub@example.com", entitled_until=ENTITLED_UNTIL):
        user = await make_user(email=email, entitled_until=entitled_until)
        token = tokens.new_ical_token()
        session.add(UserSettings(user_id=user.id, ical_token=token))
        await session.commit()
        return user, token

    return _make


@pytest.fixture
def watchlist(session):
    """Put a film on a user's watchlist — which, since M8, is following it by title."""

    async def _add(*, user, film, source: str = "manual"):
        session.add(
            Follow(
                user_id=user.id,
                entity_type="title",
                entity_id=str(film.id),
                source=source,
                coverage="lead",
            )
        )
        await session.commit()

    return _add


async def _fetch(client, token: str):
    return await client.get(f"/calendar/{token}.ics")


def _events(body: str):
    return events(body)


def _summaries(body: str) -> list[str]:
    return [prop_text(v, "summary") for v in _events(body)]


# --- the refusals --------------------------------------------------------------------------


async def test_an_unknown_token_is_404(client):
    r = await _fetch(client, "not-a-real-token")
    assert r.status_code == 404


async def test_a_rotated_token_stops_resolving_and_the_new_one_works(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    # Rotation goes through the affordance that actually performs it (D-34,
    # `POST /me/settings/ical-token/rotate`) rather than by writing the column here: the claim
    # under test is "rotation invalidates", and a test that rotates by hand would pass even if
    # the route that users reach failed to.
    user, old_token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    assert (await _fetch(client, old_token)).status_code == 200

    async with await _build_authed_client(session, user) as owner:
        rotated = await owner.post("/me/settings/ical-token/rotate")
    assert rotated.status_code == 200
    new_token = rotated.json()["ical_token"]
    assert new_token != old_token

    assert (await _fetch(client, old_token)).status_code == 404
    assert (await _fetch(client, new_token)).status_code == 200


async def test_an_unentitled_owner_is_404_and_a_later_grant_restores_the_same_url(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    """D-40: revocation preserves `ical_token`, so a lapsed subscriber's calendar app stops
    receiving events and resumes on a new grant without them re-subscribing."""
    user, token = await subscriber(entitled_until=datetime(2020, 1, 1, tzinfo=UTC))
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    lapsed = await _fetch(client, token)
    assert lapsed.status_code == 404

    # The token itself survived the lapse — nothing rotated it, which is what makes the URL
    # already on the subscriber's phone the one that starts working again.
    assert (await session.get(UserSettings, user.id)).ical_token == token  # type: ignore[union-attr]

    user.entitled_until = ENTITLED_UNTIL
    await session.commit()

    restored = await _fetch(client, token)
    assert restored.status_code == 200
    assert _summaries(restored.text) == ["A Film — in theaters"]


async def test_an_unentitled_owner_is_indistinguishable_from_an_unknown_token(
    client, subscriber, make_film, add_release_date, watchlist
):
    # Byte-for-byte, not just both-404: a difference in body or headers is an oracle for
    # "this token names a real account" (D-39).
    user, token = await subscriber(entitled_until=None)
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    real = await _fetch(client, token)
    bogus = await _fetch(client, "not-a-real-token")

    assert (real.status_code, real.text) == (bogus.status_code, bogus.text)


async def test_a_user_with_no_settings_row_has_no_feed(client, make_user):
    # Nothing to rotate and nothing to serve: the row is created lazily by `/me/settings`.
    await make_user(email="rowless@example.com", entitled_until=ENTITLED_UNTIL)
    assert (await _fetch(client, tokens.new_ical_token())).status_code == 404


# --- the response --------------------------------------------------------------------------


async def test_the_response_is_a_private_hour_cached_calendar(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    r = await _fetch(client, token)

    assert r.status_code == 200
    assert r.headers["content-type"] == "text/calendar; charset=utf-8"
    assert r.headers["cache-control"] == "private, max-age=3600"


async def test_an_empty_watchlist_is_an_empty_calendar_not_a_404(client, subscriber):
    _, token = await subscriber()

    r = await _fetch(client, token)

    assert r.status_code == 200
    assert _events(r.text) == []


async def test_an_event_carries_the_date_the_title_and_the_films_url(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)

    assert prop_dt(vevent, "dtstart") == date(2026, 12, 5)
    assert prop_text(vevent, "summary") == "A Film — in theaters"
    assert prop_text(vevent, "uid") == f"{film.id}-wide@backlotter"
    base = get_settings().public_base_url.rstrip("/")
    assert prop_text(vevent, "description") == f"{base}/film/{ref(film)}"


# --- which films ---------------------------------------------------------------------------


async def test_only_this_users_watchlist_reaches_their_feed(
    client, subscriber, make_film, add_release_date, watchlist
):
    mine, token = await subscriber(email="mine@example.com")
    theirs, _ = await subscriber(email="theirs@example.com")
    my_film = await make_film(slug="mine", title="My Film")
    their_film = await make_film(slug="theirs", title="Their Film")
    await add_release_date(film=my_film, release_date=_FUTURE, release_type=3)
    await add_release_date(film=their_film, release_date=_FUTURE, release_type=3)
    await watchlist(user=mine, film=my_film)
    await watchlist(user=theirs, film=their_film)

    assert _summaries((await _fetch(client, token)).text) == ["My Film — in theaters"]


async def test_a_film_reached_through_a_director_follow_is_in_the_feed(
    client, session, subscriber, make_film, add_release_date, attach_credits
):
    # M8 (D-42): the feed is drawn from the computed watchlist, so following a director puts
    # their in-window films in it. This is the assertion that used to say the opposite.
    user, token = await subscriber()
    film = await make_film(slug="followed", title="Followed Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await attach_credits(film, crew=[{"id": 900, "name": "A Director", "job": "Director"}])
    session.add(
        Follow(
            user_id=user.id,
            entity_type="person",
            entity_id="900",
            source="manual",
            coverage="lead",
        )
    )
    await session.commit()

    assert _summaries((await _fetch(client, token)).text) == ["Followed Film — in theaters"]


async def test_a_muted_film_leaves_the_feed(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    # D-45: the `.ics` feed and `/me/calendar` read one set, so a mute empties both together.
    user, token = await subscriber()
    film = await make_film(slug="muted", title="Muted Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()

    assert _events((await _fetch(client, token)).text) == []


async def test_a_film_with_no_slug_is_skipped(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    unslugged = await make_film(slug=None, title="No Page Film")
    await add_release_date(film=unslugged, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=unslugged)

    assert _events((await _fetch(client, token)).text) == []


async def test_the_calendars_popularity_and_adult_cuts_do_not_apply(
    client, subscriber, make_film, add_release_date, watchlist
):
    # `/calendar` hides these to keep a public listing clean; a film the user watchlisted is
    # not noise to them.
    user, token = await subscriber()
    obscure = await make_film(slug="obscure", title="Obscure Film", popularity=0.1, runtime=40)
    await add_release_date(film=obscure, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=obscure)

    assert _summaries((await _fetch(client, token)).text) == ["Obscure Film — in theaters"]


async def test_a_recently_passed_date_stays_in_the_feed(
    client, subscriber, make_film, add_release_date, watchlist
):
    # Unlike `/calendar`. A subscribed client mirrors whatever the feed publishes, so dropping
    # a date once it passes would delete the release from the user's calendar the next day.
    user, token = await subscriber()
    yesterday = datetime.now(UTC) - timedelta(days=1)
    film = await make_film(slug="released", title="Released Film")
    await add_release_date(film=film, release_date=yesterday, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstart") == yesterday.date()


async def test_a_date_older_than_the_past_window_is_dropped(
    client, subscriber, make_film, add_release_date, watchlist
):
    # The cut that keeps the document bounded by something other than the size of an imported
    # watchlist (`ICAL_PAST_WINDOW_DAYS`).
    user, token = await subscriber()
    long_gone = datetime.now(UTC) - timedelta(days=ICAL_PAST_WINDOW_DAYS + 1)
    film = await make_film(slug="ancient", title="Ancient Film")
    await add_release_date(film=film, release_date=long_gone, release_type=3)
    await watchlist(user=user, film=film)

    assert _events((await _fetch(client, token)).text) == []


async def test_the_edge_of_the_past_window_is_still_published(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    edge = datetime.now(UTC) - timedelta(days=ICAL_PAST_WINDOW_DAYS)
    film = await make_film(slug="edge", title="Edge Film")
    await add_release_date(film=film, release_date=edge, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstart") == edge.date()


async def test_the_window_does_not_cut_the_future(
    client, subscriber, make_film, add_release_date, watchlist
):
    # Everything ahead is published in full — the cut is backwards only.
    user, token = await subscriber()
    far = datetime.now(UTC) + timedelta(days=ICAL_PAST_WINDOW_DAYS * 5)
    film = await make_film(slug="far", title="Far Film")
    await add_release_date(film=film, release_date=far, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstart") == far.date()


# --- which dates ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("release_type", "expected"),
    [
        (2, "A Film — in theaters (limited)"),
        (3, "A Film — in theaters"),
        (4, "A Film — digital"),
        (5, "A Film — physical"),
    ],
)
async def test_every_displayable_bucket_becomes_an_event(
    client, subscriber, make_film, add_release_date, watchlist, release_type, expected
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=release_type)
    await watchlist(user=user, film=film)

    assert _summaries((await _fetch(client, token)).text) == [expected]


@pytest.mark.parametrize("release_type", [1, 6])
async def test_premiere_and_tv_dates_are_not_events(
    client, subscriber, make_film, add_release_date, watchlist, release_type
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=release_type)
    await watchlist(user=user, film=film)

    assert _events((await _fetch(client, token)).text) == []


async def test_a_non_us_date_is_not_an_event(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film", origin_country=["FR"])
    await add_release_date(film=film, release_date=_FUTURE, release_type=3, iso_3166_1="FR")
    await watchlist(user=user, film=film)

    assert _events((await _fetch(client, token)).text) == []


async def test_the_governing_date_is_the_earliest_in_the_subject(
    client, subscriber, make_film, add_release_date, watchlist
):
    # Two US wide rows (TMDB carries per-city dates): one subject, one event, the earlier date.
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_LATER, release_type=3)
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstart") == date(2026, 12, 5)


async def test_two_buckets_on_one_film_are_two_events_with_distinct_uids(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await add_release_date(film=film, release_date=_LATER, release_type=4)
    await watchlist(user=user, film=film)

    feed = _events((await _fetch(client, token)).text)

    assert [prop_text(v, "uid") for v in feed] == [
        f"{film.id}-wide@backlotter",
        f"{film.id}-digital@backlotter",
    ]
    assert [prop_dt(v, "dtstart") for v in feed] == [date(2026, 12, 5), date(2027, 2, 9)]


async def test_events_are_ordered_by_date_then_by_the_calendars_bucket_rank(
    client, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    wide_and_limited = await make_film(slug="both", title="Both Film")
    await add_release_date(film=wide_and_limited, release_date=_FUTURE, release_type=2)
    await add_release_date(film=wide_and_limited, release_date=_FUTURE, release_type=3)
    later = await make_film(slug="later", title="Later Film")
    await add_release_date(film=later, release_date=_LATER, release_type=3)
    await watchlist(user=user, film=wide_and_limited)
    await watchlist(user=user, film=later)

    assert _summaries((await _fetch(client, token)).text) == [
        "Both Film — in theaters",  # wide leads its own date
        "Both Film — in theaters (limited)",
        "Later Film — in theaters",
    ]


# --- DTSTAMP -------------------------------------------------------------------------------


async def test_dtstamp_is_when_the_subjects_date_last_moved(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_LATER, release_type=3)
    await watchlist(user=user, film=film)
    moved_at = datetime(2026, 9, 18, 14, 30, tzinfo=UTC)
    session.add(
        FilmReleaseDateChange(
            film_id=film.id,
            iso_3166_1="US",
            release_type=3,
            previous_date=date(2026, 12, 5),
            new_date=date(2027, 2, 9),
            change="moved",
            changed_at=moved_at,
        )
    )
    await session.commit()

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstamp") == moved_at


async def test_dtstamp_ignores_a_change_to_another_subject(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    # A digital date moving must not restamp the theatrical event: the client would show every
    # date on the film as freshly changed.
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)
    session.add(
        FilmReleaseDateChange(
            film_id=film.id,
            iso_3166_1="US",
            release_type=4,
            previous_date=None,
            new_date=date(2027, 2, 9),
            change="set",
            changed_at=datetime(2026, 9, 18, 14, 30, tzinfo=UTC),
        )
    )
    await session.commit()

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstamp") != datetime(2026, 9, 18, 14, 30, tzinfo=UTC)


async def test_dtstamp_is_stable_across_two_fetches_of_an_unchanged_date(
    client, subscriber, make_film, add_release_date, watchlist
):
    # The property that makes a subscribed feed cheap for the client: nothing changed, so
    # nothing says it did. A `now()` DTSTAMP would fail this.
    user, token = await subscriber()
    film = await make_film(slug="a-film", title="A Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    first = prop_dt(_events((await _fetch(client, token)).text)[0], "dtstamp")
    second = prop_dt(_events((await _fetch(client, token)).text)[0], "dtstamp")

    assert first == second


async def test_dtstamp_falls_back_to_the_slate_observation_when_nothing_moved(
    client, session, subscriber, make_film, add_release_date, watchlist
):
    user, token = await subscriber()
    observed = datetime.now(UTC) - timedelta(days=30)
    film = await make_film(slug="a-film", title="A Film")
    film.release_dates_observed_at = observed
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await watchlist(user=user, film=film)

    (vevent,) = _events((await _fetch(client, token)).text)
    assert prop_dt(vevent, "dtstamp") == observed.replace(microsecond=0)


async def test_the_feed_does_not_create_a_settings_row(client, session):
    # Reading a calendar is not the lazy-creation path `/me/settings` owns: an unknown token
    # must not mint anything.
    await _fetch(client, tokens.new_ical_token())

    assert (await session.execute(select(UserSettings))).scalars().all() == []
