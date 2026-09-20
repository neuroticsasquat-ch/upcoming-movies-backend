"""`/me/watchlist` (D-42, D-45): the computed watchlist, and the two verbs — want and stop —
that change what covers a film, behind the entitlement gate (D-39)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_credit, add_film
from upmovies.app.models import Follow, WatchlistDismissal
from upmovies.catalog.models import FilmReleaseDate, Person

# The service asks the clock for "today", so the fixture's release date has to move with it — a
# literal would quietly stop being upcoming one day and take the assertions with it.
UPCOMING = datetime.now(tz=UTC).date() + timedelta(days=30)


def _at(day: date) -> datetime:
    return datetime.combine(day, datetime.min.time(), tzinfo=UTC)


@pytest.fixture
async def film(session):
    """Fight Club, carrying a displayable US wide date **and** a different primary date.

    The two disagree on purpose: the payload must lead with the displayable one, so a row that
    went back to echoing `film.release_date` (the pre-NEU-1397 behaviour) fails here rather
    than passing on a date the film page never shows."""
    f = await add_film(
        session,
        tmdb_id=550,
        title="Fight Club",
        slug="fight-club",
        origin_country=["US"],
        release_date=date(2027, 3, 5),
    )
    session.add(
        FilmReleaseDate(film_id=f.id, iso_3166_1="US", release_type=3, release_date=_at(UPCOMING))
    )
    await session.commit()
    return f


HEADLINE = {
    "date": UPCOMING.isoformat(),
    "kind": "upcoming",
    "country": "US",
    "bucket": "wide",
}


async def _mutes(session) -> list[WatchlistDismissal]:
    return list((await session.execute(select(WatchlistDismissal))).scalars().all())


async def _follows(session) -> list[Follow]:
    return list((await session.execute(select(Follow))).scalars().all())


async def _direct_follow(session, film) -> Follow | None:
    """The user's own title follow on this film, if any — what `stop` deletes."""
    return await session.scalar(
        select(Follow).where(Follow.entity_type == "title", Follow.entity_id == str(film.id))
    )


@pytest.fixture
async def directed_film(session, film):
    """A *second* film, covered only through a person follow of its director.

    The indirect half of the merged model: the user never named this film, and it is on their
    watchlist because they follow the person who is making it (D-42)."""
    other = await add_film(session, tmdb_id=551, title="The Game", slug="the-game")
    session.add(Person(id=7467, name="David Fincher"))
    await session.flush()
    await add_credit(
        session, other, 7467, credit_type="crew", job="Director", department="Directing"
    )
    await add_credit(
        session, film, 7467, credit_type="crew", job="Director", department="Directing"
    )
    await session.commit()
    return other


# --- the gate ------------------------------------------------------------------------------


async def test_list_requires_auth(client):
    r = await client.get("/me/watchlist")
    assert r.status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
async def test_every_verb_is_403_for_an_unentitled_user(authed_client, film, method):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in
    # (D-37). Each verb is named so that a refactor that drops the gate from one of them fails
    # here rather than shipping.
    path = "/me/watchlist" if method in ("GET", "POST") else f"/me/watchlist/{film.id}"
    body = {"film_id": str(film.id)} if method == "POST" else None
    r = await authed_client.request(method, path, json=body)
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


async def test_there_is_no_patch_route(entitled_client, film):
    """The per-item alert preferences went with the table; the stores are one setting per user
    (`/me/settings`, D-44)."""
    r = await entitled_client.patch(f"/me/watchlist/{film.id}", json={"alert_prefs": ["buy"]})
    assert r.status_code == 405


# --- want ----------------------------------------------------------------------------------


async def test_wanting_a_film_follows_the_title_and_lists_it(entitled_client, session, film):
    r = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    assert r.status_code == 200
    body = r.json()
    assert body["film"] == {
        "id": str(film.id),
        "tmdb_id": 550,
        "slug": "fight-club",
        "title": "Fight Club",
        "poster_path": None,
        "headline_release": HEADLINE,
    }
    assert body["covered_by"] == [
        {"entity_type": "title", "entity_id": str(film.id), "name": "Fight Club"}
    ]
    assert (body["followed"], body["muted"]) == (True, False)

    follows = await _follows(session)
    assert [(f.entity_type, f.entity_id, f.source) for f in follows] == [
        ("title", str(film.id), "manual")
    ]

    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
    assert [i["film"]["id"] for i in r.json()["items"]] == [str(film.id)]


async def test_wanting_a_film_something_already_covers_writes_no_second_follow(
    entitled_client, session, directed_film
):
    """The film is already on the list through its director. Wanting it says nothing new, so
    it creates nothing — following the title *beside* the person would mean stopping it once
    did not stop it."""
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(directed_film.id)})
    assert r.status_code == 200
    assert r.json()["followed"] is False
    assert r.json()["covered_by"] == [
        {"entity_type": "person", "entity_id": "7467", "name": "David Fincher"}
    ]
    assert [f.entity_type for f in await _follows(session)] == ["person"]


async def test_wanting_twice_is_idempotent(entitled_client, session, film):
    first = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    again = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    assert (first.status_code, again.status_code) == (200, 200)
    assert again.json() == first.json()
    assert len(await _follows(session)) == 1


async def test_wanting_a_muted_film_un_mutes_it(entitled_client, session, film):
    user = entitled_client.user  # type: ignore[attr-defined]
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    assert r.status_code == 200
    assert r.json()["muted"] is False
    assert await _mutes(session) == []


async def test_wanting_a_released_film_still_yields_an_item(entitled_client, session):
    """A title follow covers its film in any state — that is what makes an old film the user
    asked for reachable at all, and what the importers rely on (D-1414.9)."""
    old = await add_film(
        session,
        tmdb_id=552,
        title="Se7en",
        slug="se7en",
        status="Released",
        release_date=date(1995, 9, 22),
    )
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(old.id)})
    assert r.status_code == 200
    assert r.json()["followed"] is True


async def test_wanting_a_film_the_catalog_does_not_hold_is_404(entitled_client):
    r = await entitled_client.post(
        "/me/watchlist", json={"film_id": "00000000-0000-0000-0000-000000000001"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "film_not_found"


@pytest.mark.parametrize("payload", [{"film_id": "not-a-uuid"}, {}])
async def test_a_malformed_want_is_422(entitled_client, payload):
    r = await entitled_client.post("/me/watchlist", json=payload)
    assert r.status_code == 422


# --- the list ------------------------------------------------------------------------------


async def test_a_muted_film_is_listed_and_marked_rather_than_dropped(
    entitled_client, session, film, directed_film
):
    """The list is where a mute is undone, so a muted film has to be visible on it."""
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})
    user = entitled_client.user  # type: ignore[attr-defined]
    session.add(WatchlistDismissal(user_id=user.id, film_id=directed_film.id))
    await session.commit()

    items = (await entitled_client.get("/me/watchlist")).json()["items"]
    assert {i["film"]["id"]: i["muted"] for i in items} == {
        str(film.id): False,
        str(directed_film.id): True,
    }


async def test_covered_by_leads_with_the_direct_title_follow(
    entitled_client, session, film, directed_film
):
    """ "Why is this here?" is answered by the user's own follow whenever they have one, and by
    the oldest standing interest otherwise (D-1414.5).

    Wanting the film *first* is what produces both covers: wanting one the person follow
    already reaches writes nothing, so this is the only order that yields a film with a direct
    follow and an indirect one."""
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})

    items = {
        i["film"]["id"]: i for i in (await entitled_client.get("/me/watchlist")).json()["items"]
    }
    assert [c["entity_type"] for c in items[str(film.id)]["covered_by"]] == ["title", "person"]
    assert items[str(film.id)]["followed"] is True
    assert [c["entity_type"] for c in items[str(directed_film.id)]["covered_by"]] == ["person"]
    assert items[str(directed_film.id)]["followed"] is False


async def test_the_item_is_dated_from_the_earliest_covering_follow(
    entitled_client, session, film, directed_film
):
    """A film reached by a director followed long ago has been on the way since then, not since
    whichever row the union happened to emit first."""
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})
    follow = await session.scalar(select(Follow).where(Follow.entity_type == "person"))
    assert follow is not None
    follow.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    await session.commit()
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    items = {
        i["film"]["id"]: i for i in (await entitled_client.get("/me/watchlist")).json()["items"]
    }
    assert items[str(film.id)]["created_at"].startswith("2020-01-01")


async def test_a_user_sees_only_their_own_watchlist(entitled_client, make_user, session, film):
    other = await make_user(email="other@example.com")
    session.add(
        Follow(user_id=other.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    r = await entitled_client.get("/me/watchlist")
    assert r.json()["items"] == []


# --- the headline release (NEU-1397) -------------------------------------------------------


async def test_list_carries_the_headline_release(entitled_client, film):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
    assert r.json()["items"][0]["film"]["headline_release"] == HEADLINE


async def test_a_film_with_no_date_at_all_is_listed_with_a_null_headline_release(
    entitled_client, session
):
    # No displayable row and no primary date. The row is still on the user's watchlist: it is
    # returned with a null date for the frontend's "No date yet", never dropped.
    undated = await add_film(session, tmdb_id=553, title="Untitled", slug="untitled")
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(undated.id)})
    assert r.status_code == 200
    assert r.json()["film"]["headline_release"] is None

    r = await entitled_client.get("/me/watchlist")
    assert [i["film"]["headline_release"] for i in r.json()["items"]] == [None]


async def test_a_film_with_nothing_displayable_falls_back_to_the_primary_date(
    entitled_client, session
):
    # A non-origin-country date is not displayable, so the primary answers — marked `primary`
    # so the frontend can render it as unconfirmed rather than as a date this site lists.
    f = await add_film(
        session,
        tmdb_id=554,
        title="Cliffhanger",
        slug="cliffhanger",
        origin_country=["US"],
        release_date=date(2029, 4, 2),
    )
    session.add(
        FilmReleaseDate(
            film_id=f.id, iso_3166_1="DE", release_type=3, release_date=_at(date(2029, 4, 2))
        )
    )
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(f.id)})
    assert r.status_code == 200
    assert r.json()["film"]["headline_release"] == {
        "date": "2029-04-02",
        "kind": "primary",
        "country": None,
        "bucket": None,
    }


# --- stop ----------------------------------------------------------------------------------


async def test_stopping_a_directly_followed_film_deletes_the_follow_and_answers_204(
    entitled_client, session, film
):
    """Nothing else covers it, so it leaves the list outright — and no mute is written, because
    a permanent record of a film the user merely removed is what D-40 keeps out of an
    unfollow."""
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 204
    assert r.content == b""

    assert (await entitled_client.get("/me/watchlist")).json()["items"] == []
    assert await _follows(session) == []
    assert await _mutes(session) == []


async def test_stopping_a_film_something_else_covers_mutes_it_and_answers_the_item(
    entitled_client, session, film, directed_film
):
    """Both halves are needed: deleting the title follow alone would leave the film on the list
    through the director who also reaches it."""
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 200
    body = r.json()
    assert (body["muted"], body["followed"]) == (True, False)
    assert [c["entity_type"] for c in body["covered_by"]] == ["person"]

    assert await _direct_follow(session, film) is None
    assert [(m.user_id, m.film_id) for m in await _mutes(session)] == [
        (entitled_client.user.id, film.id)  # type: ignore[attr-defined]
    ]


async def test_stopping_an_indirectly_covered_film_mutes_it(
    entitled_client, session, directed_film
):
    """No title follow to delete — the mute is the whole of the answer, and the person follow
    it was reached through is left alone (D-40)."""
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})

    r = await entitled_client.delete(f"/me/watchlist/{directed_film.id}")
    assert r.status_code == 200
    assert r.json()["muted"] is True
    assert [f.entity_type for f in await _follows(session)] == ["person"]


async def test_stopping_an_already_muted_film_answers_the_muted_item(
    entitled_client, session, directed_film
):
    await entitled_client.post("/me/follows", json={"entity_type": "person", "entity_id": "7467"})
    await entitled_client.delete(f"/me/watchlist/{directed_film.id}")
    first_at = (await _mutes(session))[0].created_at

    r = await entitled_client.delete(f"/me/watchlist/{directed_film.id}")
    assert r.status_code == 200
    assert r.json()["muted"] is True

    session.expire_all()
    mutes = await _mutes(session)
    assert len(mutes) == 1
    assert mutes[0].created_at == first_at


async def test_stopping_a_film_nothing_covers_is_404(entitled_client, film):
    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "watchlist_item_not_found"


async def test_stopping_a_film_the_catalog_does_not_hold_is_404_film_not_found(entitled_client):
    """A different answer from the one above, deliberately: "that is not a film" and "that film
    was never on your list" are different things to be told."""
    r = await entitled_client.delete("/me/watchlist/00000000-0000-0000-0000-000000000001")
    assert r.status_code == 404
    assert r.json()["detail"] == "film_not_found"


async def test_stopping_leaves_another_users_follow_alone(
    entitled_client, make_user, session, film
):
    other = await make_user(email="other@example.com")
    session.add(
        Follow(user_id=other.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 404
    assert [f.user_id for f in await _follows(session)] == [other.id]


# --- CSRF ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "DELETE"])
async def test_writes_require_the_csrf_header(entitled_client, film, method):
    del entitled_client.headers["X-CSRF-Token"]
    path = "/me/watchlist" if method == "POST" else f"/me/watchlist/{film.id}"
    body = {"film_id": str(film.id)} if method == "POST" else None
    r = await entitled_client.request(method, path, json=body)
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_list_does_not_require_csrf_header(entitled_client):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
