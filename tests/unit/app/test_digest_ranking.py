"""The digest's pure functions (NEU-1460, NEU-1462, NEU-1528, NEU-1529, NEU-1530): how the lines a
batch's reaches deliver are laid out — the daily as timeline days, the weekly by entry; follow
blocks, sections, update types, film and entity rows — how films rank for the subject, what
the subject and preheader say, which day the daily carries the slate, how the slate nests as the
calendar does, and which marker a slate row wears.

Hand-built lines throughout — none of this touches the database, which is the point of the
functions being pure. `test_digest_sender.py` proves the loader feeds them what they expect."""

from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from upmovies.app.services.digest_sender import (
    DAY_LINE_ORDER,
    MAX_DAY_POSTERS,
    DigestBatch,
    DigestBeat,
    DigestFilm,
    DigestLine,
    DigestReach,
    DigestRecipient,
    DigestRow,
    DigestSource,
    SlateItem,
    arc_stage_label,
    beat_order_key,
    carries_slate,
    change_from_summary,
    day_posters,
    digest_context,
    digest_preheader,
    digest_subject,
    group_blocks,
    group_days,
    group_rows,
    group_slate,
    group_update_types,
    group_week,
    natural_title,
    rank_films,
    short_date,
    slate_marker,
    slate_months,
)
from upmovies.config import get_settings
from upmovies.public.dto import CalendarItem

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


def _calendar(
    title: str = "Dune", *, day: date = TODAY, bucket: str = "wide", tmdb_id: int = 1
) -> CalendarItem:
    return CalendarItem(
        film_ref=f"{tmdb_id}-{title.lower()}",
        film_title=title,
        release_year=day.year,
        poster_path=None,
        release_date=day,
        release_type=bucket,
        director=None,
        stars=[],
        genres=[],
    )


def _slate_item(title: str = "Dune", *, marker=None, **kwargs) -> SlateItem:
    return SlateItem(calendar=_calendar(title, **kwargs), marker=marker)


def _slate(n: int):
    return group_slate(_slate_item(tmdb_id=i) for i in range(n))


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

    days = group_days([older, newer])

    assert [d.day for d in days] == [T0.date(), (T0 - timedelta(days=1)).date()]


def test_the_publication_day_is_the_utc_one():
    late_evening_utc = datetime(2026, 9, 22, 23, 30, tzinfo=UTC)

    (day,) = group_days([_title(_beat(created_at=late_evening_utc))])

    assert day.day == date(2026, 9, 22)


# --- the weekly's entries (FB-19) -----------------------------------------------------------


def test_a_weekly_reads_by_entry_one_film_entry_across_the_week():
    """No days: a film's cards from three days are one entry, its lines in publication order —
    not the feed's event order, which would put the older-news casting first."""
    first = _beat("Heat 2", "trailer", created_at=T0 - timedelta(days=3))
    second = _beat("Heat 2", "trailer", created_at=T0 - timedelta(days=1))
    old_news = _beat("Heat 2", "trailer", created_at=T0, occurred_at=T0 - timedelta(days=30))

    week = group_week([_title(old_news), _title(first), _title(second)])

    (films,) = week.blocks
    (section,) = films.sections
    (trailer,) = section.update_types
    (entry,) = trailer.rows
    assert entry.beats == (first, second, old_news)


def test_an_entity_with_cards_on_two_films_is_one_entry_with_its_lines_in_publication_order():
    """An entity entry spans its films and days; its lines run as the week went (FB-19), not
    film by film as the daily's entity row does (FB-6)."""
    ada = _reach("person", "Ada", "1")
    zodiac = _beat("Zodiac", "casting", created_at=T0 - timedelta(days=2))
    abyss = _beat("The Abyss", "casting", created_at=T0)

    week = group_week([_via(ada, abyss), _via(ada, zodiac)])

    (people,) = week.blocks
    (attached,) = people.sections[0].update_types
    (entry,) = attached.rows
    assert entry.reach == ada
    assert [b.film.title for b in entry.beats] == ["Zodiac", "The Abyss"]


def test_a_film_with_cards_in_both_sections_is_an_entry_in_both():
    week = group_week(
        [
            _title(_beat("Heat 2", "casting", news=True, created_at=T0 - timedelta(days=2))),
            _title(_beat("Heat 2", "release_date", created_at=T0)),
        ]
    )

    (films,) = week.blocks
    news, catalog = films.sections
    assert _titles(news.rows) == ["Heat 2"]
    (release_date,) = catalog.update_types
    assert _titles(release_date.rows) == ["Heat 2"]


def test_film_entries_rank_by_significance_then_title_and_entity_entries_by_name():
    """DC-3 kept within the Films block; FB-6's name order for the entity blocks — across
    every day of the week."""
    week = group_week(
        [
            _title(_beat("Arrival", "casting", news=True, created_at=T0)),
            _title(_beat("Zodiac", "release_date", news=True, created_at=T0 - timedelta(days=4))),
            _title(_beat("Cobra", "casting", news=True, created_at=T0 - timedelta(days=1))),
            _via(_reach("person", "Zed", "1"), _beat("Heat 2", news=True)),
            _via(
                _reach("person", "Bea", "2"),
                _beat("Heat 2", news=True, created_at=T0 - timedelta(days=5)),
            ),
        ]
    )

    films, people = week.blocks
    assert _titles(films.sections[0].rows) == ["Zodiac", "Arrival", "Cobra"]
    assert _titles(people.sections[0].rows) == ["Bea", "Zed"]


def test_the_week_has_one_poster_strip_over_its_films_news_backed_first():
    week = group_week(
        [
            _title(_beat("Arrival", created_at=T0)),
            _title(_beat("Zodiac", news=True, created_at=T0 - timedelta(days=3))),
            _via(_reach("company", "A24"), _beat("Arrival", created_at=T0 - timedelta(days=1))),
        ]
    )

    assert [film.title for film in week.posters] == ["Zodiac", "Arrival"]


def test_blocks_come_in_their_fixed_order_and_an_empty_one_is_left_out():
    """FB-1: Films, People, Studios, Franchises — however the lines arrive, and a block with
    nothing in it is silence."""
    lines = [
        _via(_reach("franchise", "Alien"), _beat("Romulus", "collection_attached")),
        _via(_reach("person", "Ada"), _beat("Heat 2", "casting")),
        _title(_beat("Dune", "trailer")),
    ]

    blocks = group_blocks(lines, order=DAY_LINE_ORDER)

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

    (films,) = group_blocks(lines, order=DAY_LINE_ORDER)

    news, catalog = films.sections
    assert (news.news_backed, catalog.news_backed) == (True, False)
    assert _titles(news.rows) == ["Heat 2"] and news.update_types == ()
    assert catalog.rows == () and [t.key for t in catalog.update_types] == ["cast"]


def test_a_block_with_only_one_section_has_only_that_section():
    (films,) = group_blocks([_title(_beat(news=True))], order=DAY_LINE_ORDER)

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

    blocks = group_blocks(lines, order=DAY_LINE_ORDER)

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
        order=DAY_LINE_ORDER,
    )

    assert _titles(rows) == ["Zed", "The Abyss", "Cobra"]


def test_a_film_row_holds_every_beat_of_its_film_in_the_feeds_event_order():
    later = _beat("Heat 2", "casting", occurred_at=T0 + timedelta(hours=2))
    earlier = _beat("Heat 2", "crew_attached", occurred_at=T0)

    (row,) = group_rows([_title(later), _title(earlier)], order=DAY_LINE_ORDER)

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
        order=DAY_LINE_ORDER,
    )

    assert _titles(rows) == ["ada Lovelace", "Bea", "The Ada"]
    assert [b.film.title for b in rows[0].beats] == ["The Abyss", "Heat 2"]

    studios = group_rows(
        [
            _via(_reach("company", "The Weinstein Company", "1"), heat),
            _via(_reach("company", "Universal", "2"), heat),
        ],
        order=DAY_LINE_ORDER,
    )
    assert _titles(studios) == ["Universal", "The Weinstein Company"]


def test_an_entity_the_catalog_cannot_name_trails_under_its_fallback_headline():
    rows = group_rows(
        [
            _via(_reach("person", None, "9"), _beat()),
            _via(_reach("person", "Zed", "1"), _beat()),
        ],
        order=DAY_LINE_ORDER,
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
    rows = group_rows([_title(b) for b in beats], order=DAY_LINE_ORDER)

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
        order=DAY_LINE_ORDER,
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
    (day,) = group_days([_via(_reach("company", "A24"), _beat("Heat 2"))])

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

    daily = _context(_title(beat), cadence="daily")
    weekly = _context(_title(beat), cadence="weekly")

    assert daily["week"] is None
    (day,) = _days(daily)
    assert day["heading"] == "Tuesday, September 22, 2026"
    assert day["blocks"][0]["sections"][0]["rows"][0]["lines"][0]["prefix"] == "New trailer"
    assert weekly["days"] == []
    week = cast(dict, weekly["week"])
    assert "heading" not in week
    assert week["blocks"][0]["sections"][0]["rows"][0]["lines"][0]["prefix"] == (
        "22 Sep · New trailer"
    )


def test_an_entity_entrys_lines_each_carry_their_date():
    ada = _reach("person", "Ada", "7")
    context = _context(
        _via(ada, _beat("Zodiac", "casting", created_at=T0 - timedelta(days=2))),
        _via(ada, _beat("Heat 2", "casting", created_at=T0)),
        cadence="weekly",
    )

    week = cast(dict, context["week"])
    (entry,) = week["blocks"][0]["sections"][0]["update_types"][0]["rows"]
    assert [(line["prefix"], line["film"]["title"]) for line in entry["lines"]] == [
        ("20 Sep", "Zodiac"),
        ("22 Sep", "Heat 2"),
    ]


def test_a_weekly_with_only_a_slate_has_no_week():
    context = digest_context(
        _batch(slate=1), cadence="weekly", today=TODAY, settings=get_settings()
    )

    assert (context["days"], context["week"]) == ([], None)


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
    batch = DigestBatch(
        recipient=RECIPIENT,
        lines=(),
        unsendable=(),
        slate=group_slate((_slate_item("A"), _slate_item("B", marker="new"))),
    )

    context = digest_context(batch, cadence="daily", today=TODAY, settings=get_settings())

    (month,) = cast(list[dict], context["slate"])
    (day,) = month["days"]
    (bucket,) = day["buckets"]
    assert [f["marker"] for f in bucket["films"]] == [None, "new"]
    assert context["days"] == []


# --- the slate as the my-films calendar (NEU-1530, FB-26) --------------------------------


def test_the_slate_groups_by_date_then_bucket_in_the_calendars_order():
    """`_calendar_type_rank`'s order within a date — wide, limited, digital, physical — and the
    rows of one bucket in the order the calendar page gave them, not re-sorted."""
    later = TODAY + timedelta(days=3)
    days = group_slate(
        (
            _slate_item("Zodiac", day=later, bucket="physical"),
            _slate_item("Edge", day=later, bucket="digital"),
            _slate_item("Casino", day=TODAY, bucket="limited"),
            _slate_item("Blade", day=TODAY, bucket="wide"),
            _slate_item("Arrival", day=TODAY, bucket="wide"),
        )
    )

    assert [d.day for d in days] == [TODAY, later]
    assert [(b.bucket, b.label) for b in days[0].buckets] == [
        ("wide", "Wide"),
        ("limited", "Limited"),
    ]
    assert [i.calendar.film_title for i in days[0].buckets[0].items] == ["Blade", "Arrival"]
    assert [b.label for b in days[1].buckets] == ["Digital", "Physical"]
    assert [d.count for d in days] == [3, 2]


def test_a_slate_inside_one_month_has_no_month_heading():
    days = group_slate(
        (_slate_item(day=date(2026, 10, 1)), _slate_item(day=date(2026, 10, 31), tmdb_id=2))
    )

    (month,) = slate_months(days)
    assert month.heading is None
    assert [d.day for d in month.days] == [date(2026, 10, 1), date(2026, 10, 31)]


def test_a_slate_across_a_month_boundary_heads_each_month_and_no_year():
    """FB-26: the month level only where the window crosses one; never a year level, even
    across December."""
    days = group_slate(
        (
            _slate_item(day=date(2026, 12, 20)),
            _slate_item(day=date(2026, 12, 31), tmdb_id=2),
            _slate_item(day=date(2027, 1, 4), tmdb_id=3),
        )
    )

    months = slate_months(days)
    assert [m.heading for m in months] == ["December", "January"]
    assert [[d.day.day for d in m.days] for m in months] == [[20, 31], [4]]


def test_an_empty_slate_has_no_months():
    assert slate_months(()) == ()


def test_the_context_carries_the_calendar_rows_fields_and_the_w92_poster():
    item = CalendarItem(
        film_ref="42-dune",
        film_title="Dune",
        release_year=2026,
        poster_path="/dune.jpg",
        release_date=TODAY,
        release_type="wide",
        director="Denis Villeneuve",
        stars=["A", "B", "C"],
        genres=["Drama", "Sci-Fi"],
    )
    settings = get_settings().model_copy(
        update={"public_base_url": "https://app.test/", "tmdb_image_base": "https://img.test/p"}
    )
    batch = DigestBatch(
        recipient=RECIPIENT,
        lines=(),
        unsendable=(),
        slate=group_slate((SlateItem(calendar=item, marker="moved"),)),
    )

    context = digest_context(batch, cadence="weekly", today=TODAY, settings=settings)

    (month,) = cast(list[dict], context["slate"])
    (film,) = month["days"][0]["buckets"][0]["films"]
    assert film == {
        "title": "Dune",
        "year": 2026,
        "url": "https://app.test/film/42-dune",
        "poster_url": "https://img.test/p/w92/dune.jpg",
        "director": "Denis Villeneuve",
        "stars": "A · B · C",
        "genres": "Drama · Sci-Fi",
        "marker": "moved",
    }
    assert month["days"][0]["heading"] == "Thursday, September 24, 2026"
