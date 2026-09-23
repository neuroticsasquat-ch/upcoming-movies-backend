"""`GET /people/{ref}` — the person page (NEU-1418, D-1416.6).

Two lists and a tier badge, and between them exactly what following this person would reach:
the in-play films, then the ones still inside the alert window but past. The back catalogue is
absent by design, so the assertions that matter most here are the ones about what is *not*
returned.
"""

from datetime import UTC, date, datetime, timedelta

from tests.fixtures.catalog import add_credit

TODAY = datetime.now(tz=UTC).date()
MAX_AGE_DAYS = 365
"""`PROVIDER_POLL_MAX_AGE_DAYS`' default, which the `recent` list's window rides on."""


async def test_the_page_answers_its_canonical_ref(client, make_person):
    """The ref resolves on the leading id and the response carries the canonical spelling —
    the client redirects when the two differ, exactly as the film page does."""
    await make_person(id=525, name="Christopher Nolan", known_for_department="Directing")

    for ref in ("525", "525-anything-at-all", "525-christopher-nolan"):
        r = await client.get(f"/people/{ref}")
        assert r.status_code == 200, f"ref={ref!r} → {r.status_code}"
        assert r.json()["ref"] == "525-christopher-nolan"


async def test_the_header_carries_what_tmdb_holds(client, session, make_person):
    person = await make_person(id=525, name="Christopher Nolan", known_for_department="Directing")
    person.birthday = date(1970, 7, 30)
    person.deathday = None
    await session.commit()

    body = (await client.get("/people/525")).json()
    assert body["id"] == 525
    assert body["name"] == "Christopher Nolan"
    assert body["profile_path"] == "/p.jpg"
    assert body["known_for_department"] == "Directing"
    assert body["birthday"] == "1970-07-30"
    assert body["deathday"] is None


async def test_an_unknown_person_is_404(client):
    assert (await client.get("/people/999")).status_code == 404


async def test_a_ref_that_does_not_lead_with_an_id_is_404(client, make_person):
    await make_person(id=525, name="Christopher Nolan")

    assert (await client.get("/people/christopher-nolan")).status_code == 404


async def test_a_tombstoned_person_is_404(client, make_person):
    """TMDB has deleted them, so nothing will ever be ingested against them again and a follow
    would be dead the day it was made — the same rule `/people/search` applies."""
    await make_person(
        id=525, name="Gone From TMDB", tmdb_missing_at=datetime(2026, 1, 1, tzinfo=UTC)
    )

    assert (await client.get("/people/525")).status_code == 404


async def test_person_search_still_routes(client, make_person):
    """`/people/search` and `/people/popular` are literal paths registered before `{ref}`, and
    a page route that swallowed them would take the follow affordance down with it."""
    await make_person(id=525, name="Christopher Nolan")

    assert (await client.get("/people/search", params={"q": "nolan"})).status_code == 200
    assert (await client.get("/people/popular")).status_code == 200


# ── the two lists ─────────────────────────────────────────────────────────────


async def test_films_split_into_upcoming_and_recent_and_never_both(
    client, session, make_person, make_film
):
    """`upcoming` is in play, `recent` is the alert window less in play. A film is in one or
    the other, and a film past the window is in neither — that set is exactly what a follow
    can reach (D-46)."""
    await make_person(id=525, name="Christopher Nolan")
    upcoming = await make_film(
        slug="upcoming", title="Upcoming", release_date=TODAY + timedelta(days=30)
    )
    recent = await make_film(slug="recent", title="Recent", release_date=TODAY - timedelta(days=30))
    old = await make_film(
        slug="old", title="Old", release_date=TODAY - timedelta(days=MAX_AGE_DAYS + 1)
    )
    for film in (upcoming, recent, old):
        await add_credit(
            session, film, 525, credit_type="crew", job="Director", department="Directing"
        )
    await session.commit()

    body = (await client.get("/people/525")).json()
    assert [i["film"]["title"] for i in body["upcoming"]] == ["Upcoming"]
    assert [i["film"]["title"] for i in body["recent"]] == ["Recent"]


async def test_a_writer_director_is_one_row_with_two_credits(
    client, session, make_person, make_film
):
    """The page reads "Director · Writer" off one row, so the credits are listed rather than
    folded. The order survives the tier rank EF-1 deleted: seed grade first, then billing,
    then job, which ties these two and breaks it alphabetically the right way round."""
    await make_person(id=525, name="Christopher Nolan")
    film = await make_film(slug="both", title="Both", release_date=None)
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await add_credit(session, film, 525, credit_type="crew", job="Screenplay", department="Writing")
    await session.commit()

    (row,) = (await client.get("/people/525")).json()["upcoming"]
    assert [c["job"] for c in row["credits"]] == ["Director", "Screenplay"]
    assert "tier" not in row
    assert all("tier" not in c for c in row["credits"])


async def test_every_credit_is_listed_without_a_tier(client, session, make_person, make_film):
    """EF-1 and EF-2: a follow reaches every credit, so there is no cut for a badge to name and
    no row the page has to qualify. The 12th-billed film is the one that used to be reachable
    only at `any`, and it is listed on the same terms as the lead role."""
    await make_person(id=525, name="An Actor")
    lead = await make_film(slug="lead", title="Lead", release_date=None)
    supporting = await make_film(slug="supporting", title="Supporting", release_date=None)
    minor = await make_film(slug="minor", title="Minor", release_date=None)
    await add_credit(session, lead, 525, credit_type="cast", credit_order=0)
    await add_credit(session, supporting, 525, credit_type="cast", credit_order=3)
    await add_credit(session, minor, 525, credit_type="cast", credit_order=11)
    await session.commit()

    rows = (await client.get("/people/525")).json()["upcoming"]
    assert {r["film"]["title"] for r in rows} == {"Lead", "Supporting", "Minor"}
    assert all("tier" not in r for r in rows)


async def test_a_films_credits_read_director_then_seed_grade_then_the_rest(
    client, session, make_person, make_film
):
    """`_ordered_credits`'s rule now that the tier rank is gone: director, then the rest of
    seed grade, then everything else, with billing ordering each band.

    The 2nd-billed cast credit is the case that fixes the rule rather than inheriting it — the
    old tier rank folded it in with the director under `lead` and let `credit_order` put it
    first, so a director who acted in their own film was billed above their own directing
    credit while one billed 4th was not. The gaffer proves the third band is still below an
    unbilled seed role."""
    await make_person(id=525, name="A Busy Person")
    film = await make_film(slug="busy", title="Busy", release_date=None)
    await add_credit(session, film, 525, credit_type="crew", job="Gaffer", department="Lighting")
    await add_credit(session, film, 525, credit_type="cast", credit_order=2)
    await add_credit(session, film, 525, credit_type="crew", job="Screenplay", department="Writing")
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    (row,) = (await client.get("/people/525")).json()["upcoming"]
    assert [(c["credit_type"], c["job"], c["credit_order"]) for c in row["credits"]] == [
        ("crew", "Director", None),
        ("cast", None, 2),
        ("crew", "Screenplay", None),
        ("crew", "Gaffer", None),
    ]


async def test_a_row_cites_the_date_the_film_page_shows(
    client, session, make_person, make_film, add_release_date
):
    """`headline_release`, not `catalog.film.release_date` — the film row's rule
    (NEU-1397), inherited by sharing its shape rather than restated."""
    await make_person(id=525, name="Christopher Nolan")
    film = await make_film(slug="dated", title="Dated", release_date=TODAY + timedelta(days=30))
    await add_release_date(
        film=film,
        release_date=datetime(TODAY.year + 1, 5, 1, tzinfo=UTC),
        release_type=3,
    )
    await add_credit(session, film, 525, credit_type="crew", job="Director", department="Directing")
    await session.commit()

    (row,) = (await client.get("/people/525")).json()["upcoming"]
    assert row["film"]["headline_release"]["date"] == f"{TODAY.year + 1}-05-01"
    assert row["film"]["ref"] == f"{film.tmdb_id}-dated"


async def test_a_person_with_nothing_in_reach_gets_two_empty_lists(client, make_person):
    """An empty page is a real answer — the client renders "No upcoming films in the
    catalog" — and is not the same as a 404."""
    await make_person(id=525, name="Nobody Yet")

    body = (await client.get("/people/525")).json()
    assert body["upcoming"] == []
    assert body["recent"] == []
