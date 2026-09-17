"""`/me/watchlist` (D-13, D-14): the watchlist's CRUD and the dismissal a removed derived item
leaves behind, behind the entitlement gate (D-39)."""

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from upmovies.app.models import WatchlistDismissal, WatchlistItem
from upmovies.catalog.models import FilmReleaseDate

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


async def _dismissals(session) -> list[WatchlistDismissal]:
    return list((await session.execute(select(WatchlistDismissal))).scalars().all())


# --- the gate ------------------------------------------------------------------------------


async def test_list_requires_auth(client):
    r = await client.get("/me/watchlist")
    assert r.status_code == 401


@pytest.mark.parametrize("method", ["GET", "POST", "PATCH", "DELETE"])
async def test_every_verb_is_403_for_an_unentitled_user(authed_client, film, method):
    # `authed_client`'s user has `entitled_until` NULL — the state every signup starts in
    # (D-37). Each verb is named so that a refactor that drops the gate from one of them fails
    # here rather than shipping.
    path = "/me/watchlist" if method in ("GET", "POST") else f"/me/watchlist/{film.id}"
    body = {
        "POST": {"film_id": str(film.id)},
        "PATCH": {"alert_prefs": ["buy"]},
    }.get(method)
    r = await authed_client.request(method, path, json=body)
    assert r.status_code == 403
    assert r.json()["detail"] == "entitlement_required"


# --- adding --------------------------------------------------------------------------------


async def test_add_a_film_and_list_it_with_the_default_prefs(entitled_client, film):
    r = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    assert r.status_code == 201
    body = r.json()
    assert body["film"] == {
        "id": str(film.id),
        "tmdb_id": 550,
        "slug": "fight-club",
        "title": "Fight Club",
        "poster_path": None,
        "headline_release": HEADLINE,
    }
    assert body["source"] == "manual"
    assert body["alert_prefs"] == ["stream"]

    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
    assert [i["film"]["id"] for i in r.json()["items"]] == [str(film.id)]


async def test_add_with_explicit_prefs_stores_them_in_canonical_order(entitled_client, film):
    r = await entitled_client.post(
        "/me/watchlist",
        json={"film_id": str(film.id), "alert_prefs": ["stream", "buy", "stream"]},
    )
    assert r.status_code == 201
    assert r.json()["alert_prefs"] == ["buy", "stream"]


async def test_add_with_an_empty_prefs_list_means_no_availability_alerts(entitled_client, film):
    # An explicit `[]` is not the same as omitting the field: omitted means the default.
    r = await entitled_client.post(
        "/me/watchlist", json={"film_id": str(film.id), "alert_prefs": []}
    )
    assert r.status_code == 201
    assert r.json()["alert_prefs"] == []


@pytest.mark.parametrize(
    "payload",
    [
        {"film_id": "not-a-uuid"},
        {"film_id": "00000000-0000-0000-0000-000000000001", "alert_prefs": ["cinema"]},
        {"film_id": "00000000-0000-0000-0000-000000000001", "alert_prefs": "stream"},
        {},
    ],
)
async def test_a_malformed_add_is_422(entitled_client, payload):
    r = await entitled_client.post("/me/watchlist", json=payload)
    assert r.status_code == 422


async def test_adding_a_film_the_catalog_does_not_hold_is_404(entitled_client):
    r = await entitled_client.post(
        "/me/watchlist", json={"film_id": "00000000-0000-0000-0000-000000000001"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "film_not_found"


async def test_adding_twice_returns_the_existing_row_with_its_prefs_untouched(
    entitled_client, film
):
    first = await entitled_client.post(
        "/me/watchlist", json={"film_id": str(film.id), "alert_prefs": ["buy"]}
    )
    again = await entitled_client.post(
        "/me/watchlist", json={"film_id": str(film.id), "alert_prefs": ["rent"]}
    )
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json() == first.json()
    assert again.json()["alert_prefs"] == ["buy"]

    r = await entitled_client.get("/me/watchlist")
    assert len(r.json()["items"]) == 1


async def test_a_user_sees_only_their_own_watchlist(entitled_client, make_user, session, film):
    other = await make_user(email="other@example.com")
    session.add(WatchlistItem(user_id=other.id, film_id=film.id, source="manual"))
    await session.commit()

    r = await entitled_client.get("/me/watchlist")
    assert r.json()["items"] == []


# --- the headline release (NEU-1397) -------------------------------------------------------


async def test_list_carries_the_headline_release(entitled_client, film):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
    assert r.json()["items"][0]["film"]["headline_release"] == HEADLINE


async def test_patch_carries_the_headline_release(entitled_client, film):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.patch(f"/me/watchlist/{film.id}", json={"alert_prefs": ["buy"]})
    assert r.status_code == 200
    assert r.json()["film"]["headline_release"] == HEADLINE


async def test_a_film_with_no_date_at_all_is_listed_with_a_null_headline_release(
    entitled_client, session
):
    # No displayable row and no primary date. The row is still the user's watchlist row: it is
    # returned with a null date for the frontend's "No date yet", never dropped.
    undated = await add_film(session, tmdb_id=551, title="Untitled", slug="untitled")
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(undated.id)})
    assert r.status_code == 201
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
        tmdb_id=552,
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
    assert r.status_code == 201
    assert r.json()["film"]["headline_release"] == {
        "date": "2029-04-02",
        "kind": "primary",
        "country": None,
        "bucket": None,
    }


# --- alert prefs ---------------------------------------------------------------------------


async def test_patch_replaces_the_prefs(entitled_client, film):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.patch(
        f"/me/watchlist/{film.id}", json={"alert_prefs": ["rent", "buy", "rent"]}
    )
    assert r.status_code == 200
    assert r.json()["alert_prefs"] == ["buy", "rent"]
    assert r.json()["film"]["id"] == str(film.id)

    r = await entitled_client.get("/me/watchlist")
    assert r.json()["items"][0]["alert_prefs"] == ["buy", "rent"]


async def test_patch_of_a_film_not_on_the_watchlist_is_404(entitled_client, film):
    r = await entitled_client.patch(f"/me/watchlist/{film.id}", json={"alert_prefs": ["buy"]})
    assert r.status_code == 404
    assert r.json()["detail"] == "watchlist_item_not_found"


@pytest.mark.parametrize("payload", [{"alert_prefs": ["cinema"]}, {"alert_prefs": None}, {}])
async def test_a_malformed_patch_is_422(entitled_client, film, payload):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    r = await entitled_client.patch(f"/me/watchlist/{film.id}", json=payload)
    assert r.status_code == 422


# --- removing, and the dismissal --------------------------------------------------------------


async def test_removing_a_manual_item_deletes_it_and_leaves_no_dismissal(
    entitled_client, session, film
):
    await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 204

    r = await entitled_client.get("/me/watchlist")
    assert r.json()["items"] == []
    assert await _dismissals(session) == []


async def test_removing_a_derived_item_writes_a_dismissal(entitled_client, session, film):
    # The follow graph put it there (D-13); the user taking it off is a refusal to remember.
    user = entitled_client.user  # type: ignore[attr-defined]
    session.add(WatchlistItem(user_id=user.id, film_id=film.id, source="derived_from_follow"))
    await session.commit()

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 204

    r = await entitled_client.get("/me/watchlist")
    assert r.json()["items"] == []
    assert [(d.user_id, d.film_id) for d in await _dismissals(session)] == [(user.id, film.id)]


async def test_a_second_dismissal_of_the_same_film_keeps_the_first(entitled_client, session, film):
    # Dismissed once, derived again by a pass that did not yet honour it, dismissed again: one
    # row, dated from the first refusal.
    user = entitled_client.user  # type: ignore[attr-defined]
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    session.add(WatchlistItem(user_id=user.id, film_id=film.id, source="derived_from_follow"))
    await session.commit()
    first_at = (await _dismissals(session))[0].created_at

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 204

    session.expire_all()
    dismissals = await _dismissals(session)
    assert len(dismissals) == 1
    assert dismissals[0].created_at == first_at


async def test_a_dismissal_does_not_block_a_manual_add(entitled_client, session, film):
    # The dismissal binds the derivation, not the user: adding the film by hand overrules it,
    # and the dismissal stays so the derivation still cannot put it back after a later removal.
    user = entitled_client.user  # type: ignore[attr-defined]
    session.add(WatchlistDismissal(user_id=user.id, film_id=film.id))
    await session.commit()

    r = await entitled_client.post("/me/watchlist", json={"film_id": str(film.id)})
    assert r.status_code == 201
    assert r.json()["source"] == "manual"
    assert len(await _dismissals(session)) == 1


async def test_removing_a_film_not_on_the_watchlist_is_404(entitled_client, film):
    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 404
    assert r.json()["detail"] == "watchlist_item_not_found"


async def test_removing_leaves_another_users_item_alone(entitled_client, make_user, session, film):
    other = await make_user(email="other@example.com")
    session.add(WatchlistItem(user_id=other.id, film_id=film.id, source="manual"))
    await session.commit()

    r = await entitled_client.delete(f"/me/watchlist/{film.id}")
    assert r.status_code == 404
    remaining = (await session.execute(select(WatchlistItem))).scalars().all()
    assert [i.user_id for i in remaining] == [other.id]


# --- CSRF ----------------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE"])
async def test_writes_require_the_csrf_header(entitled_client, film, method):
    del entitled_client.headers["X-CSRF-Token"]
    path = "/me/watchlist" if method == "POST" else f"/me/watchlist/{film.id}"
    body = {"POST": {"film_id": str(film.id)}, "PATCH": {"alert_prefs": ["buy"]}}.get(method)
    r = await entitled_client.request(method, path, json=body)
    assert r.status_code == 403
    assert r.json()["detail"] == "csrf_invalid"


async def test_list_does_not_require_csrf_header(entitled_client):
    del entitled_client.headers["X-CSRF-Token"]
    r = await entitled_client.get("/me/watchlist")
    assert r.status_code == 200
