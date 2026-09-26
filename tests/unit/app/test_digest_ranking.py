"""The digest's pure functions (NEU-1460, NEU-1462): how film entries rank, how beats order,
how dates and the status line read, what the subject and preheader say, which day the daily
carries the slate, and which marker a slate row wears.

Hand-built entries throughout — none of this touches the database, which is the point of the
functions being pure. `test_digest_sender.py` proves the loader feeds them what they expect."""

from datetime import UTC, date, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest

from upmovies.app.services.digest_sender import (
    DIGEST_MAX_ENTRIES,
    DigestBatch,
    DigestBeat,
    DigestEntry,
    DigestFilm,
    DigestHeader,
    DigestRecipient,
    SlateDay,
    SlateItem,
    arc_stage_label,
    beat_order_key,
    carries_slate,
    change_from_summary,
    digest_context,
    digest_preheader,
    digest_subject,
    long_date,
    rank_entries,
    short_date,
    slate_marker,
    status_line,
)
from upmovies.catalog.headline_release import HeadlineRelease
from upmovies.config import get_settings

TODAY = date(2026, 9, 24)
T0 = datetime(2026, 9, 22, 9, tzinfo=UTC)

RECIPIENT = DigestRecipient(
    user_id=UUID(int=1), email="ada@example.com", display_name="Ada", deliverable=True
)


def _beat(
    event_type: str = "casting",
    *,
    created_at: datetime = T0,
    occurred_at: datetime = T0,
    event_id: UUID | None = None,
) -> DigestBeat:
    return DigestBeat(
        notification_id=uuid4(),
        event_id=event_id or uuid4(),
        event_type=event_type,
        created_at=created_at,
        occurred_at=occurred_at,
        confidence="confirmed",
        summary="Something happened.",
        source=None,
    )


def _entry(title: str, *types: str, tmdb_id: int = 1, poster: str | None = None) -> DigestEntry:
    return DigestEntry(
        film=DigestFilm(
            film_id=uuid4(),
            tmdb_id=tmdb_id,
            title=title,
            film_url=f"https://app.example.test/film/{tmdb_id}",
            poster_url=poster and f"https://image.test/w154{poster}",
            lead_poster_url=poster and f"https://image.test/w185{poster}",
        ),
        header=DigestHeader(parenthetical="2026", status="Announced"),
        following=(),
        beats=tuple(_beat(t) for t in types or ("casting",)),
    )


def _slate(n: int) -> tuple[SlateDay, ...]:
    item = SlateItem(
        title="Dune", release_label="Wide release", film_url="https://x.test/f", poster_url=None
    )
    return (SlateDay(day=TODAY, items=(item,) * n),) if n else ()


def _batch(*entries: DigestEntry, slate: int = 0) -> DigestBatch:
    return DigestBatch(recipient=RECIPIENT, entries=entries, unsendable=(), slate=_slate(slate))


# --- ranking ----------------------------------------------------------------------------


def test_entries_rank_by_their_most_significant_beat_on_the_arc():
    """The lead beat is the entry's most significant one, whatever else it carries: a film
    with a casting and a release date ranks as a release date."""
    casting = _entry("A casting", "casting")
    released = _entry("Z release", "casting", "release_date")
    first_look = _entry("B first look", "first_look")

    ranked = rank_entries([casting, first_look, released])

    assert [e.film.title for e in ranked] == ["Z release", "A casting", "B first look"]
    assert ranked[0].lead_type == "release_date"


def test_a_stage_tie_is_broken_by_casefolded_title_then_by_tmdb_id():
    b = _entry("beta", "casting", tmdb_id=5)
    a_later = _entry("Alpha", "casting", tmdb_id=9)
    a_earlier = _entry("alpha", "casting", tmdb_id=2)

    ranked = rank_entries([b, a_later, a_earlier])

    assert [(e.film.title, e.film.tmdb_id) for e in ranked] == [
        ("alpha", 2),
        ("Alpha", 9),
        ("beta", 5),
    ]


def test_an_entry_of_only_unstaged_beats_ranks_last():
    """`other` and `first_look` sit below `announced` (`event_stage_rank` -1)."""
    other = _entry("AAA", "other")
    announced = _entry("ZZZ", "announced")

    assert [e.film.title for e in rank_entries([other, announced])] == ["ZZZ", "AAA"]


def test_beats_order_by_publication_then_occurrence_then_id():
    """`created_at` leads — the ticket flips the old sender's `occurred_at`-first order."""
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


# --- dates and the status line -----------------------------------------------------------


def test_the_short_date_carries_the_year_only_when_it_is_not_the_runs():
    assert short_date(date(2026, 9, 22), today=TODAY) == "22 Sep"
    assert short_date(date(2026, 1, 3), today=TODAY) == "3 Jan"
    assert short_date(date(2025, 9, 22), today=TODAY) == "22 Sep 2025"


def test_the_long_date_has_no_ordinal_and_no_zero_pad():
    assert long_date(date(2026, 8, 14)) == "14 August 2026"
    assert long_date(date(2026, 10, 3)) == "3 October 2026"


@pytest.mark.parametrize(
    ("release", "expected"),
    [
        pytest.param(
            HeadlineRelease(date=date(2026, 8, 14), kind="upcoming", country="US", bucket="wide"),
            "Wide release · 14 August 2026",
            id="upcoming",
        ),
        pytest.param(
            HeadlineRelease(date=date(2026, 3, 3), kind="released", country="FR", bucket="limited"),
            "Limited release · 3 March 2026",
            id="released, tense-free",
        ),
        pytest.param(
            HeadlineRelease(date=date(2027, 5, 1), kind="primary", country=None, bucket=None),
            "1 May 2027 (unconfirmed)",
            id="primary",
        ),
        pytest.param(None, "Shooting", id="undated falls back to the arc stage"),
    ],
)
def test_the_status_line(release, expected):
    assert status_line(release, "shooting") == expected


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
    assert digest_subject(_batch(_entry("Heat 2", "casting"))) == "Heat 2 — casting"


def test_the_subject_counts_the_other_films_singular_and_plural():
    one_more = _batch(_entry("Heat 2", "casting"), _entry("Dune"))
    two_more = _batch(_entry("Heat 2", "casting"), _entry("Dune"), _entry("Ran"))

    assert digest_subject(one_more) == "Heat 2 — casting, + 1 more film"
    assert digest_subject(two_more) == "Heat 2 — casting, + 2 more films"


def test_the_subject_counts_entries_past_the_cap():
    entries = [_entry(f"Film {n:02}", tmdb_id=n) for n in range(DIGEST_MAX_ENTRIES + 1)]
    batch = _batch(*entries)

    assert len(batch.rendered_entries) == DIGEST_MAX_ENTRIES
    assert batch.overflow == 1
    assert digest_subject(batch) == f"Film 00 — casting, + {DIGEST_MAX_ENTRIES} more films"


def test_entries_and_a_slate_add_your_slate():
    assert (
        digest_subject(_batch(_entry("Dune: Part Three", "trailer"), slate=2))
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


def test_the_preheader_names_up_to_two_entries_after_the_lead():
    batch = _batch(
        _entry("Heat 2", "casting"),
        _entry("Dune", "trailer"),
        _entry("Ran", "announced"),
        _entry("Zodiac", "announced"),
    )

    assert digest_preheader(batch) == "Also: Dune — new trailer; Ran — announced"


def test_the_preheader_joins_the_slate_and_the_entries():
    batch = _batch(_entry("Heat 2"), _entry("Dune", "trailer"), slate=1)

    assert digest_preheader(batch) == (
        "Your slate: 1 date in the next 30 days. Also: Dune — new trailer"
    )


def test_one_entry_and_no_slate_has_nothing_to_preview():
    assert digest_preheader(_batch(_entry("Heat 2"))) == ""


# --- the batch ---------------------------------------------------------------------------


def test_every_row_counts_as_an_item_past_the_cap_too():
    entries = [_entry(f"Film {n:02}", "casting", "trailer", tmdb_id=n) for n in range(21)]
    batch = _batch(*entries)

    assert len(batch.item_ids) == 42
    assert batch.has_content


def test_the_justwatch_credit_and_the_following_line_are_the_entrys():
    plain = _entry("Heat 2", "casting")
    streaming = _entry("Zodiac", "now_available", "casting")

    assert (plain.credits_justwatch, streaming.credits_justwatch) == (False, True)
    assert plain.entity_following == ()


# --- context (DC-14) ---------------------------------------------------------------------


def test_the_context_leads_with_the_first_ranked_entry_at_the_lead_poster_size():
    """The lead card is the lead film — the entry the subject names — at `w185`; the rows
    after it keep the compact `w154`."""
    lead = _entry("Lead", "release_date", poster="/lead.jpg")
    row = _entry("Row", "casting", poster="/row.jpg")

    context = digest_context(
        _batch(*rank_entries([row, lead])), cadence="weekly", today=TODAY, settings=get_settings()
    )

    lead_card = cast(dict[str, object], context["lead"])
    rows = cast(list[dict[str, object]], context["entries"])
    assert (lead_card["title"], lead_card["poster_url"]) == (
        "Lead",
        "https://image.test/w185/lead.jpg",
    )
    assert [(e["title"], e["poster_url"]) for e in rows] == [
        ("Row", "https://image.test/w154/row.jpg")
    ]


def test_a_slate_only_context_has_no_lead():
    context = digest_context(
        _batch(slate=1), cadence="weekly", today=TODAY, settings=get_settings()
    )

    assert (context["lead"], context["entries"]) == (None, [])


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
        recipient=RECIPIENT, entries=(), unsendable=(), slate=(SlateDay(day=TODAY, items=items),)
    )

    context = digest_context(batch, cadence="daily", today=TODAY, settings=get_settings())

    (day,) = cast(list[dict[str, object]], context["slate"])
    assert [e["marker"] for e in cast(list[dict[str, object]], day["entries"])] == [None, "new"]
