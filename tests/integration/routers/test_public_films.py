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


# ── film metadata (genres, companies, collection, scalars) ────────────────────


async def test_detail_exposes_film_metadata_all_fields(
    client, make_film, add_event, make_collection, attach_genres, attach_companies
):
    col = await make_collection(
        id=1, name="The Franchise Collection", poster_path="/collection.jpg"
    )
    film = await make_film(
        slug="meta-full-2026",
        title="Full Meta Film",
        overview="A gripping story.",
        tagline="The tagline.",
        runtime=120,
        vote_average=7.5,
        vote_count=1200,
        original_language="en",
        backdrop_path="/backdrop.jpg",
        collection_id=col.id,
    )
    await add_event(film=film, summary="Event.")
    await attach_genres(film, [(28, "Action"), (12, "Adventure")])
    await attach_companies(film, [(10, "Zeta Studios"), (5, "Alpha Films")])

    r = await client.get("/films/meta-full-2026")
    assert r.status_code == 200
    body = r.json()

    assert body["overview"] == "A gripping story."
    assert body["tagline"] == "The tagline."
    assert body["runtime"] == 120
    assert body["vote_average"] == 7.5
    assert body["vote_count"] == 1200
    assert body["original_language"] == "en"
    assert body["backdrop_path"] == "/backdrop.jpg"
    # genres: name-ascending — Action before Adventure
    assert body["genres"] == ["Action", "Adventure"]
    # companies: name-ascending — Alpha before Zeta
    assert body["production_companies"] == ["Alpha Films", "Zeta Studios"]
    assert body["collection"] == {
        "name": "The Franchise Collection",
        "poster_path": "/collection.jpg",
    }


async def test_detail_sparse_film_returns_nulls_and_empty_lists(client, make_film, add_event):
    film = await make_film(slug="meta-sparse-2026")
    await add_event(film=film, summary="Event.")

    r = await client.get("/films/meta-sparse-2026")
    assert r.status_code == 200
    body = r.json()

    assert body["overview"] is None
    assert body["tagline"] is None
    assert body["runtime"] is None
    assert body["vote_average"] is None
    assert body["vote_count"] is None
    assert body["original_language"] is None
    assert body["backdrop_path"] is None
    assert body["genres"] == []
    assert body["production_companies"] == []
    assert body["collection"] is None


async def test_detail_rating_raw_passthrough(client, make_film, add_event):
    film = await make_film(slug="meta-zero-rating-2026", vote_average=0.0, vote_count=0)
    await add_event(film=film, summary="Event.")

    r = await client.get("/films/meta-zero-rating-2026")
    assert r.status_code == 200
    body = r.json()
    assert body["vote_average"] == 0.0
    assert body["vote_count"] == 0


async def test_detail_metadata_is_scoped_per_film(
    client, make_film, add_event, make_collection, attach_genres, attach_companies
):
    """The genre/company/collection joins filter on film_id — one film's metadata never
    leaks into another's. Distinct reference ids per film avoid PK collisions."""
    col_a = await make_collection(id=1, name="Collection A")
    film_a = await make_film(slug="meta-scope-a-2026", title="Film A", collection_id=col_a.id)
    await add_event(film=film_a, summary="Event A.")
    await attach_genres(film_a, [(28, "Action"), (12, "Adventure")])
    await attach_companies(film_a, [(10, "Zeta Studios"), (5, "Alpha Films")])

    col_b = await make_collection(id=2, name="Collection B")
    film_b = await make_film(slug="meta-scope-b-2026", title="Film B", collection_id=col_b.id)
    await add_event(film=film_b, summary="Event B.")
    await attach_genres(film_b, [(99, "Horror")])
    await attach_companies(film_b, [(77, "Beta Films")])

    body_a = (await client.get("/films/meta-scope-a-2026")).json()
    assert body_a["genres"] == ["Action", "Adventure"]
    assert body_a["production_companies"] == ["Alpha Films", "Zeta Studios"]
    assert body_a["collection"] == {"name": "Collection A", "poster_path": None}

    body_b = (await client.get("/films/meta-scope-b-2026")).json()
    assert body_b["genres"] == ["Horror"]
    assert body_b["production_companies"] == ["Beta Films"]
    assert body_b["collection"] == {"name": "Collection B", "poster_path": None}


# ── alternative_titles exposure tests ────────────────────────────────────────


async def test_detail_no_alt_titles_returns_empty_list(client, make_film, add_event):
    film = await make_film(slug="no-alt-2026", title="No Alt Titles Film")
    await add_event(film=film, summary="Event.")

    r = await client.get("/films/no-alt-2026")
    assert r.status_code == 200
    assert r.json()["alternative_titles"] == []


async def test_detail_alt_titles_returned_as_strings(
    client, make_film, add_event, attach_alt_titles
):
    film = await make_film(slug="alt-basic-2026", title="Alt Titles Film")
    await add_event(film=film, summary="Event.")
    await attach_alt_titles(film, ["International Title", "Another Title"])

    r = await client.get("/films/alt-basic-2026")
    assert r.status_code == 200
    body = r.json()
    assert "alternative_titles" in body
    assert isinstance(body["alternative_titles"], list)
    assert all(isinstance(t, str) for t in body["alternative_titles"])
    assert "International Title" in body["alternative_titles"]
    assert "Another Title" in body["alternative_titles"]


async def test_detail_alt_titles_exclude_film_title(
    client, make_film, add_event, attach_alt_titles
):
    """Alt-titles equal to the canonical title (case-insensitive) are excluded."""
    film = await make_film(slug="alt-excl-title-2026", title="The Movie")
    await add_event(film=film, summary="Event.")
    await attach_alt_titles(film, ["The Movie", "the movie", "THE MOVIE", "Foreign Title"])

    r = await client.get("/films/alt-excl-title-2026")
    assert r.status_code == 200
    alt_titles = r.json()["alternative_titles"]
    assert "The Movie" not in alt_titles
    assert "the movie" not in alt_titles
    assert "THE MOVIE" not in alt_titles
    assert "Foreign Title" in alt_titles


async def test_detail_alt_titles_exclude_original_title(
    client, make_film, add_event, attach_alt_titles, session
):
    """Alt-titles equal to original_title (case-insensitive) are excluded."""
    film = await make_film(slug="alt-excl-orig-2026", title="The Movie")
    film.original_title = "기생충"
    session.add(film)
    await session.commit()
    await session.refresh(film)
    await add_event(film=film, summary="Event.")
    await attach_alt_titles(film, ["기생충", "기생충 Extra", "Another Title"])

    r = await client.get("/films/alt-excl-orig-2026")
    assert r.status_code == 200
    alt_titles = r.json()["alternative_titles"]
    assert "기생충" not in alt_titles
    assert "기생충 Extra" in alt_titles
    assert "Another Title" in alt_titles


async def test_detail_alt_titles_ordered_alphabetically_case_insensitive(
    client, make_film, add_event, attach_alt_titles
):
    """Alt-titles are returned in case-insensitive alphabetical order."""
    film = await make_film(slug="alt-order-2026", title="Order Film")
    await add_event(film=film, summary="Event.")
    await attach_alt_titles(film, ["Zebra Title", "apple title", "Mango Title"])

    r = await client.get("/films/alt-order-2026")
    assert r.status_code == 200
    alt_titles = r.json()["alternative_titles"]
    assert alt_titles == sorted(alt_titles, key=str.lower)


async def test_detail_alt_titles_capped_at_eight(client, make_film, add_event, attach_alt_titles):
    """More than 8 alt-titles are capped to 8."""
    film = await make_film(slug="alt-cap-2026", title="Cap Film")
    await add_event(film=film, summary="Event.")
    titles = [f"Title {chr(ord('A') + i)}" for i in range(12)]
    await attach_alt_titles(film, titles)

    r = await client.get("/films/alt-cap-2026")
    assert r.status_code == 200
    alt_titles = r.json()["alternative_titles"]
    assert len(alt_titles) <= 8


async def test_detail_alt_titles_distinct(client, make_film, add_event, attach_alt_titles):
    """Duplicate alt-title rows are returned as a single entry."""
    film = await make_film(slug="alt-dedup-2026", title="Dedup Film")
    await add_event(film=film, summary="Event.")
    await attach_alt_titles(film, ["Duplicate Title", "Duplicate Title", "Other Title"])

    r = await client.get("/films/alt-dedup-2026")
    assert r.status_code == 200
    alt_titles = r.json()["alternative_titles"]
    assert alt_titles.count("Duplicate Title") == 1


async def test_detail_alt_titles_scoped_per_film(client, make_film, add_event, attach_alt_titles):
    """Alt-titles from one film must not appear on another film's detail."""
    film_a = await make_film(slug="alt-scope-a-2026", title="Film A")
    await add_event(film=film_a, summary="Event A.")
    await attach_alt_titles(film_a, ["Title A Only"])

    film_b = await make_film(slug="alt-scope-b-2026", title="Film B")
    await add_event(film=film_b, summary="Event B.")
    await attach_alt_titles(film_b, ["Title B Only"])

    body_a = (await client.get("/films/alt-scope-a-2026")).json()
    body_b = (await client.get("/films/alt-scope-b-2026")).json()

    assert "Title A Only" in body_a["alternative_titles"]
    assert "Title B Only" not in body_a["alternative_titles"]
    assert "Title B Only" in body_b["alternative_titles"]
    assert "Title A Only" not in body_b["alternative_titles"]


# ── /films/search tests ───────────────────────────────────────────────────────


async def test_search_route_order_regression(client, make_film, add_event):
    """GET /films/search?q=… must resolve to the search handler, not /films/{slug}."""
    film = await make_film(slug="odyssey-2026", title="The Odyssey")
    await add_event(film=film, summary="A summary.")

    r = await client.get("/films/search", params={"q": "odyssey"})
    body = r.json()
    # Must return an index envelope, not a FilmDetailResponse or slug-lookup 404.
    assert r.status_code == 200
    assert "items" in body
    assert "total" in body
    assert "limit" in body
    assert "offset" in body


async def test_search_title_match_case_insensitive_and_substring(client, make_film, add_event):
    """A visible film is returned for exact, case-insensitive, and mid-word substring queries."""
    film = await make_film(slug="odyssey-2026", title="The Odyssey")
    await add_event(film=film, summary="A summary.")
    other = await make_film(slug="other-2026", title="Completely Different")
    await add_event(film=other, summary="Another summary.")

    for q in ["odyssey", "ODYSSEY", "dyss"]:
        r = await client.get("/films/search", params={"q": q})
        assert r.status_code == 200, f"q={q!r} → {r.status_code}"
        slugs = [i["slug"] for i in r.json()["items"]]
        assert "odyssey-2026" in slugs, f"q={q!r}: expected odyssey-2026 in {slugs}"
        assert "other-2026" not in slugs, f"q={q!r}: other-2026 should be excluded"


async def test_search_original_title_match(client, make_film, add_event, session):
    """A film matched by original_title is returned; NULL original_title is excluded."""
    film = await make_film(slug="parasite-2019", title="Parasite")
    film.original_title = "기생충"
    session.add(film)
    await session.commit()
    await session.refresh(film)
    await add_event(film=film, summary="A summary.")

    no_match = await make_film(slug="no-match-2026", title="Unrelated Film")
    # original_title is None by default — must not match on NULL ILIKE
    await add_event(film=no_match, summary="Another summary.")

    r = await client.get("/films/search", params={"q": "기생충"})
    assert r.status_code == 200
    slugs = [i["slug"] for i in r.json()["items"]]
    assert "parasite-2019" in slugs
    assert "no-match-2026" not in slugs


async def test_search_visibility_parity_no_summarized_event(client, make_film, add_event):
    """A film whose title matches but has no summarized event is excluded, same as /films."""
    # visible: has summarized event
    shown = await make_film(slug="shown-2026", title="The Odyssey Shown")
    await add_event(film=shown, event_type="casting", summary="A summary.")
    # hidden: event exists but no summary
    hidden = await make_film(slug="hidden-odyssey-2026", title="The Odyssey Hidden")
    await add_event(film=hidden, summary=None)
    # bare: no events at all
    _ = await make_film(slug="bare-odyssey-2026", title="The Odyssey Bare")

    r = await client.get("/films/search", params={"q": "Odyssey"})
    assert r.status_code == 200
    slugs = [i["slug"] for i in r.json()["items"]]
    assert "shown-2026" in slugs
    assert "hidden-odyssey-2026" not in slugs
    assert "bare-odyssey-2026" not in slugs


async def test_search_visibility_parity_only_other_events(client, make_film, add_event):
    """A film whose only summarized events are 'other'-type is excluded from search."""
    shown = await make_film(slug="real-search-2026", title="Odyssey Real")
    await add_event(film=shown, event_type="casting", summary="s")
    other_only = await make_film(slug="otheronly-search-2026", title="Odyssey Other Only")
    await add_event(film=other_only, event_type="other", summary="s")

    r = await client.get("/films/search", params={"q": "Odyssey"})
    assert r.status_code == 200
    slugs = [i["slug"] for i in r.json()["items"]]
    assert "real-search-2026" in slugs
    assert "otheronly-search-2026" not in slugs


async def test_search_envelope_and_pagination(client, make_film, add_event):
    """total/limit/offset are echoed correctly; paging slices the matched set."""
    for i in range(5):
        film = await make_film(slug=f"odyssey-search-{i}-2026", title=f"Odyssey Search {i}")
        await add_event(film=film, summary="s")
    # a non-matching film
    other = await make_film(slug="other-2026", title="Completely Different")
    await add_event(film=other, summary="s")

    # Default limit=20
    r = await client.get("/films/search", params={"q": "Odyssey Search"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 20
    assert body["offset"] == 0
    assert len(body["items"]) == 5

    # Page 1: limit=2
    page1 = (
        await client.get("/films/search", params={"q": "Odyssey Search", "limit": 2, "offset": 0})
    ).json()
    assert page1["total"] == 5
    assert len(page1["items"]) == 2
    assert page1["limit"] == 2
    assert page1["offset"] == 0

    # Page 2: limit=2, offset=2
    page2 = (
        await client.get("/films/search", params={"q": "Odyssey Search", "limit": 2, "offset": 2})
    ).json()
    assert page2["total"] == 5
    assert len(page2["items"]) == 2
    assert page2["offset"] == 2

    # Last page: limit=2, offset=4
    page3 = (
        await client.get("/films/search", params={"q": "Odyssey Search", "limit": 2, "offset": 4})
    ).json()
    assert page3["total"] == 5
    assert len(page3["items"]) == 1


async def test_search_rejects_out_of_range_pagination(client):
    """limit=0, limit=101, offset=-1 all return 422."""
    r0 = await client.get("/films/search", params={"q": "odyssey", "limit": 0})
    assert r0.status_code == 422
    r1 = await client.get("/films/search", params={"q": "odyssey", "limit": 101})
    assert r1.status_code == 422
    r2 = await client.get("/films/search", params={"q": "odyssey", "offset": -1})
    assert r2.status_code == 422


async def test_search_item_shape_parity(client, make_film, add_event):
    """A search hit carries the same keys as an index item for the same film."""
    film = await make_film(slug="shape-2026", title="Shape Film", poster_path="/shape.jpg")
    await add_event(film=film, event_type="casting", summary="A summary.")

    index_r = await client.get("/films")
    search_r = await client.get("/films/search", params={"q": "Shape Film"})

    assert search_r.status_code == 200
    search_items = search_r.json()["items"]
    assert len(search_items) == 1
    item = search_items[0]

    index_items = index_r.json()["items"]
    index_item = next(i for i in index_items if i["slug"] == "shape-2026")

    assert item["slug"] == index_item["slug"]
    assert item["title"] == index_item["title"]
    assert item["release_year"] == index_item["release_year"]
    assert item["poster_path"] == index_item["poster_path"]
    assert item["arc_stage"] == index_item["arc_stage"]


async def test_search_subthreshold_q_returns_empty(client):
    """Queries below the two-alphanumeric gate return 200 with an empty page, not 422.

    Covers blank, whitespace-only, single-character, and all-punctuation queries. The
    all-punctuation cases ("%", "_", "--", "\\") pin the gate for the same input shapes
    the wildcard-literal escaping handles, so a future gate change can't silently start
    running an unbounded scan on them (see get_film_search's gate comment)."""
    for q in ["", "   ", "a", "%", "_", "--", "\\"]:
        r = await client.get("/films/search", params={"q": q})
        assert r.status_code == 200, f"q={q!r} → {r.status_code}"
        body = r.json()
        assert body["items"] == [], f"q={q!r}: expected empty items"
        assert body["total"] == 0, f"q={q!r}: expected total=0"


async def test_search_like_wildcard_is_literal(client, make_film, add_event):
    """% and _ in q are matched literally, not as LIKE wildcards (queries clear the gate)."""
    # "50%" has two alphanumerics, so it clears the gate. Escaped, it matches only the
    # literal "50%"; an unescaped %50%% would also sweep in "5000 Reasons".
    percent_film = await make_film(slug="percent-film-2026", title="50% Off")
    await add_event(film=percent_film, summary="s")
    not_percent = await make_film(slug="not-percent-2026", title="5000 Reasons")
    await add_event(film=not_percent, summary="s")

    r_percent = await client.get("/films/search", params={"q": "50%"})
    assert r_percent.status_code == 200
    slugs_percent = [i["slug"] for i in r_percent.json()["items"]]
    assert "percent-film-2026" in slugs_percent
    assert "not-percent-2026" not in slugs_percent

    # "py_Ki" has four alphanumerics. Escaped, the "_" matches a literal underscore, so
    # only "Spy_Kids" matches; an unescaped "_" wildcard would also match "SpyXKids".
    underscore_film = await make_film(slug="under-film-2026", title="Spy_Kids")
    await add_event(film=underscore_film, summary="s")
    not_underscore = await make_film(slug="not-under-2026", title="SpyXKids")
    await add_event(film=not_underscore, summary="s")

    r_underscore = await client.get("/films/search", params={"q": "py_Ki"})
    assert r_underscore.status_code == 200
    slugs_underscore = [i["slug"] for i in r_underscore.json()["items"]]
    assert "under-film-2026" in slugs_underscore
    assert "not-under-2026" not in slugs_underscore


async def test_search_backslash_is_literal(client, make_film, add_event):
    """A literal backslash in q round-trips through the escape= wiring at the SQL level."""
    # "C\\D" has two alphanumerics (C, D) so it clears the gate. _escape_like doubles the
    # backslash and ILIKE(escape="\\") collapses it back to one literal "\", matching only
    # the title that actually contains "C\D". A broken escape= would match the wrong row.
    backslash_film = await make_film(slug="acdc-slash-2026", title="AC\\DC")
    await add_event(film=backslash_film, summary="s")
    plain_film = await make_film(slug="acdc-plain-2026", title="ACDC")
    await add_event(film=plain_film, summary="s")

    r = await client.get("/films/search", params={"q": "C\\D"})
    assert r.status_code == 200
    slugs = [i["slug"] for i in r.json()["items"]]
    assert "acdc-slash-2026" in slugs
    assert "acdc-plain-2026" not in slugs
