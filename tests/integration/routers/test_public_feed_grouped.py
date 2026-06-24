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
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["film_slug"] == "film-2026"
    assert item["film_title"] == "A Film"
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


async def test_grouped_within_day_orders_by_latest_event_time(client, make_film, add_event):
    # Same UTC day. latest-event-time must win over slug ordering:
    # zzz-late (22:00) precedes aaa-early (08:00) even though "aaa" < "zzz".
    early = await make_film(slug="aaa-early")
    late = await make_film(slug="zzz-late")
    await add_event(film=early, summary="early", created_at=datetime(2026, 6, 5, 8, tzinfo=UTC))
    await add_event(film=late, summary="late", created_at=datetime(2026, 6, 5, 22, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_slug"] for i in items] == ["zzz-late", "aaa-early"]


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
