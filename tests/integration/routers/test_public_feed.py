from datetime import UTC, datetime


async def test_feed_orders_by_created_at_desc(client, make_film, add_event):
    film = await make_film(slug="film-2026", title="A Film")
    await add_event(
        film=film,
        event_type="casting",
        summary="Oldest.",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="Newest.",
        created_at=datetime(2026, 6, 3, tzinfo=UTC),
    )
    await add_event(
        film=film,
        event_type="release_date",
        summary="Middle.",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
    )

    r = await client.get("/feed")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["summary"] for item in body["items"]] == ["Newest.", "Middle.", "Oldest."]


async def test_feed_item_shape_includes_film_and_sources(client, make_film, add_event):
    film = await make_film(slug="odyssey-2026", title="The Odyssey")
    await add_event(
        film=film,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime(2025, 3, 1, tzinfo=UTC),
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        summary="Casting announced.",
        sources=(
            {
                "url": "https://deadline.com/a",
                "source": "Deadline",
                "title": "Cast revealed",
                "published_at": datetime(2025, 3, 1, 12, tzinfo=UTC),
            },
        ),
    )

    item = (await client.get("/feed")).json()["items"][0]
    assert item["film_slug"] == "odyssey-2026"
    assert item["film_title"] == "The Odyssey"
    assert item["event_type"] == "casting"
    assert item["confidence"] == "confirmed"
    assert item["summary"] == "Casting announced."
    assert item["occurred_at"].startswith("2025-03-01")
    assert item["created_at"].startswith("2026-06-01")
    assert item["sources"][0]["url"] == "https://deadline.com/a"
    assert item["sources"][0]["source"] == "Deadline"
    assert item["sources"][0]["title"] == "Cast revealed"


async def test_feed_omits_summary_less_events(client, make_film, add_event):
    film = await make_film(slug="partial-2026")
    await add_event(film=film, event_type="casting", summary="Has summary.")
    await add_event(film=film, event_type="trailer", summary=None)

    body = (await client.get("/feed")).json()
    assert body["total"] == 1
    assert [item["event_type"] for item in body["items"]] == ["casting"]


async def test_feed_spans_multiple_films(client, make_film, add_event):
    a = await make_film(slug="a-2026", title="Film A")
    b = await make_film(slug="b-2026", title="Film B")
    await add_event(film=a, summary="From A.", created_at=datetime(2026, 6, 1, tzinfo=UTC))
    await add_event(film=b, summary="From B.", created_at=datetime(2026, 6, 2, tzinfo=UTC))

    body = (await client.get("/feed")).json()
    assert [item["film_slug"] for item in body["items"]] == ["b-2026", "a-2026"]


async def test_feed_pagination(client, make_film, add_event):
    film = await make_film(slug="film-2026")
    for i in range(3):
        await add_event(
            film=film,
            event_type="casting",
            summary=f"s{i}",
            created_at=datetime(2026, 6, 1 + i, tzinfo=UTC),
        )

    page1 = (await client.get("/feed", params={"limit": 2, "offset": 0})).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2
    assert page1["offset"] == 0

    page2 = (await client.get("/feed", params={"limit": 2, "offset": 2})).json()
    assert page2["total"] == 3
    assert len(page2["items"]) == 1


async def test_feed_rejects_out_of_range_pagination(client):
    assert (await client.get("/feed", params={"limit": 0})).status_code == 422
    assert (await client.get("/feed", params={"limit": 101})).status_code == 422
    assert (await client.get("/feed", params={"offset": -1})).status_code == 422


async def test_feed_empty_returns_empty_list(client):
    body = (await client.get("/feed")).json()
    assert body["items"] == []
    assert body["total"] == 0


async def test_feed_source_label_uses_resolved_outlet(client, make_film, add_event):
    film = await make_film(slug="resolve-feed-2026")
    await add_event(
        film=film,
        summary="Casting.",
        sources=(
            {
                "url": "https://news.google.com/rss/articles/abc",
                "source": "Google News: per-film",
                "title": "Cast Revealed - Deadline",
                "outlet": "Deadline",
            },
            {
                "url": "https://variety.com/trade",
                "source": "Variety",
                "title": "Trade story",
                "outlet": None,
            },
        ),
    )

    item = (await client.get("/feed")).json()["items"][0]
    labels = {s["source"] for s in item["sources"]}
    assert labels == {"Deadline", "Variety"}  # google resolved; trade falls back to source


async def test_feed_caps_sources_at_three_distinct_outlets_newest_first(
    client, make_film, add_event
):
    film = await make_film(slug="cap-feed-2026")
    await add_event(
        film=film,
        summary="Lots of coverage.",
        sources=(
            {
                "url": "https://d/old",
                "source": "Deadline",
                "published_at": datetime(2025, 3, 1, tzinfo=UTC),
            },
            {
                "url": "https://d/new",
                "source": "Deadline",
                "published_at": datetime(2025, 3, 5, tzinfo=UTC),
            },
            {
                "url": "https://v/1",
                "source": "Variety",
                "published_at": datetime(2025, 3, 4, tzinfo=UTC),
            },
            {
                "url": "https://t/1",
                "source": "The Hollywood Reporter",
                "published_at": datetime(2025, 3, 3, tzinfo=UTC),
            },
            {
                "url": "https://c/1",
                "source": "Collider",
                "published_at": datetime(2025, 3, 2, tzinfo=UTC),
            },
        ),
    )

    item = (await client.get("/feed")).json()["items"][0]
    # 5 stories / 4 distinct outlets -> top 3 by recency, deduped, newest-first.
    assert [s["source"] for s in item["sources"]] == [
        "Deadline",
        "Variety",
        "The Hollywood Reporter",
    ]


async def test_feed_excludes_other_events(client, make_film, add_event):
    film = await make_film(slug="mixed-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="shown",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await add_event(
        film=film, event_type="other", summary="hidden", created_at=datetime(2026, 6, 2, tzinfo=UTC)
    )

    body = (await client.get("/feed")).json()
    assert body["total"] == 1
    assert [i["event_type"] for i in body["items"]] == ["casting"]
