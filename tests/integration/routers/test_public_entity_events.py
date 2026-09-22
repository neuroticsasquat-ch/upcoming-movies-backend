"""`GET /people/{ref}/events`, `/companies/{ref}/events`, `/collections/{ref}/events` — the
card list beside the follow button on every entity page (EF-18, NEU-1440).

The contract is "this is what following would deliver", so the assertions that matter most are
the ones about what is *absent*: the film's other beats, a hidden type, somebody else's
attachment. The three types run through one parametrized body wherever the rule is shared,
because sameness is the contract — an entity page whose franchise half quietly listed more than
its studio half would be three pages, not one shape.
"""

from datetime import UTC, datetime, timedelta

import pytest

from upmovies.news.subject_key import normalize_name

PERSON_ID = 525
PERSON_NAME = "Céline  Sciamma"
"""Deliberately not already normalized — an accent and a double space — so the name match goes
through `sql_normalized_name` rather than past it, as it does in production."""
COMPANY_ID = 711
COLLECTION_ID = 10

NOW = datetime(2026, 9, 18, 9, tzinfo=UTC)


@pytest.fixture
def make_entity(make_person, make_company, make_collection, session):
    """Mint one entity of `kind` and return `(url_prefix, attach, card)`.

    `attach` makes a film the entity's — a credit, a production-company row, a collection id —
    which is what the `canceled` branch reads. `card` writes that entity's own attach or detach
    card on a film, with the `subject_key` its carding path writes.
    """

    async def _make(kind: str):
        if kind == "person":
            await make_person(id=PERSON_ID, name=PERSON_NAME)

            async def attach(film):
                from tests.fixtures.catalog import add_credit

                await add_credit(session, film, PERSON_ID, credit_type="cast", credit_order=0)
                await session.commit()

            return "people", PERSON_ID, attach, ("casting", [normalize_name(PERSON_NAME)])

        if kind == "company":
            await make_company(id=COMPANY_ID, name="A Studio")

            async def attach(film):
                from upmovies.catalog.models import FilmProductionCompany

                session.add(FilmProductionCompany(film_id=film.id, company_id=COMPANY_ID))
                await session.commit()

            return "companies", COMPANY_ID, attach, ("company_attached", [f"company:{COMPANY_ID}"])

        await make_collection(id=COLLECTION_ID, name="A Saga")

        async def attach(film):
            film.collection_id = COLLECTION_ID
            await session.commit()

        return (
            "collections",
            COLLECTION_ID,
            attach,
            ("collection_attached", [f"collection:{COLLECTION_ID}"]),
        )

    return _make


KINDS = ("person", "company", "collection")


@pytest.mark.parametrize("kind", KINDS)
async def test_the_entitys_own_card_is_listed(client, make_entity, make_film, add_event, kind):
    """The headline case, and the one every type shares: the attachment card naming this entity
    is the page's list."""
    prefix, entity_id, _attach, (event_type, subject_key) = await make_entity(kind)
    film = await make_film(slug="portrait", title="Portrait")
    card = await add_event(
        film=film, event_type=event_type, subject_key=subject_key, created_at=NOW
    )

    r = await client.get(f"/{prefix}/{entity_id}/events")

    assert r.status_code == 200
    body = r.json()
    assert [item["event_id"] for item in body["items"]] == [str(card.id)]
    assert body["next_cursor"] is None
    assert body["items"][0]["summary"] == "A neutral summary."


@pytest.mark.parametrize("kind", KINDS)
async def test_the_films_other_beats_are_not_listed(
    client, make_entity, make_film, add_event, kind
):
    """EF-3, from the page: an entity follow reaches the attachment and nothing else about the
    film. The trailer is on the same film and is deliberately given the same `subject_key`, so
    what excludes it is the event type and not a missing token."""
    prefix, entity_id, _attach, (event_type, subject_key) = await make_entity(kind)
    film = await make_film(slug="portrait", title="Portrait")
    card = await add_event(film=film, event_type=event_type, subject_key=subject_key)
    for other in ("trailer", "release_date", "announced"):
        await add_event(film=film, event_type=other, subject_key=subject_key)

    r = await client.get(f"/{prefix}/{entity_id}/events")

    assert [item["event_id"] for item in r.json()["items"]] == [str(card.id)]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_cancellation_of_an_attached_film_is_listed(
    client, make_entity, make_film, add_event, kind
):
    """EF-6, the one beat every follow type carries. The `canceled` card has no `subject_key` at
    all, so its branch reads the film's *current* attachments — which is why `attach` has to run
    for this case and not for the others."""
    prefix, entity_id, attach, _card = await make_entity(kind)
    film = await make_film(slug="shelved", title="Shelved")
    await attach(film)
    card = await add_event(film=film, event_type="canceled", provenance="catalog")

    r = await client.get(f"/{prefix}/{entity_id}/events")

    assert [item["event_id"] for item in r.json()["items"]] == [str(card.id)]


@pytest.mark.parametrize("kind", KINDS)
async def test_another_entitys_card_is_not_listed(client, make_entity, make_film, add_event, kind):
    """The page is one entity's stream. A card of the same type carrying somebody else's token
    is the control that the id, and not the type, is what admits a card."""
    prefix, entity_id, _attach, (event_type, _subject_key) = await make_entity(kind)
    film = await make_film(slug="elsewhere", title="Elsewhere")
    await add_event(
        film=film,
        event_type=event_type,
        subject_key=[normalize_name("Somebody Else"), "company:999999", "collection:999999"],
    )

    r = await client.get(f"/{prefix}/{entity_id}/events")

    assert r.json()["items"] == []


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unknown_ref_is_404(client, make_entity, kind):
    """404 on the same terms as the entity page itself, so a ref cannot resolve on one half of
    a page and not the other."""
    prefix, _entity_id, _attach, _card = await make_entity(kind)

    r = await client.get(f"/{prefix}/999999/events")

    assert r.status_code == 404


async def test_a_tombstoned_person_is_404(client, make_person, make_film, add_event):
    """`/people/{ref}` 404s a person TMDB has deleted, because they are not a follow target
    anywhere — and the cards list has to agree, or the page renders half."""
    await make_person(id=PERSON_ID, name=PERSON_NAME, tmdb_missing_at=NOW)
    film = await make_film(slug="portrait", title="Portrait")
    await add_event(film=film, event_type="casting", subject_key=[normalize_name(PERSON_NAME)])

    assert (await client.get(f"/people/{PERSON_ID}/events")).status_code == 404


# --- visibility (the feed's terms, EF-18) ---------------------------------------------------


async def test_a_hidden_type_is_not_listed(client, make_person, make_film, add_event):
    """`visible_events()`: `other` is the uncategorized catch-all, hidden from every surface.
    Given this entity's own `subject_key` so the type is what excludes it."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    film = await make_film(slug="portrait", title="Portrait")
    token = [normalize_name(PERSON_NAME)]
    card = await add_event(film=film, event_type="casting", subject_key=token)
    await add_event(film=film, event_type="other", subject_key=token)

    r = await client.get(f"/people/{PERSON_ID}/events")

    assert [item["event_id"] for item in r.json()["items"]] == [str(card.id)]


async def test_a_card_on_a_film_with_no_slug_is_not_listed(client, make_person, session, add_event):
    """The slug term: a film with no page has no URL to send the reader to."""
    from tests.fixtures.catalog import add_film

    await make_person(id=PERSON_ID, name=PERSON_NAME)
    film = await add_film(session, tmdb_id=9100, slug=None)
    await session.commit()
    await add_event(film=film, event_type="casting", subject_key=[normalize_name(PERSON_NAME)])

    r = await client.get(f"/people/{PERSON_ID}/events")

    assert r.json()["items"] == []


async def test_a_superseded_card_is_not_listed(client, session, make_person, make_film, add_event):
    """D-2, inherited from `entity_attachment_event_ids`: a superseded attach card is still
    *rendered* wherever it is linked from, but it is not a beat this stream delivers. The
    correction that replaced it is."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    film = await make_film(slug="portrait", title="Portrait")
    token = [normalize_name(PERSON_NAME)]
    withdrawn = await add_event(
        film=film, event_type="casting", subject_key=token, status="superseded"
    )
    correction = await add_event(film=film, event_type="credit_removed", subject_key=token)
    withdrawn.superseded_by = correction.id
    await session.commit()

    r = await client.get(f"/people/{PERSON_ID}/events")

    assert [item["event_id"] for item in r.json()["items"]] == [str(correction.id)]


# --- paging (EF-18: page size 20, newest first) ---------------------------------------------


@pytest.fixture
async def twenty_five_cards(make_person, make_film, add_event):
    """One person with 25 attach cards, each a minute apart, newest last in creation order."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    token = [normalize_name(PERSON_NAME)]
    cards = []
    for i in range(25):
        film = await make_film(slug=f"film-{i}", title=f"Film {i}")
        cards.append(
            await add_event(
                film=film,
                event_type="casting",
                subject_key=token,
                created_at=NOW + timedelta(minutes=i),
            )
        )
    return cards


async def test_the_page_is_twenty_newest_first(client, twenty_five_cards):
    """EF-18's page size, and the feed's axis: `created_at` descending, which is "what has
    happened with them lately" rather than the film page's `occurred_at` order."""
    r = await client.get(f"/people/{PERSON_ID}/events")

    body = r.json()
    assert len(body["items"]) == 20
    expected = [str(card.id) for card in reversed(twenty_five_cards)][:20]
    assert [item["event_id"] for item in body["items"]] == expected
    assert body["next_cursor"] is not None


async def test_the_cursor_returns_the_rest_exactly_once(client, twenty_five_cards):
    """A keyset page cannot drop or repeat a card across the boundary, which is the whole
    reason it is not an offset: this list grows at the top while it is read."""
    first = (await client.get(f"/people/{PERSON_ID}/events")).json()
    second = (await client.get(f"/people/{PERSON_ID}/events?cursor={first['next_cursor']}")).json()

    assert len(second["items"]) == 5
    assert second["next_cursor"] is None
    seen = [item["event_id"] for item in first["items"] + second["items"]]
    assert len(set(seen)) == 25
    assert set(seen) == {str(card.id) for card in twenty_five_cards}


async def test_the_last_page_carries_no_cursor(client, make_person, make_film, add_event):
    """`next_cursor` is null on the last page — how a caller knows it has reached the end,
    since a keyset page reports no total."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    film = await make_film(slug="portrait", title="Portrait")
    await add_event(film=film, event_type="casting", subject_key=[normalize_name(PERSON_NAME)])

    r = await client.get(f"/people/{PERSON_ID}/events?limit=1")

    assert len(r.json()["items"]) == 1
    assert r.json()["next_cursor"] is None


async def test_a_forged_cursor_is_400(client, make_person):
    """A token this API did not mint is the caller's mistake, not a 500. The cursor is opaque
    precisely so a client does not learn to construct one."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)

    r = await client.get(f"/people/{PERSON_ID}/events?cursor=not-a-cursor")

    assert r.status_code == 400
    assert r.json()["detail"] == "invalid_cursor"


# --- routing --------------------------------------------------------------------------------


async def test_entity_events_still_route(client, make_person, make_company, make_collection):
    """The literal `/search` and `/popular` paths sit beside these two-segment ones; a `{ref}`
    cannot swallow a second segment, and this is the assertion that stays true."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    await make_company(id=COMPANY_ID, name="A Studio")
    await make_collection(id=COLLECTION_ID, name="A Saga")

    for path in (
        f"/people/{PERSON_ID}/events",
        f"/companies/{COMPANY_ID}/events",
        f"/collections/{COLLECTION_ID}/events",
    ):
        assert (await client.get(path)).status_code == 200

    assert (await client.get("/people/search?q=sciamma")).status_code == 200
    assert (await client.get("/companies/search?q=studio")).status_code == 200


async def test_the_list_is_public(client, make_person, make_film, add_event):
    """No session, no entitlement: the page renders for an anonymous visitor and the follow
    button is the thing that asks for an account (EF-18)."""
    await make_person(id=PERSON_ID, name=PERSON_NAME)
    film = await make_film(slug="portrait", title="Portrait")
    await add_event(film=film, event_type="casting", subject_key=[normalize_name(PERSON_NAME)])

    r = await client.get(f"/people/{PERSON_ID}/events")

    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
