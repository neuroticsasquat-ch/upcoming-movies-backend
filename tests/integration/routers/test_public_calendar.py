from datetime import UTC, datetime

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def make_film(make_film):
    """Override: default popularity above the calendar's visibility floor (> 1.5), since
    most tests in this file aren't exercising the popularity gate itself."""

    async def _make(*, popularity: float | None = 10.0, **kwargs):
        return await make_film(popularity=popularity, **kwargs)

    return _make


_FUTURE = datetime(2099, 7, 4, 0, 0, tzinfo=UTC)
_PAST = datetime(2000, 1, 1, 0, 0, tzinfo=UTC)


def _today_dt() -> datetime:
    today = datetime.now(UTC).date()
    return datetime(today.year, today.month, today.day, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Case 1 — Envelope + defaults (empty DB)
# ---------------------------------------------------------------------------


async def test_calendar_empty_db(client):
    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"items": [], "total": 0, "limit": 20, "offset": 0}


# ---------------------------------------------------------------------------
# Case 2 — Future-first cutoff
# ---------------------------------------------------------------------------


async def test_calendar_future_cutoff(client, make_film, add_release_date):
    film_past = await make_film(slug="film-past", title="Past Film")
    await add_release_date(film=film_past, release_date=_PAST, release_type=3)

    film_future = await make_film(slug="film-future", title="Future Film")
    await add_release_date(film=film_future, release_date=_FUTURE, release_type=3)

    film_today = await make_film(slug="film-today", title="Today Film")
    await add_release_date(film=film_today, release_date=_today_dt(), release_type=3)

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    slugs = [item["film_slug"] for item in body["items"]]
    assert "film-past" not in slugs
    assert "film-future" in slugs
    assert "film-today" in slugs


# ---------------------------------------------------------------------------
# Case 3 — US-only scoping
# ---------------------------------------------------------------------------


async def test_calendar_us_only(client, make_film, add_release_date):
    film_gb_only = await make_film(slug="film-gb-only", title="GB Only")
    await add_release_date(film=film_gb_only, iso_3166_1="GB", release_date=_FUTURE, release_type=3)

    film_us_and_gb = await make_film(slug="film-us-gb", title="US and GB")
    await add_release_date(
        film=film_us_and_gb, iso_3166_1="US", release_date=_FUTURE, release_type=3
    )
    await add_release_date(
        film=film_us_and_gb, iso_3166_1="GB", release_date=_FUTURE, release_type=3
    )

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    slugs = [item["film_slug"] for item in body["items"]]
    assert "film-gb-only" not in slugs
    assert slugs.count("film-us-gb") == 1


# ---------------------------------------------------------------------------
# Case 4 — Type mapping → buckets
# ---------------------------------------------------------------------------


async def test_calendar_type_mapping(client, make_film, add_release_date):
    film_type1 = await make_film(slug="film-type1", title="Type 1")
    await add_release_date(film=film_type1, release_type=1, release_date=_FUTURE)

    film_type2 = await make_film(slug="film-type2", title="Type 2")
    await add_release_date(film=film_type2, release_type=2, release_date=_FUTURE)

    film_type3 = await make_film(slug="film-type3", title="Type 3")
    await add_release_date(film=film_type3, release_type=3, release_date=_FUTURE)

    film_type4 = await make_film(slug="film-type4", title="Type 4")
    await add_release_date(film=film_type4, release_type=4, release_date=_FUTURE)

    film_type5 = await make_film(slug="film-type5", title="Type 5")
    await add_release_date(film=film_type5, release_type=5, release_date=_FUTURE)

    film_type6 = await make_film(slug="film-type6", title="Type 6")
    await add_release_date(film=film_type6, release_type=6, release_date=_FUTURE)

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    items_by_slug = {item["film_slug"]: item for item in body["items"]}

    assert items_by_slug["film-type2"]["release_type"] == "limited"
    assert items_by_slug["film-type3"]["release_type"] == "wide"
    assert items_by_slug["film-type3"]["release_year"] == 2026  # from Film.release_date default
    assert "film-type1" not in items_by_slug  # premiere excluded
    assert "film-type4" not in items_by_slug
    assert "film-type5" not in items_by_slug
    assert "film-type6" not in items_by_slug


# ---------------------------------------------------------------------------
# Case 5 — Premiere/festival (type 1) is excluded entirely
# ---------------------------------------------------------------------------


async def test_calendar_excludes_premiere(client, make_film, add_release_date):
    film = await make_film(slug="film-cannes", title="Cannes Film")
    await add_release_date(
        film=film,
        release_type=1,
        release_date=_FUTURE,
        note="Cannes Film Festival",
    )

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    items_by_slug = {item["film_slug"]: item for item in body["items"]}
    assert "film-cannes" not in items_by_slug


# ---------------------------------------------------------------------------
# Case 6 — Ordering: soonest-first, then by release_type, then by popularity, then slug
# ---------------------------------------------------------------------------


async def test_calendar_ordering(client, make_film, add_release_date):
    near_future = datetime(2090, 1, 1, 0, 0, tzinfo=UTC)
    far_future = datetime(2095, 6, 6, 0, 0, tzinfo=UTC)

    # Film with limited release nearer than a wide release
    film_near_limited = await make_film(slug="zzz-near-limited", title="Near Limited")
    await add_release_date(film=film_near_limited, release_type=2, release_date=near_future)

    film_far_wide = await make_film(slug="zzz-far-wide", title="Far Wide")
    await add_release_date(film=film_far_wide, release_type=3, release_date=far_future)

    # Same date, limited < wide (type ordering)
    same_date = datetime(2092, 3, 15, 0, 0, tzinfo=UTC)
    film_same_wide = await make_film(slug="zzz-same-wide", title="Same Wide")
    await add_release_date(film=film_same_wide, release_type=3, release_date=same_date)

    film_same_limited = await make_film(slug="zzz-same-limited", title="Same Limited")
    await add_release_date(film=film_same_limited, release_type=2, release_date=same_date)

    # Same date + type: popularity tiebreak (higher popularity first)
    tie_date = datetime(2093, 8, 20, 0, 0, tzinfo=UTC)
    film_tie_low_pop = await make_film(slug="zzz-tie-low", title="Tie Low Pop", popularity=10.0)
    await add_release_date(film=film_tie_low_pop, release_type=3, release_date=tie_date)

    film_tie_high_pop = await make_film(slug="zzz-tie-high", title="Tie High Pop", popularity=99.0)
    await add_release_date(film=film_tie_high_pop, release_type=3, release_date=tie_date)

    # Same date + type + no popularity: slug ASC tiebreak
    slug_date = datetime(2094, 4, 1, 0, 0, tzinfo=UTC)
    film_slug_b = await make_film(slug="zzz-slug-b", title="Slug B")
    await add_release_date(film=film_slug_b, release_type=3, release_date=slug_date)

    film_slug_a = await make_film(slug="zzz-slug-a", title="Slug A")
    await add_release_date(film=film_slug_a, release_type=3, release_date=slug_date)

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    slugs = [item["film_slug"] for item in body["items"]]

    # near_future (2090) < same_date (2092) < tie_date (2093) < slug_date (2094) < far_future (2095)
    idx_near_limited = slugs.index("zzz-near-limited")
    idx_far_wide = slugs.index("zzz-far-wide")
    idx_same_limited = slugs.index("zzz-same-limited")
    idx_same_wide = slugs.index("zzz-same-wide")
    idx_tie_high = slugs.index("zzz-tie-high")
    idx_tie_low = slugs.index("zzz-tie-low")
    idx_slug_a = slugs.index("zzz-slug-a")
    idx_slug_b = slugs.index("zzz-slug-b")

    # Date ordering
    assert idx_near_limited < idx_same_limited
    assert idx_same_wide < idx_tie_high
    assert idx_tie_high < idx_slug_a
    assert idx_slug_a < idx_far_wide

    # Type ordering within same date: wide < limited (wide releases listed first)
    assert idx_same_wide < idx_same_limited

    # Popularity tiebreak: higher popularity first
    assert idx_tie_high < idx_tie_low

    # Slug tiebreak: slug ASC
    assert idx_slug_a < idx_slug_b


# ---------------------------------------------------------------------------
# Case 7 — Distinct (film, date, bucket) de-dup
# ---------------------------------------------------------------------------


async def test_calendar_dedup(client, make_film, add_release_date):
    # Two type-3 rows for the same film on same date → collapses to ONE calendar row
    film_dup = await make_film(slug="film-dup", title="Dup Film")
    await add_release_date(
        film=film_dup,
        release_type=3,
        release_date=_FUTURE,
        note="Note A",
        certification="PG",
    )
    await add_release_date(
        film=film_dup,
        release_type=3,
        release_date=_FUTURE,
        note="Note B",
        certification="PG-13",
    )

    # Film with both type-2 and type-3 future rows → yields TWO calendar rows
    film_two = await make_film(slug="film-two-types", title="Two Types")
    await add_release_date(film=film_two, release_type=2, release_date=_FUTURE)
    await add_release_date(film=film_two, release_type=3, release_date=_FUTURE)

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()

    dup_rows = [item for item in body["items"] if item["film_slug"] == "film-dup"]
    assert len(dup_rows) == 1

    two_rows = [item for item in body["items"] if item["film_slug"] == "film-two-types"]
    assert len(two_rows) == 2
    buckets = {row["release_type"] for row in two_rows}
    assert buckets == {"limited", "wide"}


# ---------------------------------------------------------------------------
# Case 8 — Visibility
# ---------------------------------------------------------------------------


async def test_calendar_visibility(client, make_film, add_release_date):
    # Film with slug=None → excluded
    film_no_slug = await make_film(slug=None, title="No Slug")
    await add_release_date(film=film_no_slug, release_date=_FUTURE, release_type=3)

    # Film with adult=True → excluded
    film_adult = await make_film(slug="film-adult", title="Adult Film", adult=True)
    await add_release_date(film=film_adult, release_date=_FUTURE, release_type=3)

    # Film with no events/summaries but valid future US type-3 row → INCLUDED
    film_no_events = await make_film(slug="film-no-events", title="No Events Film")
    await add_release_date(film=film_no_events, release_date=_FUTURE, release_type=3)

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    slugs = [item["film_slug"] for item in body["items"]]

    assert None not in slugs
    assert "film-adult" not in slugs
    assert "film-no-events" in slugs


# ---------------------------------------------------------------------------
# Case 9 — limit/offset + total
# ---------------------------------------------------------------------------


async def test_calendar_pagination(client, make_film, add_release_date):
    # Insert 5 future US wide films
    for i in range(1, 6):
        film = await make_film(slug=f"film-page-{i}", title=f"Page Film {i}")
        # Stagger release dates so ordering is deterministic
        rd = datetime(2099, i, 1, 0, 0, tzinfo=UTC)
        await add_release_date(film=film, release_date=rd, release_type=3)

    # Default: all 5
    resp = await client.get("/calendar")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 5

    # limit=2 offset=0
    resp = await client.get("/calendar?limit=2&offset=0")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2

    # limit=2 offset=4 → 1 remaining
    resp = await client.get("/calendar?limit=2&offset=4")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 1

    # limit=0 → 422
    resp = await client.get("/calendar?limit=0")
    assert resp.status_code == 422

    # limit=201 → 422
    resp = await client.get("/calendar?limit=201")
    assert resp.status_code == 422

    # offset=-1 → 422
    resp = await client.get("/calendar?offset=-1")
    assert resp.status_code == 422


async def test_calendar_paginates_by_date_not_film_rows(client, make_film, add_release_date):
    """limit/offset count distinct dates: date A (2 films) + date B (1 film) → total=2 dates;
    a 1-date page returns every film on that date, not a single film row."""
    a1 = await make_film(slug="cal-a1", title="Cal A1")
    a2 = await make_film(slug="cal-a2", title="Cal A2")
    b1 = await make_film(slug="cal-b1", title="Cal B1")
    for f in (a1, a2):
        await add_release_date(
            film=f, release_date=datetime(2099, 1, 1, tzinfo=UTC), release_type=3
        )
    await add_release_date(film=b1, release_date=datetime(2099, 2, 1, tzinfo=UTC), release_type=3)

    page1 = (await client.get("/calendar", params={"limit": 1, "offset": 0})).json()
    assert page1["total"] == 2  # two distinct dates, not three film rows
    assert {i["film_slug"] for i in page1["items"]} == {"cal-a1", "cal-a2"}
    assert all(i["release_date"].startswith("2099-01-01") for i in page1["items"])

    page2 = (await client.get("/calendar", params={"limit": 1, "offset": 1})).json()
    assert page2["total"] == 2
    assert [i["film_slug"] for i in page2["items"]] == ["cal-b1"]


# ---------------------------------------------------------------------------
# Case 10 — Director, stars (≤3), and genres (≤3) on each row
# ---------------------------------------------------------------------------


async def test_calendar_includes_director_stars_genres(
    client, make_film, add_release_date, attach_credits, attach_genres
):
    film = await make_film(slug="film-enriched", title="Enriched Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await attach_credits(
        film,
        cast=[
            {"id": 1, "name": "First Star", "credit_order": 0},
            {"id": 2, "name": "Second Star", "credit_order": 1},
            {"id": 3, "name": "Third Star", "credit_order": 2},
            {"id": 4, "name": "Fourth Star", "credit_order": 3},
        ],
        crew=[
            {"id": 5, "name": "The Director", "job": "Director", "department": "Directing"},
            {"id": 6, "name": "A Producer", "job": "Producer", "department": "Production"},
        ],
    )
    await attach_genres(film, [(28, "Action"), (18, "Drama"), (12, "Adventure"), (35, "Comedy")])

    resp = await client.get("/calendar")
    assert resp.status_code == 200
    item = next(i for i in resp.json()["items"] if i["film_slug"] == "film-enriched")

    assert item["director"] == "The Director"  # job=Director only; Producer excluded
    assert item["stars"] == ["First Star", "Second Star", "Third Star"]  # capped at 3, in order
    assert len(item["genres"]) == 3  # capped at 3
    assert item["genres"] == ["Action", "Adventure", "Comedy"]  # ordered by name


async def test_calendar_joins_co_directors(client, make_film, add_release_date, attach_credits):
    film = await make_film(slug="film-codir", title="Co-Directed Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)
    await attach_credits(
        film,
        crew=[
            {"id": 10, "name": "Bob Director", "job": "Director", "credit_order": 1},
            {"id": 11, "name": "Ann Director", "job": "Director", "credit_order": 0},
        ],
    )

    item = next(
        i for i in (await client.get("/calendar")).json()["items"] if i["film_slug"] == "film-codir"
    )
    assert item["director"] == "Ann Director, Bob Director"  # credit_order asc → Ann first


async def test_calendar_defaults_when_no_metadata(client, make_film, add_release_date):
    film = await make_film(slug="film-bare", title="Bare Film")
    await add_release_date(film=film, release_date=_FUTURE, release_type=3)

    item = next(
        i for i in (await client.get("/calendar")).json()["items"] if i["film_slug"] == "film-bare"
    )
    assert item["director"] is None
    assert item["stars"] == []
    assert item["genres"] == []
