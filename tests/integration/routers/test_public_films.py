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
    """Events must be ordered by created_at ascending, expose a created_at key, omit occurred_at.

    The fixture sets occurred_at in the *opposite* order from created_at so that any
    stale sort on occurred_at would produce the wrong order — proving the switch took effect.

    - casting: created_at=Jan (earlier), occurred_at=Mar (later)
    - trailer:  created_at=Mar (later),  occurred_at=Jan (earlier)

    Expected order by created_at asc: casting first, then trailer.
    A sort by occurred_at asc would yield the opposite: trailer first.
    """
    film = await make_film(slug="odyssey-2026", title="The Odyssey", status="In Production")
    casting_created_at = datetime(2025, 1, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="casting",
        confidence="confirmed",
        occurred_at=datetime(2025, 3, 1, tzinfo=UTC),  # later occurred_at (opposite of created_at)
        created_at=casting_created_at,  # earlier created_at → should appear first
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
    trailer_created_at = datetime(2025, 3, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="trailer",
        occurred_at=datetime(
            2025, 1, 1, tzinfo=UTC
        ),  # earlier occurred_at (opposite of created_at)
        created_at=trailer_created_at,  # later created_at → should appear second
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
    # created_at ascending: casting (Jan created_at) before trailer (Mar created_at).
    # A stale occurred_at sort would yield the reverse order and fail this assertion.
    assert [e["event_type"] for e in body["events"]] == ["casting", "trailer"]
    # Each event must expose created_at and must NOT expose occurred_at (the rename).
    for event in body["events"]:
        assert "created_at" in event, "event must expose created_at"
        assert "occurred_at" not in event, "event must not expose occurred_at after rename"
    casting = body["events"][0]
    assert casting["created_at"] == casting_created_at.isoformat().replace("+00:00", "Z")
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


async def test_film_detail_caps_sources_at_three_distinct_outlets_newest_first(
    client, make_film, add_event
):
    film = await make_film(slug="cap-detail-2026")
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

    sources = (await client.get("/films/cap-detail-2026")).json()["events"][0]["sources"]
    assert [s["source"] for s in sources] == ["Deadline", "Variety", "The Hollywood Reporter"]


async def test_detail_excludes_other_events(client, make_film, add_event):
    film = await make_film(slug="mixed-detail-2026")
    await add_event(film=film, event_type="casting", summary="Casting.")
    await add_event(film=film, event_type="other", summary="Misc.")

    body = (await client.get("/films/mixed-detail-2026")).json()
    assert [e["event_type"] for e in body["events"]] == ["casting"]


async def test_index_excludes_film_with_only_other_events(client, make_film, add_event):
    shown = await make_film(slug="real-2026")
    await add_event(film=shown, event_type="casting", summary="s")
    other_only = await make_film(slug="otheronly-2026")
    await add_event(film=other_only, event_type="other", summary="s")

    slugs = [i["slug"] for i in (await client.get("/films")).json()["items"]]
    assert slugs == ["real-2026"]


# ── release_dates projection tests ───────────────────────────────────────────


async def test_detail_exposes_us_release_dates(client, make_film, add_event, add_release_date):
    film = await make_film(slug="rd-us-2026", title="US Dates Film")
    await add_event(film=film, summary="Event.")
    await add_release_date(
        film=film,
        iso_3166_1="US",
        release_type=3,
        release_date=datetime(2026, 7, 17, tzinfo=UTC),
        certification="PG-13",
    )
    await add_release_date(
        film=film,
        iso_3166_1="US",
        release_type=1,
        release_date=datetime(2026, 6, 1, tzinfo=UTC),
        certification=None,
    )

    r = await client.get("/films/rd-us-2026")
    assert r.status_code == 200
    body = r.json()
    rds = body["release_dates"]
    assert len(rds) == 2
    # ordered by release_date asc: June before July
    assert rds[0]["date"].startswith("2026-06-01")
    assert rds[0]["release_type"] == 1
    assert rds[0]["type_label"] == "Premiere"
    assert rds[0]["country"] == "US"
    assert rds[0]["certification"] is None
    assert rds[1]["date"].startswith("2026-07-17")
    assert rds[1]["release_type"] == 3
    assert rds[1]["type_label"] == "Theatrical"
    assert rds[1]["certification"] == "PG-13"


async def test_detail_excludes_non_home_region_dates(
    client, make_film, add_event, add_release_date
):
    film = await make_film(slug="rd-excl-2026", title="Exclusion Film")
    await add_event(film=film, summary="Event.")
    await add_release_date(
        film=film,
        iso_3166_1="FR",
        release_type=3,
        release_date=datetime(2026, 7, 17, tzinfo=UTC),
    )

    r = await client.get("/films/rd-excl-2026")
    assert r.status_code == 200
    assert r.json()["release_dates"] == []


async def test_detail_release_dates_empty_when_none(client, make_film, add_event):
    film = await make_film(slug="rd-none-2026", title="No Dates Film")
    await add_event(film=film, summary="Event.")

    r = await client.get("/films/rd-none-2026")
    assert r.status_code == 200
    assert r.json()["release_dates"] == []


async def test_detail_includes_origin_country_dates(
    client, make_film, add_event, add_release_date, session
):
    film = await make_film(slug="rd-kr-2026", title="Korean Film")
    film.origin_country = ["KR"]
    session.add(film)
    await session.commit()
    await session.refresh(film)

    await add_event(film=film, summary="Event.")
    await add_release_date(
        film=film,
        iso_3166_1="US",
        release_type=3,
        release_date=datetime(2026, 7, 17, tzinfo=UTC),
    )
    await add_release_date(
        film=film,
        iso_3166_1="KR",
        release_type=3,
        release_date=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await add_release_date(
        film=film,
        iso_3166_1="JP",
        release_type=3,
        release_date=datetime(2026, 8, 1, tzinfo=UTC),
    )

    r = await client.get("/films/rd-kr-2026")
    assert r.status_code == 200
    rds = r.json()["release_dates"]
    countries = [rd["country"] for rd in rds]
    assert "US" in countries
    assert "KR" in countries
    assert "JP" not in countries
    assert len(rds) == 2
