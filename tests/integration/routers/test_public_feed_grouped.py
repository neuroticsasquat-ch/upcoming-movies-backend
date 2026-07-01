from datetime import UTC, datetime


async def test_grouped_one_row_per_film_day_with_count_and_top_type(client, make_film, add_event):
    film = await make_film(slug="film-2026", title="A Film")
    await add_event(
        film=film,
        event_type="announced",
        summary="a",
        created_at=datetime(2026, 6, 3, 8, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="casting",
        summary="b",
        created_at=datetime(2026, 6, 3, 12, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="c",
        created_at=datetime(2026, 6, 3, 20, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["total"] == 1
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["film_slug"] == "film-2026"
    assert item["film_title"] == "A Film"
    assert item["release_year"] == 2026
    assert item["poster_path"] == "/poster.jpg"
    assert item["day"] == "2026-06-03"
    assert item["event_count"] == 3
    assert item["top_event_type"] == "trailer"


async def test_grouped_same_film_two_days_makes_two_rows(client, make_film, add_event):
    film = await make_film(slug="film-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="day1",
        created_at=datetime(2026, 6, 1, 10, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="day2",
        created_at=datetime(2026, 6, 2, 10, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["total"] == 2
    assert [(i["day"], i["top_event_type"]) for i in body["items"]] == [
        ("2026-06-02", "trailer"),
        ("2026-06-01", "casting"),
    ]


async def test_grouped_newest_day_first_across_films(client, make_film, add_event):
    a = await make_film(slug="a-2026")
    b = await make_film(slug="b-2026")
    await add_event(film=a, summary="A", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    await add_event(film=b, summary="B", created_at=datetime(2026, 6, 2, tzinfo=UTC))

    body = (await client.get("/feed/grouped")).json()
    assert [i["film_slug"] for i in body["items"]] == ["b-2026", "a-2026"]


async def test_grouped_within_day_orders_by_popularity(client, make_film, add_event):
    # Same UTC day. Popularity must win over BOTH slug order and event time:
    # zzz-blockbuster (pop 90, earlier event) precedes aaa-popular (pop 5, later event),
    # even though "aaa" < "zzz" and aaa's event is more recent.
    low = await make_film(slug="aaa-popular", popularity=5.0)
    high = await make_film(slug="zzz-blockbuster", popularity=90.0)
    await add_event(film=low, summary="low", created_at=datetime(2026, 6, 5, 22, tzinfo=UTC))
    await add_event(film=high, summary="high", created_at=datetime(2026, 6, 5, 8, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_slug"] for i in items] == ["zzz-blockbuster", "aaa-popular"]


async def test_grouped_within_day_null_popularity_sorts_last(client, make_film, add_event):
    # A film with no popularity sorts after one that has popularity, regardless of slug:
    # aaa-nopop (None) has the alphabetically-first slug but must come last.
    nopop = await make_film(slug="aaa-nopop", popularity=None)
    haspop = await make_film(slug="zzz-haspop", popularity=10.0)
    await add_event(film=nopop, summary="nopop", created_at=datetime(2026, 6, 5, tzinfo=UTC))
    await add_event(film=haspop, summary="haspop", created_at=datetime(2026, 6, 5, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_slug"] for i in items] == ["zzz-haspop", "aaa-nopop"]


async def test_grouped_within_day_equal_popularity_ties_break_by_slug(client, make_film, add_event):
    # Equal popularity falls back to slug ascending (NOT event time):
    # aaa-2026 wins on slug even though bbb-2026 has the later event.
    a = await make_film(slug="aaa-2026", popularity=10.0)
    b = await make_film(slug="bbb-2026", popularity=10.0)
    await add_event(film=a, summary="a", created_at=datetime(2026, 6, 5, 8, tzinfo=UTC))
    await add_event(film=b, summary="b", created_at=datetime(2026, 6, 5, 22, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_slug"] for i in items] == ["aaa-2026", "bbb-2026"]


async def test_grouped_only_counts_summarized_events(client, make_film, add_event):
    film = await make_film(slug="partial-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="has",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary=None,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["event_count"] == 1
    assert item["top_event_type"] == "casting"  # the summary-less trailer is ignored


async def test_grouped_film_with_no_summarized_events_absent(client, make_film, add_event):
    film = await make_film(slug="nosum-2026")
    await add_event(
        film=film,
        event_type="trailer",
        summary=None,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_grouped_excludes_films_without_slug(client, make_film, add_event):
    slugged = await make_film(slug="has-slug-2026")
    unslugged = await make_film(slug=None)
    await add_event(film=slugged, summary="shown", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    await add_event(film=unslugged, summary="hidden", created_at=datetime(2026, 6, 1, tzinfo=UTC))

    body = (await client.get("/feed/grouped")).json()
    assert [i["film_slug"] for i in body["items"]] == ["has-slug-2026"]
    assert body["total"] == 1


async def test_grouped_utc_day_boundary_splits_groups(client, make_film, add_event):
    film = await make_film(slug="boundary-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="before midnight",
        created_at=datetime(2026, 6, 1, 23, 59, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="after midnight",
        created_at=datetime(2026, 6, 2, 0, 1, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["total"] == 2
    assert [i["day"] for i in body["items"]] == ["2026-06-02", "2026-06-01"]


async def test_grouped_pagination(client, make_film, add_event):
    film = await make_film(slug="film-2026")
    for i in range(3):
        await add_event(
            film=film,
            event_type="casting",
            summary=f"s{i}",
            created_at=datetime(2026, 6, 1 + i, 10, tzinfo=UTC),
        )

    page1 = (await client.get("/feed/grouped", params={"limit": 2, "offset": 0})).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2
    assert page1["offset"] == 0
    assert [i["day"] for i in page1["items"]] == ["2026-06-03", "2026-06-02"]

    page2 = (await client.get("/feed/grouped", params={"limit": 2, "offset": 2})).json()
    assert page2["total"] == 3
    assert len(page2["items"]) == 1
    assert page2["items"][0]["day"] == "2026-06-01"


async def test_grouped_paginates_by_day_not_film_rows(client, make_film, add_event):
    """limit/offset count distinct days: day A (2 films) + day B (1 film) → total=2 days;
    a 1-day page returns *all* of that day's films, not a single film row."""
    a1 = await make_film(slug="a1-2026", popularity=90.0)
    a2 = await make_film(slug="a2-2026", popularity=10.0)
    b1 = await make_film(slug="b1-2026")
    for f in (a1, a2):
        await add_event(film=f, summary="s", created_at=datetime(2026, 6, 2, 10, tzinfo=UTC))
    await add_event(film=b1, summary="s", created_at=datetime(2026, 6, 1, 10, tzinfo=UTC))

    page1 = (await client.get("/feed/grouped", params={"limit": 1, "offset": 0})).json()
    assert page1["total"] == 2  # two distinct days, not three film rows
    assert [i["day"] for i in page1["items"]] == ["2026-06-02", "2026-06-02"]
    assert [i["film_slug"] for i in page1["items"]] == ["a1-2026", "a2-2026"]  # popularity order

    page2 = (await client.get("/feed/grouped", params={"limit": 1, "offset": 1})).json()
    assert page2["total"] == 2
    assert [i["film_slug"] for i in page2["items"]] == ["b1-2026"]


async def test_grouped_rejects_out_of_range_pagination(client):
    assert (await client.get("/feed/grouped", params={"limit": 0})).status_code == 422
    assert (await client.get("/feed/grouped", params={"limit": 101})).status_code == 422
    assert (await client.get("/feed/grouped", params={"offset": -1})).status_code == 422


async def test_grouped_empty_returns_empty_list(client):
    body = (await client.get("/feed/grouped")).json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_grouped_other_only_day_is_hidden(client, make_film, add_event):
    film = await make_film(slug="other-2026")
    await add_event(
        film=film,
        event_type="other",
        summary="misc",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_grouped_first_look_is_visible_with_its_top_type(client, make_film, add_event):
    # NEU-447: first_look is NOT hidden (unlike "other") and is the top type for a day
    # where it is the only beat.
    film = await make_film(slug="dynamic-duo-2028", title="Dynamic Duo")
    await add_event(
        film=film,
        event_type="first_look",
        summary="first footage screened at the event",
        created_at=datetime(2026, 6, 3, 12, tzinfo=UTC),
    )

    body = (await client.get("/feed/grouped")).json()
    assert body["total"] == 1
    assert body["items"][0]["top_event_type"] == "first_look"
