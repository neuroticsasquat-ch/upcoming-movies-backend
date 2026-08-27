from datetime import UTC, datetime

from tests.fixtures.public import ref


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
    assert item["film_ref"] == ref(film)
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
    assert [i["film_ref"] for i in body["items"]] == [ref(b), ref(a)]


async def test_grouped_within_day_orders_alphabetically(client, make_film, add_event):
    # Same UTC day. Sorts by title ascending, slug as tiebreaker.
    z = await make_film(slug="zzz-last", popularity=90.0)
    a = await make_film(slug="aaa-first", popularity=5.0)
    await add_event(film=z, summary="z", created_at=datetime(2026, 6, 5, 8, tzinfo=UTC))
    await add_event(film=a, summary="a", created_at=datetime(2026, 6, 5, 22, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(a), ref(z)]


async def test_grouped_within_day_alphabetical_ignores_popularity(client, make_film, add_event):
    # Alphabetical order, regardless of popularity: aaa-first precedes zzz-last even with lower pop.
    nopop = await make_film(slug="aaa-first", popularity=None)
    haspop = await make_film(slug="zzz-last", popularity=10.0)
    await add_event(film=nopop, summary="nopop", created_at=datetime(2026, 6, 5, tzinfo=UTC))
    await add_event(film=haspop, summary="haspop", created_at=datetime(2026, 6, 5, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(nopop), ref(haspop)]


async def test_grouped_within_day_equal_popularity_ties_break_by_slug(client, make_film, add_event):
    # Equal popularity falls back to slug ascending (NOT event time):
    # aaa-2026 wins on slug even though bbb-2026 has the later event.
    a = await make_film(slug="aaa-2026", popularity=10.0)
    b = await make_film(slug="bbb-2026", popularity=10.0)
    await add_event(film=a, summary="a", created_at=datetime(2026, 6, 5, 8, tzinfo=UTC))
    await add_event(film=b, summary="b", created_at=datetime(2026, 6, 5, 22, tzinfo=UTC))

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [i["film_ref"] for i in items] == [ref(a), ref(b)]


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
    assert [i["film_ref"] for i in body["items"]] == [ref(slugged)]
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
    assert [i["film_ref"] for i in page1["items"]] == [ref(a1), ref(a2)]  # alphabetical by title

    page2 = (await client.get("/feed/grouped", params={"limit": 1, "offset": 1})).json()
    assert page2["total"] == 2
    assert [i["film_ref"] for i in page2["items"]] == [ref(b1)]


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


async def test_grouped_carries_arc_stage_derived_from_status(client, make_film, add_event):
    # The feed renders the arc-stage label where an undated film's release year would go
    # (NEU-1085), so the row has to carry the stage — it is not inferable from release_year.
    # Within-day order is alphabetical by title.
    shooting = await make_film(
        slug="shooting-film", status="In Production", release_date=None, popularity=5.0
    )
    undated = await make_film(
        slug="undated-film", status="Planned", release_date=None, popularity=90.0
    )
    await add_event(film=shooting, summary="b", created_at=datetime(2026, 6, 3, tzinfo=UTC))
    await add_event(film=undated, summary="a", created_at=datetime(2026, 6, 3, tzinfo=UTC))

    body = (await client.get("/feed/grouped")).json()
    assert [(i["film_ref"], i["release_year"], i["arc_stage"]) for i in body["items"]] == [
        (ref(shooting), None, "shooting"),
        (ref(undated), None, "announced"),
    ]


async def test_grouped_dated_film_still_carries_its_arc_stage(client, make_film, add_event):
    film = await make_film(slug="film-2026", status="Released")
    await add_event(film=film, summary="a", created_at=datetime(2026, 6, 3, tzinfo=UTC))

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["release_year"] == 2026
    assert item["arc_stage"] == "released"


async def test_grouped_arc_stage_covers_wrapped_and_the_unknown_status_fallback(
    client, make_film, add_event
):
    # `derive_arc_stage` defaults an absent/unmapped status to "announced". That fallback now
    # reaches a third surface, so pin it here rather than inferring it from the other two.
    # Within-day order is alphabetical by title.
    unknown = await make_film(slug="unknown-film", status=None, release_date=None, popularity=5.0)
    wrapped = await make_film(
        slug="wrapped-film", status="Post Production", release_date=None, popularity=90.0
    )
    await add_event(film=unknown, summary="b", created_at=datetime(2026, 6, 3, tzinfo=UTC))
    await add_event(film=wrapped, summary="a", created_at=datetime(2026, 6, 3, tzinfo=UTC))

    body = (await client.get("/feed/grouped")).json()
    assert [(i["film_ref"], i["arc_stage"]) for i in body["items"]] == [
        (ref(unknown), "announced"),
        (ref(wrapped), "wrapped"),
    ]


# NEU-1137 — the news-backed signal. The classifier is EXISTS(event_story) — "did any of this
# film-day's events pick up a story" — and deliberately NOT `Event.provenance`, which records
# where an event was *born* and is never mutated when a story attaches later (NEU-1136). A row
# is one (film, day), so the film is news-backed for that day if *any* of its events that day
# has a story: one row, one section, never a film listed twice under one date heading.


async def test_grouped_day_backed_by_a_story_is_flagged(client, make_film, add_event):
    film = await make_film(slug="reported-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="Variety reported the casting",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        sources=({"url": "https://variety.com/casting"},),
    )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["news_backed"] is True


async def test_grouped_tmdb_only_day_is_not_news_backed(client, make_film, add_event):
    film = await make_film(slug="tmdb-only-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="TMDB added cast",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        provenance="catalog",
    )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["news_backed"] is False


async def test_grouped_story_event_without_sources_is_not_news_backed(client, make_film, add_event):
    # The signal is the linked story, not the provenance column. A `story`-provenance event
    # whose sources were all dropped (e.g. by the source gate) carries no reporting to point
    # at, so it must not claim the news-backed section.
    film = await make_film(slug="sourceless-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="no sources left",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        provenance="story",
    )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["news_backed"] is False


async def test_grouped_promoted_catalog_event_is_news_backed_and_keeps_its_provenance(
    client, make_film, add_event
):
    """The case the whole story rests on: TMDB carded the beat, a trade covered it later and
    the story attached to that same event. It moves section without becoming a second card,
    and `provenance` still reads `catalog` on the flat feed."""
    film = await make_film(slug="promoted-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="TMDB carded it; Variety confirmed it",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        provenance="catalog",
        sources=({"url": "https://variety.com/scoop"},),
    )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["news_backed"] is True

    flat = (await client.get("/feed")).json()["items"][0]
    assert flat["provenance"] == "catalog"
    assert [s["url"] for s in flat["sources"]] == ["https://variety.com/scoop"]


async def test_grouped_one_story_backed_event_flags_the_whole_film_day(
    client, make_film, add_event
):
    """Classified by "any": one Variety story plus four TMDB-only changes → two items:
    news-backed (1 event) and catalog (4 events), each with scoped metrics."""
    film = await make_film(slug="mixed-2026")
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="casting",
        summary="Variety on the casting",
        created_at=day,
        sources=({"url": "https://variety.com/casting"},),
    )
    tmdb_types = ("crew_attached", "release_date", "production_start", "trailer")
    for i, event_type in enumerate(tmdb_types):
        await add_event(
            film=film,
            event_type=event_type,
            summary=f"tmdb {i}",
            created_at=day,
            occurred_at=datetime(2026, 5, 1 + i, tzinfo=UTC),
            provenance="catalog",
        )

    body = (await client.get("/feed/grouped")).json()
    assert len(body["items"]) == 2
    news_item = next(i for i in body["items"] if i["news_backed"] is True)
    catalog_item = next(i for i in body["items"] if i["news_backed"] is False)
    assert news_item["event_count"] == 1
    assert news_item["top_event_type"] == "casting"
    assert catalog_item["event_count"] == 4
    assert catalog_item["top_event_type"] == "trailer"


async def test_grouped_news_backed_is_per_day_not_per_film(client, make_film, add_event):
    # A story on Monday must not flag the same film's TMDB-only Tuesday.
    film = await make_film(slug="two-days-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="reported",
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
        sources=({"url": "https://variety.com/monday"},),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="tmdb only",
        created_at=datetime(2026, 6, 2, tzinfo=UTC),
        provenance="catalog",
    )

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [(i["day"], i["news_backed"]) for i in items] == [
        ("2026-06-02", False),
        ("2026-06-01", True),
    ]


async def test_grouped_news_backed_is_per_film_not_per_day(client, make_film, add_event):
    # Two films sharing a day are classified independently; the flag must not spread across
    # the day's rows. Within-day order is alphabetical by title.
    reported = await make_film(slug="reported-film", popularity=5.0)
    tmdb_only = await make_film(slug="tmdb-film", popularity=90.0)
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=reported,
        summary="reported",
        created_at=day,
        sources=({"url": "https://variety.com/one"},),
    )
    await add_event(film=tmdb_only, summary="tmdb", created_at=day, provenance="catalog")

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [(i["film_ref"], i["news_backed"]) for i in items] == [
        (ref(reported), True),
        (ref(tmdb_only), False),
    ]


async def test_grouped_hidden_event_story_does_not_flag_the_day(client, make_film, add_event):
    # `other` never reaches the feed, so a story attached to one must not flag a day whose
    # only visible activity is TMDB's — the signal has to agree with what the row counts.
    film = await make_film(slug="hidden-source-2026")
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="other",
        summary="hype",
        created_at=day,
        sources=({"url": "https://variety.com/hype"},),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="tmdb trailer",
        created_at=day,
        provenance="catalog",
    )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["event_count"] == 1
    assert item["news_backed"] is False


async def test_grouped_promoted_event_stays_under_its_original_date_heading(
    client, make_film, add_event
):
    """ADR-0016's day axis, read through the promotion. TMDB carded the beat on the 1st; the
    trade that promoted it published on the 3rd. The row must stay under the 1st — the story's
    own date must not pull it forward, or a promoted card would jump date headings the day it
    gained a source.

    Asserted on the rendered feed rather than on `Event.created_at`, which the attach never
    writes to and so could not have failed here."""
    film = await make_film(slug="promoted-day-2026")
    await add_event(
        film=film,
        event_type="casting",
        summary="carded Monday, reported Wednesday",
        created_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        occurred_at=datetime(2026, 6, 1, 9, tzinfo=UTC),
        provenance="catalog",
        sources=(
            {
                "url": "https://variety.com/wednesday-scoop",
                "published_at": datetime(2026, 6, 3, 17, tzinfo=UTC),
            },
        ),
    )

    body = (await client.get("/feed/grouped")).json()
    assert [(i["day"], i["news_backed"]) for i in body["items"]] == [("2026-06-01", True)]


async def test_grouped_lists_every_beat_the_film_day_carries(client, make_film, add_event):
    """The row carries all of the day's distinct beats, not just the top one — the feed
    labels each of them under the title. Ordered most-significant first, so
    `event_types[0]` is `top_event_type`."""
    film = await make_film(slug="film-2026")
    for event_type in ("casting", "announced", "trailer"):
        await add_event(
            film=film,
            event_type=event_type,
            summary=event_type,
            created_at=datetime(2026, 6, 3, 8, tzinfo=UTC),
        )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["event_types"] == ["trailer", "casting", "announced"]
    assert item["top_event_type"] == item["event_types"][0]


async def test_grouped_event_types_dedupes_repeats_of_one_beat(client, make_film, add_event):
    film = await make_film(slug="film-2026")
    for index in range(3):
        await add_event(
            film=film,
            event_type="casting",
            summary=f"cast {index}",
            created_at=datetime(2026, 6, 3, 8 + index, tzinfo=UTC),
        )

    item = (await client.get("/feed/grouped")).json()["items"][0]
    assert item["event_types"] == ["casting"]
    assert item["event_count"] == 3


async def test_grouped_event_types_are_scoped_to_the_film_day(client, make_film, add_event):
    """Same film, two days: each row lists only its own day's beats."""
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

    items = (await client.get("/feed/grouped")).json()["items"]
    assert [(i["day"], i["event_types"]) for i in items] == [
        ("2026-06-02", ["trailer"]),
        ("2026-06-01", ["casting"]),
    ]


# NEU-1199 — split feed sections by source category


async def test_grouped_same_film_day_in_both_sections(client, make_film, add_event):
    """1 story event + 1 catalog event → 2 items, same film_ref and day, different news_backed."""
    film = await make_film(slug="both-2026", title="Both Sides")
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="casting",
        summary="reported",
        created_at=day,
        sources=({"url": "https://variety.com/casting"},),
    )
    await add_event(
        film=film,
        event_type="release_date",
        summary="tmdb date change",
        created_at=day,
        provenance="catalog",
    )

    body = (await client.get("/feed/grouped")).json()
    assert len(body["items"]) == 2
    for item in body["items"]:
        assert item["film_ref"] == ref(film)
        assert item["day"] == "2026-06-01"
    news_item = next(i for i in body["items"] if i["news_backed"] is True)
    catalog_item = next(i for i in body["items"] if i["news_backed"] is False)
    assert news_item["event_count"] == 1
    assert news_item["top_event_type"] == "casting"
    assert [e["event_type"] for e in news_item["events"]] == ["casting"]
    assert catalog_item["event_count"] == 1
    assert catalog_item["top_event_type"] == "release_date"
    assert [e["event_type"] for e in catalog_item["events"]] == ["release_date"]


async def test_grouped_split_event_count_and_types_are_scoped(client, make_film, add_event):
    """Multiple events per category: verify event_count and top_event_type are category-scoped."""
    film = await make_film(slug="scoped-2026", title="Scoped")
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="casting",
        summary="casting report",
        created_at=day,
        sources=({"url": "https://variety.com/casting"},),
    )
    await add_event(
        film=film,
        event_type="announced",
        summary="announcement story",
        created_at=day,
        sources=({"url": "https://deadline.com/announced"},),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="tmdb trailer",
        created_at=day,
        provenance="catalog",
    )
    await add_event(
        film=film,
        event_type="release_date",
        summary="tmdb date",
        created_at=day,
        provenance="catalog",
    )

    body = (await client.get("/feed/grouped")).json()
    assert len(body["items"]) == 2
    news_item = next(i for i in body["items"] if i["news_backed"] is True)
    catalog_item = next(i for i in body["items"] if i["news_backed"] is False)
    assert news_item["event_count"] == 2
    assert news_item["top_event_type"] == "casting"
    assert sorted(news_item["event_types"]) == ["announced", "casting"]
    assert sorted([e["event_type"] for e in news_item["events"]]) == ["announced", "casting"]
    assert catalog_item["event_count"] == 2
    assert catalog_item["top_event_type"] == "trailer"
    assert sorted(catalog_item["event_types"]) == ["release_date", "trailer"]
    assert sorted([e["event_type"] for e in catalog_item["events"]]) == ["release_date", "trailer"]


async def test_grouped_promoted_then_split(client, make_film, add_event):
    """Catalog event that gains a story stays in news-backed; remaining catalog-only events
    stay in catalog section."""
    film = await make_film(slug="promoted-split-2026", title="Promoted Split")
    day = datetime(2026, 6, 1, tzinfo=UTC)
    await add_event(
        film=film,
        event_type="casting",
        summary="TMDB carded it, then Variety confirmed",
        created_at=day,
        provenance="catalog",
        sources=({"url": "https://variety.com/scoop"},),
    )
    await add_event(
        film=film,
        event_type="trailer",
        summary="tmdb trailer only",
        created_at=day,
        provenance="catalog",
    )

    body = (await client.get("/feed/grouped")).json()
    assert len(body["items"]) == 2
    news_item = next(i for i in body["items"] if i["news_backed"] is True)
    catalog_item = next(i for i in body["items"] if i["news_backed"] is False)
    # The promoted (news-backed) casting event
    assert news_item["event_count"] == 1
    assert news_item["top_event_type"] == "casting"
    assert [e["event_type"] for e in news_item["events"]] == ["casting"]
    # The catalog-only trailer
    assert catalog_item["event_count"] == 1
    assert catalog_item["top_event_type"] == "trailer"
    assert [e["event_type"] for e in catalog_item["events"]] == ["trailer"]
