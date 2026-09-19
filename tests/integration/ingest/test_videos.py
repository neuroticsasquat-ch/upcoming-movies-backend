"""The video poll end to end against a mocked TMDB: which films it selects, what it records,
and when a `trailer` card comes out of it (D-35).

Two rules carry most of the weight.

**First observation is a baseline, never an event** (ADR-0014). A film admitted with three
years of trailers behind it must record all of them and say nothing; only what turns up
*after* that is news. The marker is `film.videos_observed_at`, so a film observed holding no
videos at all — the ordinary state of an in-play title — is still observed, and the teaser it
gets next month is a real beat rather than a second baseline.

**The scoped set is the provider poll's**, which already admits any followed or watchlisted
film whatever its dates say. That rule is the one that matters here: a trailer precedes a
theatrical date by months, so for videos the followed-but-unreleased film is the typical
subject rather than the exception.
"""

from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

from tests.fixtures.catalog import add_film
from tests.fixtures.tmdb import make_video, make_videos
from upmovies.app.models import Follow, WatchlistItem
from upmovies.catalog.models import Film, FilmFieldChange, FilmReleaseDate, FilmVideo
from upmovies.ingest import runs
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.videos import run_video_poll
from upmovies.news.models import Event, EventSummary

BASE_URL = "https://api.themoviedb.org/3"
TODAY = date(2026, 9, 17)
MIN_AGE = 14
MAX_AGE = 200
IN_WINDOW = TODAY - timedelta(days=60)
UPCOMING = TODAY + timedelta(days=200)
WIDE = 3

PUBLISHED = datetime(2026, 9, 1, 15, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 10, 9, 30, tzinfo=UTC)
SEEN_AT = datetime(2026, 9, 17, 6, 0, tzinfo=UTC)


@pytest.fixture
async def tmdb_client():
    async with TMDBClient(
        base_url=BASE_URL,
        api_key="test-key",
        rate_calls=100,
        rate_window=1,
        retry_max_attempts=2,
        retry_base_delay=0.01,
    ) as client:
        yield client


@pytest.fixture
async def run_id(session):
    run = await runs.create_run(session, kind="providers")
    await session.commit()
    return run


async def _add_released_film(session, tmdb_id: int, **overrides) -> Film:
    """A film past its US theatrical date — in the poll set on rule 1 alone."""
    film = await add_film(session, tmdb_id, status=overrides.pop("status", "Released"), **overrides)
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=WIDE,
            release_date=datetime.combine(IN_WINDOW, datetime.min.time(), tzinfo=UTC),
        )
    )
    await session.flush()
    return film


def _mock_videos(tmdb_id: int, videos: list[dict] | None = None):
    return respx.get(f"{BASE_URL}/movie/{tmdb_id}/videos").mock(
        return_value=httpx.Response(200, json=make_videos(tmdb_id, videos))
    )


async def _run(session_factory, tmdb_client, run_id, **overrides):
    kwargs = {
        "session_factory": session_factory,
        "client": tmdb_client,
        "run_id": run_id,
        "today": TODAY,
        "min_age_days": MIN_AGE,
        "max_age_days": MAX_AGE,
        "now": SEEN_AT,
    }
    return await run_video_poll(**{**kwargs, **overrides})


async def _videos(session, film: Film) -> list[tuple[str, str, str]]:
    rows = await session.execute(
        select(FilmVideo.site, FilmVideo.key, FilmVideo.type)
        .where(FilmVideo.film_id == film.id)
        .order_by(FilmVideo.id)
    )
    return [tuple(row) for row in rows]


async def _trailer_events(session, film: Film) -> list[Event]:
    rows = await session.execute(
        select(Event)
        .where(Event.film_id == film.id, Event.event_type == "trailer")
        .order_by(Event.occurred_at)
    )
    return list(rows.scalars())


# --- the baseline rule (ADR-0014) ----------------------------------------------


@respx.mock
async def test_a_films_first_observation_records_everything_and_cards_nothing(
    session, session_factory, tmdb_client, run_id
):
    """The rule the whole feature rests on. Admitting a catalogue of films that each already
    have trailers must not empty three years of promo onto the feed on day one."""
    film = await _add_released_film(session, 100)
    await session.commit()
    _mock_videos(100, [make_video("aaa"), make_video("bbb", type="Teaser")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.polled, result.recorded, result.baselined, result.cards) == (1, 2, 1, 0)
    assert await _videos(session, film) == [
        ("youtube", "aaa", "Trailer"),
        ("youtube", "bbb", "Teaser"),
    ]
    assert await _trailer_events(session, film) == []


@respx.mock
async def test_a_trailer_arriving_after_the_baseline_cards_once(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 101)
    await session.commit()
    _mock_videos(101, [make_video("aaa")])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(
        101, [make_video("aaa"), make_video("bbb", published_at="2026-09-10T09:30:00.000Z")]
    )

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.baselined, result.cards) == (1, 0, 1)
    (event,) = await _trailer_events(session, film)
    assert event.event_type == "trailer"
    assert event.confidence == "confirmed"
    assert event.provenance == "catalog"
    assert event.region is None
    assert event.subject_key == ["youtube:bbb"]


@respx.mock
async def test_the_card_is_dated_to_when_the_trailer_went_up_not_to_the_poll(
    session, session_factory, tmdb_client, run_id
):
    """A pass catching up after an outage still dates each card to the video that produced it,
    which is what makes `occurred_at` worth reading on the film page (NEU-1204)."""
    film = await _add_released_film(session, 102)
    await session.commit()
    _mock_videos(102, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(102, [make_video("aaa")])

    await _run(session_factory, tmdb_client, run_id)

    (event,) = await _trailer_events(session, film)
    assert event.occurred_at == PUBLISHED


@respx.mock
async def test_a_film_observed_holding_nothing_is_still_observed(
    session, session_factory, tmdb_client, run_id
):
    """The reason the marker is a column and not "does this film have rows". An in-play film
    normally has no videos at all on its first read, and a rows-based test would re-baseline it
    every poll and swallow the first teaser it ever gets — the beat the poll exists to catch."""
    film = await _add_released_film(session, 103)
    await session.commit()
    _mock_videos(103, [])

    first = await _run(session_factory, tmdb_client, run_id)

    assert (first.recorded, first.baselined, first.cards) == (0, 1, 0)
    await session.refresh(film)
    assert film.videos_observed_at is not None

    respx.reset()
    _mock_videos(103, [make_video("aaa")])
    second = await _run(session_factory, tmdb_client, run_id)

    assert (second.recorded, second.baselined, second.cards) == (1, 0, 1)


@respx.mock
async def test_re_polling_an_unchanged_film_writes_nothing(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 104)
    await session.commit()
    _mock_videos(104, [make_video("aaa")])
    await _run(session_factory, tmdb_client, run_id)

    second = await _run(session_factory, tmdb_client, run_id)
    third = await _run(session_factory, tmdb_client, run_id)

    assert (second.recorded, second.cards) == (0, 0)
    assert (third.recorded, third.cards) == (0, 0)
    assert len(await _videos(session, film)) == 1


@respx.mock
async def test_observing_a_films_videos_is_not_a_film_field_change(
    session, session_factory, tmdb_client, run_id
):
    """`videos_observed_at` is ingest bookkeeping, so it belongs in the denylist: a history row
    for it would card as a public event and revive the film in `dormant_film_clause` on the day
    it was polled."""
    film = await _add_released_film(session, 105)
    await session.commit()
    _mock_videos(105, [make_video("aaa")])

    await _run(session_factory, tmdb_client, run_id)

    changes = (
        (await session.execute(select(FilmFieldChange).where(FilmFieldChange.film_id == film.id)))
        .scalars()
        .all()
    )
    assert changes == []


# --- what cards and what does not (D-35) ---------------------------------------


@respx.mock
async def test_a_new_teaser_is_recorded_and_stays_silent(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 110)
    await session.commit()
    _mock_videos(110, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(110, [make_video("tease", type="Teaser")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards) == (1, 0)
    assert await _trailer_events(session, film) == []


@respx.mock
async def test_a_trailer_somewhere_other_than_youtube_is_recorded_and_stays_silent(
    session, session_factory, tmdb_client, run_id
):
    """The card carries one key the film page embeds as a YouTube player (NEU-1386); a Vimeo
    key in that field renders an empty box."""
    film = await _add_released_film(session, 111)
    await session.commit()
    _mock_videos(111, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(111, [make_video("vim", site="Vimeo")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards) == (1, 0)
    assert await _trailer_events(session, film) == []


@respx.mock
async def test_a_trailer_tmdb_holds_no_publication_time_for_does_not_card(
    session, session_factory, tmdb_client, run_id
):
    """`occurred_at` *is* `published_at`, and dating an undated video to the poll would put a
    years-old trailer on today's feed. It is still recorded, so it never cards later either."""
    film = await _add_released_film(session, 112)
    await session.commit()
    _mock_videos(112, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(112, [make_video("aaa", published_at="")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards) == (1, 0)
    assert await _videos(session, film) == [("youtube", "aaa", "Trailer")]
    assert await _trailer_events(session, film) == []


@respx.mock
async def test_two_trailers_arriving_together_card_once_each(
    session, session_factory, tmdb_client, run_id
):
    """One card per (film, video key) — not one per observation. Two distinct trailers are two
    beats, and they carry different keys so the film page can embed each."""
    film = await _add_released_film(session, 113)
    await session.commit()
    _mock_videos(113, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(
        113,
        [make_video("aaa"), make_video("bbb", published_at="2026-09-10T09:30:00.000Z")],
    )

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.cards == 2
    events = await _trailer_events(session, film)
    assert [e.subject_key for e in events] == [["youtube:aaa"], ["youtube:bbb"]]
    assert [e.occurred_at for e in events] == [PUBLISHED, LATER]


@respx.mock
async def test_two_trailers_published_in_the_same_second_both_card(
    session, session_factory, tmdb_client, run_id
):
    """One card per (film, video key) still holds when two keys share a timestamp.

    `uq_event_catalog_change` is unique on (film, type, occurred_at) for catalog events, so
    the second insert would raise and roll back the film's ledger rows with it. The clash is
    broken by a microsecond rather than by dropping the video, which would lose the beat for
    good — the ledger row is written either way, so it is never reconsidered."""
    film = await _add_released_film(session, 114)
    await session.commit()
    _mock_videos(114, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(114, [make_video("bbb"), make_video("aaa")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards, result.failures) == (2, 2, 0)
    events = await _trailer_events(session, film)
    assert [e.subject_key for e in events] == [["youtube:aaa"], ["youtube:bbb"]]
    assert [e.occurred_at for e in events] == [PUBLISHED, PUBLISHED + timedelta(microseconds=1)]


@respx.mock
async def test_a_site_casing_change_does_not_record_or_card_a_known_video_again(
    session, session_factory, tmdb_client, run_id
):
    """`uq_film_video` is keyed on the stored `site`, so the ledger's key and the key the
    payload is deduplicated on have to be the same string — otherwise a payload saying
    `youtube` where yesterday's said `YouTube` inserts a second row for a video already
    recorded and cards that same YouTube key again."""
    film = await _add_released_film(session, 117)
    await session.commit()
    _mock_videos(117, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(117, [make_video("aaa")])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(117, [make_video("aaa", site="youtube")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards) == (0, 0)
    assert len(await _videos(session, film)) == 1
    assert len(await _trailer_events(session, film)) == 1


@respx.mock
async def test_a_second_trailer_at_an_already_carded_second_is_nudged_past_it(
    session, session_factory, tmdb_client, run_id
):
    """The clash may straddle two polls, so the batch alone cannot break it — and the earlier
    card may itself already sit at a nudged timestamp, which is why the lookup is a span
    rather than an exact-match list."""
    film = await _add_released_film(session, 115)
    await session.commit()
    _mock_videos(115, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(115, [make_video("aaa")])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(115, [make_video("aaa"), make_video("bbb")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.recorded, result.cards, result.failures) == (1, 1, 0)
    events = await _trailer_events(session, film)
    assert [e.occurred_at for e in events] == [PUBLISHED, PUBLISHED + timedelta(microseconds=1)]


@respx.mock
async def test_the_card_carries_a_deterministic_summary(
    session, session_factory, tmdb_client, run_id
):
    """Every read path inner-joins `EventSummary`, so an event written without one is invisible
    on the feed and the film page alike."""
    film = await _add_released_film(session, 116)
    await session.commit()
    _mock_videos(116, [])
    await _run(session_factory, tmdb_client, run_id)
    respx.reset()
    _mock_videos(116, [make_video("aaa")])

    await _run(session_factory, tmdb_client, run_id)

    (event,) = await _trailer_events(session, film)
    summary = await session.get(EventSummary, event.id)
    assert summary is not None
    assert summary.summary == "A new trailer is out."
    assert summary.model == "deterministic"


# --- the scoped set (D-27, borrowed by D-35) -----------------------------------


@respx.mock
async def test_an_in_play_followed_film_is_polled(
    session, session_factory, tmdb_client, run_id, make_user
):
    """D-35's "plus in-play films with a follow". A trailer precedes a theatrical date by
    months, so a film nowhere near the provider window is exactly the one worth watching — and
    the poll set's follow rule has no date bound, which is what delivers it."""
    user = await make_user(email="follower@example.com")
    film = await add_film(session, 120, status="Post Production")
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=WIDE,
            release_date=datetime.combine(UPCOMING, datetime.min.time(), tzinfo=UTC),
        )
    )
    session.add(
        Follow(user_id=user.id, entity_type="title", entity_id=str(film.id), source="manual")
    )
    await session.commit()
    _mock_videos(120, [])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_an_in_play_watchlisted_film_is_polled(
    session, session_factory, tmdb_client, run_id, make_user
):
    user = await make_user(email="watcher@example.com")
    film = await add_film(session, 121, status="In Production")
    session.add(WatchlistItem(user_id=user.id, film_id=film.id, source="manual"))
    await session.commit()
    _mock_videos(121, [])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.selected, result.polled) == (1, 1)


@respx.mock
async def test_an_unfollowed_in_play_film_is_not_polled(
    session, session_factory, tmdb_client, run_id
):
    """Nobody is waiting on it and its date is nowhere near the window, so it costs nothing."""
    film = await add_film(session, 122, status="In Production")
    session.add(
        FilmReleaseDate(
            film_id=film.id,
            iso_3166_1="US",
            release_type=WIDE,
            release_date=datetime.combine(UPCOMING, datetime.min.time(), tzinfo=UTC),
        )
    )
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0


@respx.mock
async def test_a_tombstoned_film_is_not_polled(session, session_factory, tmdb_client, run_id):
    """The provider pass runs first over the same set and tombstones what TMDB has deleted, so
    the video pass must not spend a request re-asking."""
    film = await _add_released_film(session, 123)
    film.tmdb_missing_at = datetime.now(UTC)
    await session.commit()

    result = await _run(session_factory, tmdb_client, run_id)

    assert result.selected == 0


# --- failure handling ----------------------------------------------------------


@respx.mock
async def test_a_404_tombstones_the_film_rather_than_counting_a_failure(
    session, session_factory, tmdb_client, run_id
):
    film = await _add_released_film(session, 130)
    await session.commit()
    respx.get(f"{BASE_URL}/movie/130/videos").mock(return_value=httpx.Response(404))

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.missing, result.failures, result.polled) == (1, 0, 0)
    await session.refresh(film)
    assert film.tmdb_missing_at is not None


@respx.mock
async def test_one_films_outage_does_not_cost_the_rest_of_the_pass(
    session, session_factory, tmdb_client, run_id
):
    first = await _add_released_film(session, 131)
    second = await _add_released_film(session, 132)
    await session.commit()
    failing, working = sorted((first, second), key=lambda f: f.id)
    respx.get(f"{BASE_URL}/movie/{failing.tmdb_id}/videos").mock(return_value=httpx.Response(500))
    _mock_videos(working.tmdb_id, [make_video("aaa")])

    result = await _run(session_factory, tmdb_client, run_id)

    assert (result.polled, result.failures) == (1, 1)
    assert len(await _videos(session, working)) == 1


@respx.mock
async def test_a_sustained_outage_aborts_the_pass(session, session_factory, tmdb_client, run_id):
    for tmdb_id in (140, 141, 142):
        await _add_released_film(session, tmdb_id)
    await session.commit()
    for tmdb_id in (140, 141, 142):
        respx.get(f"{BASE_URL}/movie/{tmdb_id}/videos").mock(return_value=httpx.Response(500))

    result = await _run(session_factory, tmdb_client, run_id, failure_threshold=2)

    assert result.aborted is True
    assert result.failures == 2
    assert "2 consecutive failures" in (result.abort_error or "")
