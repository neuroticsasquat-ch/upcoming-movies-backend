from datetime import UTC, date, datetime


async def test_index_lists_only_films_with_a_summarized_event(client, make_film, add_event):
    shown = await make_film(slug="shown-2026", title="Shown")
    await add_event(film=shown, event_type="casting", summary="A summary.")
    hidden = await make_film(slug="hidden-2026", title="Hidden")
    await add_event(film=hidden, summary=None)  # event but no summary
    await make_film(slug="bare-2026", title="Bare")  # no events at all

    r = await client.get("/films")
    assert r.status_code == 200
    body = r.json()
    assert [item["slug"] for item in body["items"]] == ["shown-2026"]
    assert body["total"] == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    item = body["items"][0]
    assert item["title"] == "Shown"
    assert item["release_year"] == 2026
    assert item["poster_path"] == "/poster.jpg"
    assert item["arc_stage"] == "cast"  # Planned baseline + casting event


async def test_index_orders_by_release_date_desc_nulls_last(client, make_film, add_event):
    later = await make_film(slug="later-2027", release_date=date(2027, 1, 1))
    await add_event(film=later, summary="s")
    earlier = await make_film(slug="earlier-2026", release_date=date(2026, 1, 1))
    await add_event(film=earlier, summary="s")
    undated = await make_film(slug="undated", release_date=None)
    await add_event(film=undated, summary="s")

    r = await client.get("/films")
    assert [i["slug"] for i in r.json()["items"]] == ["later-2027", "earlier-2026", "undated"]


async def test_index_pagination(client, make_film, add_event):
    for i in range(3):
        film = await make_film(slug=f"f{i}-2026", release_date=date(2026, 1, 1 + i))
        await add_event(film=film, summary="s")

    page1 = (await client.get("/films", params={"limit": 2, "offset": 0})).json()
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2
    assert page1["offset"] == 0

    page2 = (await client.get("/films", params={"limit": 2, "offset": 2})).json()
    assert len(page2["items"]) == 1
    assert page2["total"] == 3
    assert page2["offset"] == 2


async def test_index_rejects_out_of_range_pagination(client):
    assert (await client.get("/films", params={"limit": 0})).status_code == 422
    assert (await client.get("/films", params={"limit": 101})).status_code == 422
    assert (await client.get("/films", params={"offset": -1})).status_code == 422


async def test_detail_returns_chronological_summarized_events_with_sources(
    client, make_film, add_event
):
    film = await make_film(slug="odyssey-2026", title="The Odyssey", status="In Production")
    await add_event(
        film=film,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime(2025, 3, 1, tzinfo=UTC),
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
    await add_event(
        film=film,
        event_type="trailer",
        occurred_at=datetime(2025, 1, 1, tzinfo=UTC),  # earlier
        summary="Trailer dropped.",
    )

    r = await client.get("/films/odyssey-2026")
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "odyssey-2026"
    assert body["title"] == "The Odyssey"
    assert body["release_date"] == "2026-07-17"
    assert body["release_year"] == 2026
    assert body["arc_stage"] == "trailer"  # In Production baseline, trailer event wins
    # Chronological ascending: the January trailer before the March casting.
    assert [e["event_type"] for e in body["events"]] == ["trailer", "casting"]
    casting = body["events"][1]
    assert casting["confidence"] == "confirmed"
    assert casting["summary"] == "Casting announced."
    assert casting["sources"][0]["url"] == "https://deadline.com/a"
    assert casting["sources"][0]["source"] == "Deadline"


async def test_detail_omits_summary_less_events(client, make_film, add_event):
    film = await make_film(slug="partial-2026")
    await add_event(film=film, event_type="casting", summary="Has summary.")
    await add_event(film=film, event_type="trailer", summary=None)

    r = await client.get("/films/partial-2026")
    assert r.status_code == 200
    assert [e["event_type"] for e in r.json()["events"]] == ["casting"]


async def test_detail_empty_log_film_returns_200_with_derived_arc(client, make_film, add_event):
    film = await make_film(slug="quiet-2026", status="In Production")
    await add_event(film=film, event_type="production_start", summary=None)  # no summary

    r = await client.get("/films/quiet-2026")
    assert r.status_code == 200
    body = r.json()
    assert body["events"] == []
    assert body["arc_stage"] == "shooting"  # In Production / production_start


async def test_detail_unknown_slug_returns_404(client):
    assert (await client.get("/films/does-not-exist")).status_code == 404


async def test_film_detail_source_label_uses_resolved_outlet(client, make_film, add_event):
    film = await make_film(slug="resolve-film-2026")
    await add_event(
        film=film,
        summary="Casting.",
        sources=(
            {
                "url": "https://news.google.com/rss/articles/xyz",
                "source": "Google News: per-film",
                "title": "Director Set - Variety",
                "outlet": "Variety",
            },
        ),
    )

    body = (await client.get(f"/films/{film.slug}")).json()
    assert body["events"][0]["sources"][0]["source"] == "Variety"
