"""The digest's pure functions (NEU-1460, NEU-1462, NEU-1528): how the lines a batch's reaches
deliver are laid out as timeline days — follow blocks, sections, update types, film and entity
rows — how films rank for the subject, what the subject and preheader say, which day the daily
carries the slate, and which marker a slate row wears.

Hand-built lines throughout — none of this touches the database, which is the point of the
functions being pure. `test_digest_sender.py` proves the loader feeds them what they expect."""

from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from upmovies.app.services.digest_sender import (
    MAX_DAY_POSTERS,
    DigestBatch,
    DigestBeat,
    DigestFilm,
    DigestLine,
    DigestReach,
    DigestRecipient,
    DigestRow,
    DigestSource,
    SlateDay,
    SlateItem,
    arc_stage_label,
    beat_order_key,
    carries_slate,
    change_from_summary,
    day_posters,
    digest_context,
    digest_preheader,
    digest_subject,
    event_order_key,
    group_blocks,
    group_days,
    group_rows,
    group_update_types,
    natural_title,
    rank_films,
    short_date,
    slate_marker,
)
from upmovies.config import get_settings

TODAY = date(2026, 9, 24)
T0 = datetime(2026, 9, 22, 9, tzinfo=UTC)

RECIPIENT = DigestRecipient(
    user_id=UUID(int=1), email="ada@example.com", display_name="Ada", deliverable=True
)

_films: dict[str, DigestFilm] = {}


def _film(title: str, *, tmdb_id: int | None = None, poster: bool = True) -> DigestFilm:
    """One `DigestFilm` per title, so two beats on "Heat 2" are on the same film."""
    if title not in _films:
        n = tmdb_id if tmdb_id is not None else len(_films) + 1
        _films[title] = DigestFilm(
            film_id=uuid4(),
            tmdb_id=n,
            title=title,
            film_url=f"https://app.example.test/film/{n}",
            poster_url=f"https://image.test/w154/{n}.jpg" if poster else None,
            parenthetical="2026",
        )
    return _films[title]


def _beat(
    title: str = "Heat 2",
    event_type: str = "casting",
    *,
    news: bool = False,
    created_at: datetime = T0,
    occurred_at: datetime | None = None,
    confidence: str = "confirmed",
    event_id: UUID | None = None,
    summary: str = "Something happened.",
) -> DigestBeat:
    return DigestBeat(
        notification_id=uuid4(),
        event_id=event_id or uuid4(),
        event_type=event_type,
        created_at=created_at,
        occurred_at=occurred_at or created_at,
        confidence=confidence,
        summary=summary,
        source=DigestSource(name="Variety", url="https://variety.test/a") if news else None,
        news_backed=news,
        film=_film(title),
    )


def _reach(entity_type: str, name: str | None, entity_id: str = "1") -> DigestReach:
    return DigestReach(
        entity_type=entity_type,
        entity_id=entity_id,
        name=name,
        url=None if name is None else f"https://app.example.test/{entity_type}/{entity_id}",
    )


def _title(beat: DigestBeat) -> DigestLine:
    return DigestLine(beat=beat, reach=None)


def _via(reach: DigestReach, beat: DigestBeat) -> DigestLine:
    return DigestLine(beat=beat, reach=reach)


def _slate(n: int) -> tuple[SlateDay, ...]:
    item = SlateItem(
        title="Dune", release_label="Wide release", film_url="https://x.test/f", poster_url=None
    )
    return (SlateDay(day=TODAY, items=(item,) * n),) if n else ()


def _batch(*lines: DigestLine, slate: int = 0) -> DigestBatch:
    return DigestBatch(recipient=RECIPIENT, lines=lines, unsendable=(), slate=_slate(slate))


def _titles(rows: tuple[DigestRow, ...]) -> list[str]:
    return [row.film.title if row.reach is None else row.reach.headline for row in rows]


@pytest.fixture(autouse=True)
def _fresh_films():
    _films.clear()


# --- the day, block and section tree (FB-18, FB-1) ---------------------------------------


def test_a_daily_lays_each_publication_day_out_newest_first():
    older = _title(_beat("Heat 2", created_at=T0 - timedelta(days=1)))
    newer = _title(_beat("Dune", created_at=T0))

    days = group_days([older, newer], dated=True)

    assert [d.day for d in days] == [T0.date(), (T0 - timedelta(days=1)).date()]


def test_the_publication_day_is_the_utc_one():
    late_evening_utc = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)

    (day,) = group_days([_title(_beat(created_at=late_evening_utc))], dated=True)

    assert day.day == date(2026, 9, 22)


def test_a_weekly_lays_every_line_out_in_one_undated_group():
    lines = [
        _title(_beat("Heat 2", created_at=T0 - timedelta(days=3))),
        _title(_beat("Dune", created_at=T0)),
    ]

    (group,) = group_days(lines, dated=False)

    assert group.day is None
    (films,) = group.blocks
    (section,) = films.sections
    (update_type,) = section.update_types
    assert _titles(update_type.rows) == ["Dune", "Heat 2"]


def test_blocks_come_in_their_fixed_order_and_an_empty_one_is_left_out():
    """FB-1: Films, People, Studios, Franchises — however the lines arrive, and a block with
    nothing in it is silence."""
    lines = [
        _via(_reach("franchise", "Alien"), _beat("Romulus", "collection_attached")),
        _via(_reach("person", "Ada"), _beat("Heat 2", "casting")),
        _title(_beat("Dune", "trailer")),
    ]

    blocks = group_blocks(lines, beat_key=event_order_key)

    assert [(b.key, b.label) for b in blocks] == [
        ("films", "Films"),
        ("people", "People"),
        ("franchises", "Franchises"),
    ]


def test_each_block_splits_into_in_the_news_then_not_yet_reported():
    lines = [
        _title(_beat("Heat 2", "casting", news=False)),
        _title(_beat("Heat 2", "trailer", news=True)),
    ]

    (films,) = group_blocks(lines, beat_key=event_order_key)

    news, catalog = films.sections
    assert (news.news_backed, catalog.news_backed) == (True, False)
    assert _titles(news.rows) == ["Heat 2"] and news.update_types == ()
    assert catalog.rows == () and [t.key for t in catalog.update_types] == ["cast"]


def test_a_block_with_only_one_section_has_only_that_section():
    (films,) = group_blocks([_title(_beat(news=True))], beat_key=event_order_key)

    assert [s.news_backed for s in films.sections] == [True]


def test_a_card_two_follows_reached_is_a_line_in_each_of_their_blocks():
    """FB-5: a cancellation reaching a followed director, a followed studio and a title follow
    is a line under all three."""
    cancel = _beat("Cleopatra", "canceled")
    lines = [
        _title(cancel),
        _via(_reach("person", "Denis Villeneuve"), cancel),
        _via(_reach("company", "Legendary"), cancel),
    ]

    blocks = group_blocks(lines, beat_key=event_order_key)

    assert [b.key for b in blocks] == ["films", "people", "studios"]
    for block in blocks:
        (section,) = block.sections
        (update_type,) = section.update_types
        (row,) = update_type.rows
        assert row.beats == (cancel,)


# --- rows (FB-3, FB-6) -------------------------------------------------------------------


def test_film_rows_rank_by_significance_then_natural_title():
    """The release date outranks two castings; between those, "The Abyss" sorts as "Abyss"."""
    rows = group_rows(
        [
            _title(_beat("Cobra", "casting")),
            _title(_beat("The Abyss", "casting")),
            _title(_beat("Zed", "release_date")),
        ],
        beat_key=event_order_key,
    )

    assert _titles(rows) == ["Zed", "The Abyss", "Cobra"]


def test_a_film_row_holds_every_beat_of_its_film_in_the_feeds_event_order():
    later = _beat("Heat 2", "casting", occurred_at=T0 + timedelta(hours=2))
    earlier = _beat("Heat 2", "crew_attached", occurred_at=T0)

    (row,) = group_rows([_title(later), _title(earlier)], beat_key=event_order_key)

    assert row.beats == (earlier, later)


def test_entity_rows_merge_their_films_and_sort_by_name():
    """One row per entity, its lines by film natural title; people by name as written,
    studios as titles sort ("The" ignored)."""
    ada = _reach("person", "ada Lovelace", "1")
    bea = _reach("person", "Bea", "2")
    the_ada = _reach("person", "The Ada", "3")
    heat = _beat("Heat 2")
    abyss = _beat("The Abyss")
    rows = group_rows(
        [_via(the_ada, heat), _via(bea, heat), _via(ada, heat), _via(ada, abyss)],
        beat_key=event_order_key,
    )

    assert _titles(rows) == ["ada Lovelace", "Bea", "The Ada"]
    assert [b.film.title for b in rows[0].beats] == ["The Abyss", "Heat 2"]

    studios = group_rows(
        [
            _via(_reach("company", "The Weinstein Company", "1"), heat),
            _via(_reach("company", "Universal", "2"), heat),
        ],
        beat_key=event_order_key,
    )
    assert _titles(studios) == ["Universal", "The Weinstein Company"]


def test_an_entity_the_catalog_cannot_name_trails_under_its_fallback_headline():
    rows = group_rows(
        [
            _via(_reach("person", None, "9"), _beat()),
            _via(_reach("person", "Zed", "1"), _beat()),
        ],
        beat_key=event_order_key,
    )

    assert _titles(rows) == ["Zed", "A person you follow"]
    assert rows[1].reach is not None and rows[1].reach.url is None


def test_natural_title_strips_a_leading_article_and_casefolds():
    assert natural_title("The Batman") == "batman"
    assert natural_title("An Affair") == "affair"
    assert natural_title("Theater") == "theater"


# --- update types (NR-3, FB-4) -----------------------------------------------------------


def test_films_use_the_feeds_update_types_in_their_order():
    beats = [
        _beat("Heat 2", "casting"),
        _beat("Heat 2", "release_date"),
        _beat("Heat 2", "first_look"),
        _beat("Heat 2", "now_available"),
        _beat("Heat 2", "canceled"),
    ]
    rows = group_rows([_title(b) for b in beats], beat_key=event_order_key)

    types = group_update_types(rows, block="films")

    assert [(t.key, t.label) for t in types] == [
        ("now_available", "Now available"),
        ("release_date", "Release date"),
        ("production_status", "Production status"),
        ("cast", "Cast"),
        ("other", "Other updates"),
    ]
    assert all(len(t.rows) == 1 and len(t.rows[0].beats) == 1 for t in types)


def test_entities_use_attached_detached_canceled_other():
    """FB-4: `casting` is Attached under People, not Cast."""
    ada = _reach("person", "Ada")
    rows = group_rows(
        [
            _via(ada, _beat("A", "canceled")),
            _via(ada, _beat("B", "crew_removed")),
            _via(ada, _beat("C", "casting")),
            _via(ada, _beat("D", "trailer")),
        ],
        beat_key=event_order_key,
    )

    types = group_update_types(rows, block="people")

    assert [(t.key, t.label) for t in types] == [
        ("attached", "Attached"),
        ("detached", "Detached"),
        ("canceled", "Canceled"),
        ("other", "Other updates"),
    ]
    assert [t.rows[0].beats[0].film.title for t in types] == ["C", "B", "A", "D"]


# --- the poster strip (FB-7) -------------------------------------------------------------


def test_the_strip_leads_with_news_backed_films_de_duplicated_and_capped():
    _film("No Poster", poster=False)
    lines = [
        _title(_beat("Zodiac", news=True)),
        _title(_beat("Arrival", news=False)),
        _via(_reach("person", "Ada"), _beat("Arrival", news=False)),
        _title(_beat("No Poster")),
        *(_title(_beat(f"Film {n}")) for n in range(10)),
    ]

    posters = day_posters(lines)

    assert len(posters) == MAX_DAY_POSTERS
    assert [p.title for p in posters[:3]] == ["Zodiac", "Arrival", "Film 0"]
    assert "No Poster" not in [p.title for p in posters]


def test_a_day_of_only_entity_rows_still_has_a_strip():
    (day,) = group_days([_via(_reach("company", "A24"), _beat("Heat 2"))], dated=True)

    assert [p.title for p in day.posters] == ["Heat 2"]


# --- ranking for the subject (DC-7, FB-22) -----------------------------------------------


def test_films_rank_by_their_most_significant_beat_on_the_arc():
    ranked = rank_films(
        [_beat("A casting", "casting"), _beat("B first look", "first_look")]
        + [_beat("Z release", "casting"), _beat("Z release", "release_date")]
    )

    assert [r.film.title for r in ranked] == ["Z release", "A casting", "B first look"]
    assert ranked[0].lead_type == "release_date"


def test_a_stage_tie_is_broken_by_casefolded_title_then_by_tmdb_id():
    _film("beta", tmdb_id=5)
    _film("Alpha", tmdb_id=9)
    _film("alpha", tmdb_id=2)

    ranked = rank_films([_beat("beta"), _beat("Alpha"), _beat("alpha")])

    assert [(r.film.title, r.film.tmdb_id) for r in ranked] == [
        ("alpha", 2),
        ("Alpha", 9),
        ("beta", 5),
    ]


def test_beats_order_by_publication_then_occurrence_then_id():
    """The weekly's order: `created_at` leads."""
    later_published_earlier_news = _beat(
        created_at=T0 + timedelta(hours=2), occurred_at=T0 - timedelta(days=30)
    )
    first = _beat(created_at=T0, occurred_at=T0)
    same_instant_low = _beat(
        created_at=T0 + timedelta(hours=1), occurred_at=T0, event_id=UUID(int=1)
    )
    same_instant_high = _beat(
        created_at=T0 + timedelta(hours=1), occurred_at=T0, event_id=UUID(int=2)
    )
    same_publish_older_news = _beat(
        created_at=T0 + timedelta(hours=1), occurred_at=T0 - timedelta(days=1)
    )

    ordered = sorted(
        [
            later_published_earlier_news,
            same_instant_high,
            first,
            same_instant_low,
            same_publish_older_news,
        ],
        key=beat_order_key,
    )

    assert ordered == [
        first,
        same_publish_older_news,
        same_instant_low,
        same_instant_high,
        later_published_earlier_news,
    ]


def test_the_short_date_carries_the_year_only_when_it_is_not_the_runs():
    assert short_date(date(2026, 9, 22), today=TODAY) == "22 Sep"
    assert short_date(date(2026, 1, 3), today=TODAY) == "3 Jan"
    assert short_date(date(2025, 9, 22), today=TODAY) == "22 Sep 2025"


def test_the_arc_stage_labels_are_the_frontends_four():
    assert [arc_stage_label(s) for s in ("announced", "shooting", "wrapped", "released")] == [
        "Announced",
        "Shooting",
        "Wrapped",
        "Released",
    ]
    assert arc_stage_label("in_production") == "Announced"


# --- subject (DC-7) ----------------------------------------------------------------------


def test_the_subject_names_the_lead_film_and_its_lead_beat():
    assert digest_subject(_batch(_title(_beat("Heat 2", "casting")))) == "Heat 2 — casting"


def test_the_subject_counts_the_other_films_singular_and_plural():
    one_more = _batch(_title(_beat("Heat 2")), _title(_beat("Dune")))
    two_more = _batch(_title(_beat("Heat 2")), _title(_beat("Dune")), _title(_beat("Ran")))

    # A stage tie, so the casefolded title leads.
    assert digest_subject(one_more) == "Dune — casting, + 1 more film"
    assert digest_subject(two_more) == "Dune — casting, + 2 more films"


def test_the_subject_counts_films_not_rows():
    """A film under two entity rows and a title row is one film (FB-22)."""
    cancel = _beat("Cleopatra", "canceled")
    batch = _batch(
        _title(cancel),
        _via(_reach("person", "Denis"), cancel),
        _via(_reach("company", "Legendary"), cancel),
        _via(_reach("company", "Legendary"), _beat("Heat 2", "company_attached")),
    )

    assert digest_subject(batch) == "Cleopatra — canceled, + 1 more film"
    assert len(batch.item_ids) == 2


def test_a_film_reached_only_through_a_studio_can_lead():
    batch = _batch(
        _title(_beat("Arrival", "casting")),
        _via(_reach("company", "A24"), _beat("Zodiac", "canceled")),
    )

    assert digest_subject(batch) == "Zodiac — canceled, + 1 more film"


def test_lines_and_a_slate_add_your_slate():
    assert (
        digest_subject(_batch(_title(_beat("Dune: Part Three", "trailer")), slate=2))
        == "Dune: Part Three — new trailer · your slate"
    )


def test_a_slate_alone_counts_its_dates():
    assert digest_subject(_batch(slate=1)) == "Your slate: 1 upcoming date"
    assert digest_subject(_batch(slate=3)) == "Your slate: 3 upcoming dates"


def test_an_empty_batch_has_no_subject_to_build():
    with pytest.raises(ValueError):
        digest_subject(_batch())


def test_an_empty_batch_has_no_context_either():
    """The loud failure reaches the context builder too, rather than a quiet empty subject."""
    with pytest.raises(ValueError):
        digest_context(_batch(), cadence="weekly", today=TODAY, settings=get_settings())


# --- preheader (DC-10) -------------------------------------------------------------------


def test_the_preheader_for_a_slate_alone():
    assert digest_preheader(_batch(slate=2)) == "Your slate: 2 dates in the next 30 days."
    assert digest_preheader(_batch(slate=1)) == "Your slate: 1 date in the next 30 days."


def test_the_preheader_names_up_to_two_films_after_the_lead_across_reaches():
    batch = _batch(
        _title(_beat("Heat 2", "casting")),
        _via(_reach("person", "Ada"), _beat("Dune", "trailer")),
        _via(_reach("company", "A24"), _beat("Ran", "announced")),
        _title(_beat("Zodiac", "announced")),
    )

    assert digest_preheader(batch) == "Also: Heat 2 — casting; Ran — announced"


def test_the_preheader_joins_the_slate_and_the_films():
    batch = _batch(_title(_beat("Heat 2")), _title(_beat("Dune", "trailer")), slate=1)

    assert digest_preheader(batch) == (
        "Your slate: 1 date in the next 30 days. Also: Heat 2 — casting"
    )


def test_one_film_and_no_slate_has_nothing_to_preview():
    assert digest_preheader(_batch(_title(_beat("Heat 2")))) == ""


# --- the batch and its context (FB-20, FB-21) --------------------------------------------


def test_nothing_is_capped_and_every_card_is_an_item_once():
    lines = [_title(_beat(f"Film {n:02}")) for n in range(30)]
    shared = _beat("Shared")
    batch = _batch(*lines, _title(shared), _via(_reach("person", "Ada"), shared))

    assert len(batch.item_ids) == 31
    assert batch.has_content


def _context(*lines: DigestLine, cadence: str = "daily") -> dict[str, object]:
    return digest_context(
        _batch(*lines),
        cadence=cadence,  # type: ignore[arg-type]
        today=TODAY,
        settings=get_settings(),
    )


def _days(context: dict[str, object]) -> list[dict]:
    return cast(list[dict], context["days"])


def test_the_context_has_no_lead_no_cap_and_no_following():
    context = _context(_title(_beat()))

    for retired in ("lead", "entries", "overflow", "overflow_line", "timeline_url"):
        assert retired not in context


def test_in_the_news_lines_carry_the_label_and_the_marker_and_their_source():
    context = _context(_title(_beat("Heat 2", "casting", news=True, confidence="rumored")))

    (day,) = _days(context)
    (block,) = day["blocks"]
    (section,) = block["sections"]
    assert (section["label"], section["qualifier"]) == ("In the news", None)
    (row,) = section["rows"]
    assert row["film"]["title"] == "Heat 2" and row["entity"] is None
    (line,) = row["lines"]
    assert line == {
        "prefix": "Casting",
        "unconfirmed": True,
        "film": None,
        "summary": "Something happened.",
        "source": {"name": "Variety", "url": "https://variety.test/a"},
    }


def test_not_yet_reported_drops_the_marker_and_the_label_under_a_named_type_only():
    context = _context(
        _title(_beat("Heat 2", "casting", confidence="rumored")),
        _title(_beat("Heat 2", "first_look", confidence="rumored")),
    )

    (day,) = _days(context)
    (section,) = day["blocks"][0]["sections"]
    assert (section["label"], section["qualifier"]) == ("Not yet reported", "(unconfirmed)")
    cast_type, other = section["update_types"]
    assert cast_type["rows"][0]["lines"][0]["prefix"] is None
    assert other["rows"][0]["lines"][0]["prefix"] == "First look"
    for update_type in (cast_type, other):
        assert update_type["rows"][0]["lines"][0]["unconfirmed"] is False


def test_an_entity_line_names_and_links_its_film():
    context = _context(_via(_reach("person", "Ada", "7"), _beat("Heat 2", "casting")))

    (day,) = _days(context)
    (row,) = day["blocks"][0]["sections"][0]["update_types"][0]["rows"]
    assert row["entity"] == {"name": "Ada", "url": "https://app.example.test/person/7"}
    assert row["film"] is None
    (line,) = row["lines"]
    assert line["film"] == {
        "title": "Heat 2",
        "parenthetical": "2026",
        "url": _film("Heat 2").film_url,
    }


def test_the_daily_omits_the_date_and_the_weekly_keeps_it():
    beat = _beat("Heat 2", "trailer", news=True)

    daily = _days(_context(_title(beat), cadence="daily"))
    weekly = _days(_context(_title(beat), cadence="weekly"))

    assert daily[0]["heading"] == "Tuesday, September 22, 2026"
    assert daily[0]["blocks"][0]["sections"][0]["rows"][0]["lines"][0]["prefix"] == "New trailer"
    assert weekly[0]["heading"] is None
    assert (
        weekly[0]["blocks"][0]["sections"][0]["rows"][0]["lines"][0]["prefix"]
        == "22 Sep · New trailer"
    )


def test_justwatch_is_credited_per_news_film_row_and_once_under_now_available():
    """DC-17 under In the news; NR-8 under Not yet reported — the heading credits it, not each
    row; and never on an entity row."""
    context = _context(
        _title(_beat("Zodiac", "now_available", news=True)),
        _title(_beat("Arrival", "now_available")),
        _title(_beat("Seven", "now_available")),
        _via(_reach("company", "A24"), _beat("Seven", "now_available")),
    )

    (day,) = _days(context)
    films, studios = day["blocks"]
    news, catalog = films["sections"]
    assert [r["credits_justwatch"] for r in news["rows"]] == [True]
    (now_available,) = catalog["update_types"]
    assert now_available["credits_justwatch"] is True
    assert [r["credits_justwatch"] for r in now_available["rows"]] == [False, False]
    (other,) = studios["sections"][0]["update_types"]
    assert other["credits_justwatch"] is False
    assert other["rows"][0]["credits_justwatch"] is False


# --- the slate day and the slate markers (NEU-1462) --------------------------------------


@pytest.mark.parametrize(
    ("weekday", "today", "expected"),
    [
        ("thursday", date(2026, 9, 24), True),  # a Thursday
        ("thursday", date(2026, 9, 25), False),
        ("friday", date(2026, 9, 25), True),
        ("monday", date(2026, 9, 21), True),
        ("sunday", date(2026, 9, 27), True),
        ("sunday", date(2026, 9, 21), False),
    ],
)
def test_the_daily_carries_the_slate_on_the_slate_weekday_only(weekday, today, expected):
    settings = get_settings().model_copy(update={"slate_weekday": weekday})

    assert carries_slate("daily", today, settings) is expected


@pytest.mark.parametrize("today", [date(2026, 9, 21) + timedelta(days=n) for n in range(7)])
def test_the_weekly_carries_the_slate_whatever_day_it_runs(today):
    """The setting documents the weekly slot's day; it does not gate it (DC-2)."""
    assert carries_slate("weekly", today, get_settings()) is True


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        ([], None),
        (["set"], "new"),
        (["moved"], "moved"),
        (["moved", "moved"], "moved"),
        # Set and then moved inside one window: the previous slate had no date at all.
        (["set", "moved"], "new"),
        (["moved", "set"], "new"),
    ],
)
def test_a_slate_marker_is_new_when_any_change_set_the_date_and_moved_otherwise(changes, expected):
    assert slate_marker(changes) == expected


@pytest.mark.parametrize(
    ("summary", "bucket", "expected"),
    [
        ("US wide release date set to 14 October 2026.", "wide", "set"),
        ("US wide release date slipped from 1 October 2026 to 14 October 2026.", "wide", "moved"),
        ("US wide release date moved from 14 October 2026 to 1 October 2026.", "wide", "moved"),
        # One card, two markets: each reads its own clause.
        (
            "US limited release date set to 2 October 2026. "
            "US wide release date moved from 9 October 2026 to 16 October 2026.",
            "limited",
            "set",
        ),
        (
            "US limited release date set to 2 October 2026. "
            "US wide release date moved from 9 October 2026 to 16 October 2026.",
            "wide",
            "moved",
        ),
        # An admin-rewritten body, or none at all, is "moved otherwise".
        ("The date is now 14 October.", "wide", "moved"),
        (None, "wide", "moved"),
    ],
)
def test_the_summary_fallback_reads_the_markets_own_verb(summary, bucket, expected):
    assert change_from_summary(summary, bucket) == expected


def test_the_context_carries_each_slate_rows_marker():
    items = (
        SlateItem(title="A", release_label="Wide release", film_url="u", poster_url=None),
        SlateItem(
            title="B", release_label="Wide release", film_url="u", poster_url=None, marker="new"
        ),
    )
    batch = DigestBatch(
        recipient=RECIPIENT, lines=(), unsendable=(), slate=(SlateDay(day=TODAY, items=items),)
    )

    context = digest_context(batch, cadence="daily", today=TODAY, settings=get_settings())

    (day,) = cast(list[dict[str, object]], context["slate"])
    assert [e["marker"] for e in cast(list[dict[str, object]], day["entries"])] == [None, "new"]
    assert context["days"] == []
