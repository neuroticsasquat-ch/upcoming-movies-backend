"""The admin JSON over `ingest.credit_hold` (D-8, §4): the gate, what a hold renders as, and
the manual release that is the only way a correct-by-the-rule hold on a real beat gets out."""

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from tests.fixtures.catalog import add_film
from upmovies.catalog.models import Person
from upmovies.ingest.models import CreditHold

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
# As Pydantic renders it: UTC serializes with a `Z`, not a `+00:00` offset.
NOW_JSON = NOW.isoformat().replace("+00:00", "Z")


async def _person(session: AsyncSession, person_id: int = 100, **overrides) -> Person:
    person = Person(id=person_id, name=overrides.pop("name", "Long Departed"), **overrides)
    session.add(person)
    await session.flush()
    return person


async def _hold(session: AsyncSession, film, person, **overrides) -> CreditHold:
    fields: dict = {
        "film_id": film.id,
        "person_id": person.id,
        "credit_type": "director",
        "changed_at": NOW,
        "reason": "deceased",
        "held_at": NOW,
    }
    fields.update(overrides)
    hold = CreditHold(**fields)
    session.add(hold)
    await session.flush()
    await session.commit()
    return hold


# --- the gate ------------------------------------------------------------------------------


async def test_requires_auth(client):
    r = await client.get("/admin/credit-holds")
    assert r.status_code == 401


async def test_forbidden_for_non_admin(authed_client):
    r = await authed_client.get("/admin/credit-holds")
    assert r.status_code == 403


async def test_release_requires_csrf(admin_authed_client, session: AsyncSession):
    film = await add_film(session, 550)
    hold = await _hold(session, film, await _person(session))

    r = await admin_authed_client.post(
        f"/admin/credit-holds/{hold.id}/release", headers={"X-CSRF-Token": "wrong"}
    )
    assert r.status_code == 403


# --- what a hold renders as ----------------------------------------------------------------


async def test_lists_open_holds_with_the_film_person_and_reason(
    admin_authed_client, session: AsyncSession
):
    """Everything needed to judge the hold without leaving the response: the two dates are here
    because two of the three reasons are decided on them."""
    film = await add_film(session, 550, title="Fight Club")
    person = await _person(session, name="Long Departed", deathday=date(2011, 3, 4))
    hold = await _hold(session, film, person)

    r = await admin_authed_client.get("/admin/credit-holds?open=true")

    assert r.status_code == 200
    assert r.json() == [
        {
            "id": str(hold.id),
            "film": {"id": str(film.id), "tmdb_id": 550, "title": "Fight Club"},
            "person": {
                "id": 100,
                "name": "Long Departed",
                "birthday": None,
                "deathday": "2011-03-04",
            },
            "credit_type": "director",
            "reason": "deceased",
            "changed_at": NOW_JSON,
            "held_at": NOW_JSON,
            "released_at": None,
            "release_reason": None,
        }
    ]


async def test_open_is_the_default(admin_authed_client, session: AsyncSession):
    film = await add_film(session, 550)
    await _hold(session, film, await _person(session))

    assert len((await admin_authed_client.get("/admin/credit-holds")).json()) == 1


async def test_released_holds_are_their_own_listing(admin_authed_client, session: AsyncSession):
    """`open=false` answers "did my release take" from the same URL that showed the hold."""
    film = await add_film(session, 550)
    person = await _person(session)
    await _hold(session, film, person)
    await _hold(
        session,
        film,
        person,
        changed_at=NOW - timedelta(days=1),
        released_at=NOW,
        release_reason="expired",
    )

    open_holds = (await admin_authed_client.get("/admin/credit-holds?open=true")).json()
    released = (await admin_authed_client.get("/admin/credit-holds?open=false")).json()

    assert [h["release_reason"] for h in open_holds] == [None]
    assert [h["release_reason"] for h in released] == ["expired"]


async def test_newest_first(admin_authed_client, session: AsyncSession):
    film = await add_film(session, 550)
    person = await _person(session)
    await _hold(session, film, person, changed_at=NOW - timedelta(days=2), held_at=NOW)
    await _hold(
        session, film, person, changed_at=NOW - timedelta(days=1), held_at=NOW + timedelta(hours=1)
    )

    body = (await admin_authed_client.get("/admin/credit-holds")).json()

    assert [h["changed_at"] for h in body] == [
        (NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        (NOW - timedelta(days=2)).isoformat().replace("+00:00", "Z"),
    ]


# --- the release ---------------------------------------------------------------------------


async def test_release_closes_the_hold_as_manual(admin_authed_client, session: AsyncSession):
    """`manual` rather than `cleared`, because an override has not established that the
    condition lifted — and because that is the value the sweep reads to know it must not
    re-hold the change."""
    film = await add_film(session, 550)
    hold = await _hold(session, film, await _person(session))

    r = await admin_authed_client.post(f"/admin/credit-holds/{hold.id}/release")

    assert r.status_code == 200
    assert r.json()["release_reason"] == "manual"
    await session.refresh(hold)
    assert hold.released_at is not None
    assert hold.release_reason == "manual"


async def test_release_of_an_unknown_hold_is_404(admin_authed_client):
    r = await admin_authed_client.post(
        "/admin/credit-holds/00000000-0000-0000-0000-000000000000/release"
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "hold_not_found"


async def test_release_of_a_closed_hold_is_409(admin_authed_client, session: AsyncSession):
    """Not an idempotent no-op: re-stamping an `expired` row as `manual` would claim an admin
    let through a beat that no longer exists to be let through."""
    film = await add_film(session, 550)
    hold = await _hold(
        session, film, await _person(session), released_at=NOW, release_reason="expired"
    )

    r = await admin_authed_client.post(f"/admin/credit-holds/{hold.id}/release")

    assert r.status_code == 409
    assert r.json()["detail"] == "hold_already_released"
