"""The video poll: what a film has to watch, read once a day for the films anyone could
plausibly be waiting on, and the `trailer` card a new one raises (D-35).

**A second phase of the providers run, not a run of its own.** `/movie/{id}/videos` shares
`/movie/{id}/watch/providers`' working set, so the two passes want exactly the same selection
query and the same daily slot; giving videos its own `ingest_run.kind` would open a second run
row that says the same thing about the same films, and a second Coolify slot to forget to
create. They stay two passes rather than one loop because a TMDB outage on one endpoint must
not cost the other its whole pass — the abort guards are per phase, the way the sweep's are.

**The scoped set is `providers.load_poll_set`, unchanged**, and it already carries D-35's
"plus films somebody is waiting on": that set's second rule is the computed watchlist asked of
every user at once (`follow_queries.covered_by_any_user_clause`, D-1414.3), which has no
release-date floor, so a followed film two years from release is in it. This matters more here
than it does for providers — a trailer precedes a theatrical date by months, so for videos the
followed-but-unreleased film is the *typical* subject rather than the exception, and the poll
would be pointless without it.

M8 widened that rule to reach a film followed only through a **person, company or franchise**,
which the previous note said it could not. That was the D-13 derivation's limitation, and there
is no derivation any more: a person follow covers the director-or-top-3 credits its `coverage`
names (or every seed-grade credit at `all`), a company follow its films, a franchise follow its
collection. So a followed director's next film is polled for its trailer now, which is the beat
this poll exists to catch. What keeps that from costing a back catalogue is the alert window
(`catalog.queries.alert_window_clause`) bounding the three indirect branches, and `lead` being
the default coverage — and it is bounded in the same shape for the provider poll beside it,
because the two passes share the one selection query on purpose.

**First observation is a baseline, never an event** (ADR-0014). The marker is
`film.videos_observed_at`, not "does this film have ledger rows": the ordinary first read of an
in-play film returns no videos at all, so a rows-based test would re-baseline it on every poll
and silently swallow the teaser it eventually gets — the one beat the poll exists to catch.

**Insert-only ledger, no snapshot.** `catalog.film_video` records every video TMDB lists, of
every type and site, and never deletes: a trailer pulled from YouTube and restored must not
read as new. Only what a poll *actually inserted* is a candidate to card, read from the
insert's own `RETURNING`, so there is no read-then-write race to lose a video to.

**One card per (film, video key)**, carrying that key in `Event.subject_key` and dated
`occurred_at = published_at` — when the trailer went up, not when we noticed. Only YouTube
videos of type `Trailer` card; teasers, clips and featurettes are recorded and stay silent.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from uuid import UUID

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, FilmVideo
from upmovies.ingest.providers import load_poll_set
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.phase import AbortGuard, Heartbeat, owned_session
from upmovies.ingest.sweep.seeds import SessionFactory
from upmovies.ingest.tmdb.client import TMDBClient, TMDBNotFound
from upmovies.ingest.tmdb.schemas import TMDBVideos
from upmovies.ingest.tmdb.upsert import mark_film_missing
from upmovies.news.catalog_events import (
    TRAILER_EVENT_TYPE,
    TRAILER_SITE,
    TRAILER_VIDEO_TYPE,
    video_subject_key,
)
from upmovies.news.models import Event
from upmovies.synthesize.deterministic import TrailerReleased, write_deterministic_summary

log = logging.getLogger(__name__)

# How far apart two trailers published in the same second are pushed so both can card, and how
# far past the newest candidate to look for timestamps an earlier poll already moved. A second
# is the whole span a tie-break can ever cover, because TMDB's `published_at` has second
# precision: no film gets a million trailers in one second.
_TIE_BREAK_STEP = timedelta(microseconds=1)
_TIE_BREAK_SPAN = timedelta(seconds=1)


@dataclass(frozen=True)
class Video:
    """One video TMDB lists for a film, cut down to what the ledger stores."""

    site: str
    key: str
    type: str
    name: str
    published_at: datetime | None


@dataclass
class VideosResult:
    """What one video poll selected, read and wrote."""

    selected: int = 0
    polled: int = 0
    videos: int = 0
    """Videos observed across every film — the size of what TMDB listed, not of what we kept."""
    recorded: int = 0
    """Rows newly inserted into the ledger. High on a film's baseline read and near zero after
    it, which is why it is reported apart from `videos`."""
    baselined: int = 0
    """Films this pass held a first observation of. Every one of them recorded rows and carded
    nothing (ADR-0014), so it is the number that explains a `recorded` with no `cards`."""
    cards: int = 0
    """`trailer` events raised (D-35). One per (film, video key)."""
    missing: int = 0
    """Films TMDB answered 404 for, and this pass tombstoned — apart from `failures` because a
    failure is a reason to worry about TMDB and a missing film is a reason to stop asking."""
    failures: int = 0
    aborted: bool = False
    abort_error: str | None = None


def videos_from_payload(payload: TMDBVideos) -> list[Video]:
    """Flatten a `/movie/{id}/videos` response into the ledger's rows, in TMDB's order.

    **`site` is case-folded here, once**, so that the key this function deduplicates on and the
    key `uq_film_video` enforces are the same string. They have to be: `site` is free text on
    TMDB's side, so a payload that says `youtube` where yesterday's said `YouTube` would insert
    a second ledger row for a video already recorded — and, since `is_trailer` matches case
    insensitively, card that same YouTube key a second time. Folding at this one boundary means
    every downstream comparison (the dedup below, the insert, its `RETURNING`, `is_trailer`)
    reads the same value. Nothing renders `site`, so nothing is lost by storing it folded.

    **Deduplicated on `(site, key)`, keeping the first sighting**, because that pair is the
    ledger's natural key: TMDB does carry the same YouTube key twice under two of its own video
    ids, and two rows with one key would raise a unique violation, fail the film and spend the
    abort budget on a payload that was never ambiguous. Deduplicating here rather than at the
    insert keeps one definition of "a video".

    `type` is *not* folded — it is stored for information rather than compared as a key, and
    TMDB's own casing is the more useful thing to keep.

    Nothing is filtered. The type cut belongs to `is_trailer` and applies to *carding* only —
    the ledger stores every video, so a teaser an editor relabels `Trailer` next month is
    already known and does not card as new.
    """
    deduplicated: dict[tuple[str, str], Video] = {}
    for entry in payload.results:
        video = Video(
            site=entry.site.lower(),
            key=entry.key,
            type=entry.type,
            name=entry.name,
            published_at=entry.published_at,
        )
        deduplicated.setdefault((video.site, video.key), video)
    return list(deduplicated.values())


def is_trailer(video: Video) -> bool:
    """Whether this video is the beat D-35 cards: a YouTube video of type `Trailer`.

    `site` arrives already folded from `videos_from_payload` — that is where the ledger's key
    is normalised — so only `type` is folded here. Both are free text on TMDB's side, and a
    card missed over letter case would be a silent failure: the poll records the video, so it
    is never re-examined and the beat is lost for good rather than raised late.
    """
    return video.site == TRAILER_SITE and video.type.lower() == TRAILER_VIDEO_TYPE


async def _videos_observed(session: AsyncSession, film_id: UUID) -> bool:
    """Whether the catalog has ever held an observation of this film's videos.

    Read from `film.videos_observed_at` and not from the presence of ledger rows — see the
    module docstring, and `Film.videos_observed_at`'s own. Must be called *before* this poll's
    insert, which is the only reason it is a function rather than a subquery.
    """
    observed_at = (
        await session.execute(select(Film.videos_observed_at).where(Film.id == film_id))
    ).scalar_one_or_none()
    return observed_at is not None


async def _mark_videos_observed(session: AsyncSession, film_id: UUID) -> None:
    """Record that the catalog has now seen this film's videos, if it had not already.

    Write-once, guarded on the column rather than on the caller remembering the order — the
    same contract as `mark_credits_observed`, and for the same reason: the marker's whole job
    is to be the thing the baseline rule cannot lose. Caller commits.
    """
    await session.execute(
        update(Film)
        .where(Film.id == film_id, Film.videos_observed_at.is_(None))
        .values(videos_observed_at=func.now())
    )


async def _insert_videos(
    session: AsyncSession, *, film_id: UUID, videos: list[Video], now: datetime
) -> list[Video]:
    """Insert the videos this film has not been observed with before; return the ones that were
    new, in the order they were listed. Caller commits.

    `ON CONFLICT DO NOTHING` over (film, site, key) is the whole insert-only rule, and
    `RETURNING` yields only the rows the statement actually inserted — so what comes back is
    exactly the set that may card, with no read-then-write race to lose a video to.
    """
    if not videos:
        return []
    stmt = insert(FilmVideo).values(
        [
            {
                "film_id": film_id,
                "site": v.site,
                "key": v.key,
                "type": v.type,
                "name": v.name,
                "published_at": v.published_at,
                "observed_at": now,
            }
            for v in videos
        ]
    )
    rows = await session.execute(
        stmt.on_conflict_do_nothing(index_elements=["film_id", "site", "key"]).returning(
            FilmVideo.site, FilmVideo.key
        )
    )
    inserted = set(rows.all())
    return [v for v in videos if (v.site, v.key) in inserted]


@dataclass(frozen=True)
class TrailerCard:
    """A trailer this poll owes a card: the YouTube key it carries and the moment it dates to.

    `occurred_at` is a plain `datetime` and not `datetime | None` — that is the point of the
    type. `Event.occurred_at` is not nullable, so the undated-video cut has to happen before
    the card is described rather than inside the writer.
    """

    key: str
    occurred_at: datetime


async def _cardable_trailers(
    session: AsyncSession, *, film_id: UUID, new_videos: list[Video]
) -> list[TrailerCard]:
    """The newly recorded videos that owe a card, oldest publication first.

    Two cuts and one adjustment:

    - **YouTube trailers only** (`is_trailer`). Everything else is recorded and silent.
    - **A publication time is required.** `occurred_at` *is* `published_at` (D-35), and TMDB
      omits it on older rows. A video with none cannot be dated, and dating it to the poll
      would put a years-old trailer on today's feed. It stays in the ledger, so it is not
      re-examined on the next pass either — deliberately: an undated video is not news.
    - **Same-second ties are nudged, not dropped.** `uq_event_catalog_change` is unique on
      (film, type, occurred_at) for catalog events, so two trailers published in the same
      second — a duplicate upload, in practice — would raise on the second insert, roll back
      the film's whole transaction including its ledger rows, and fail identically on every
      later poll. The rule the ticket asks for is *one card per (film, video key)*, so
      dropping the second would under-deliver it, and permanently: the video is recorded, so
      it is never reconsidered. Each clash therefore advances by a microsecond until the
      timestamp is free. TMDB's `published_at` has second precision and nothing renders below
      a minute, so the nudge is a tie-break rather than a claim — and it preserves publication
      order, which dropping does not.

    The timestamps already spoken for are read from this film's existing catalog trailer cards
    over the candidates' own span *plus a second*, not from an exact-match list: a card carded
    at a nudged timestamp on an earlier poll has to be visible here, or the clash it was moved
    out of would be walked straight back into.
    """
    candidates = sorted(
        (
            TrailerCard(key=v.key, occurred_at=v.published_at)
            for v in new_videos
            if is_trailer(v) and v.published_at is not None
        ),
        key=lambda c: (c.occurred_at, c.key),
    )
    if not candidates:
        return []
    taken = set(
        (
            await session.execute(
                select(Event.occurred_at).where(
                    Event.film_id == film_id,
                    Event.event_type == TRAILER_EVENT_TYPE,
                    Event.provenance == "catalog",
                    Event.occurred_at.between(
                        candidates[0].occurred_at,
                        candidates[-1].occurred_at + _TIE_BREAK_SPAN,
                    ),
                )
            )
        ).scalars()
    )
    cardable: list[TrailerCard] = []
    for candidate in candidates:
        occurred_at = candidate.occurred_at
        while occurred_at in taken:
            occurred_at += _TIE_BREAK_STEP
        if occurred_at != candidate.occurred_at:
            log.info(
                "videos: film %s already cards a trailer at %s; %s carded at %s instead",
                film_id,
                candidate.occurred_at,
                candidate.key,
                occurred_at,
            )
        taken.add(occurred_at)
        cardable.append(TrailerCard(key=candidate.key, occurred_at=occurred_at))
    return cardable


async def _card_trailer(session: AsyncSession, *, film_id: UUID, trailer: TrailerCard) -> None:
    """Raise the one `trailer` event a newly recorded trailer is owed (D-35). Caller commits.

    The event and its summary are written together, so an event never reaches the feed without
    the summary row every read path inner-joins.
    """
    event = Event(
        film_id=film_id,
        event_type=TRAILER_EVENT_TYPE,
        # A trailer is not a claim awaiting corroboration — the video is on YouTube and anyone
        # can watch it — so it is `confirmed`, the same standing a status change gets (ADR-0014
        # as refined by NEU-1081), and it is on the push whitelist (D-32) on those terms.
        confidence="confirmed",
        provenance="catalog",
        # When the trailer went up, not when the poll ran: a pass catching up after an outage
        # still dates each card to the video that produced it.
        occurred_at=trailer.occurred_at,
        # No region: a trailer is on YouTube, which has no theatrical market. `region_visible()`
        # admits a NULL region, so the card is visible on every timeline.
        region=None,
        subject_key=video_subject_key(trailer.key),
    )
    session.add(event)
    await session.flush()
    await write_deterministic_summary(
        session,
        event_id=event.id,
        change=TrailerReleased(),
        source_updated_at=event.updated_at,
    )


async def run_video_poll(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    min_age_days: int,
    max_age_days: int,
    excluded_statuses: frozenset[str],
    now: datetime | None = None,
    failure_threshold: int = 10,
    log_every: int = 250,
) -> VideosResult:
    """Read `/movie/{id}/videos` for every film in the scoped set, one at a time."""
    result = VideosResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)
    heartbeat = Heartbeat(session_factory, run_id)

    async with owned_session(session_factory) as s:
        targets = await load_poll_set(
            s,
            today=today,
            min_age_days=min_age_days,
            max_age_days=max_age_days,
            excluded_statuses=excluded_statuses,
        )
    result.selected = len(targets)
    log.info("videos: %d films due", result.selected)

    for i, target in enumerate(targets, start=1):
        await heartbeat.tick()
        try:
            payload = await client.movie_videos(target.tmdb_id)
            videos = videos_from_payload(payload)
            # Stamped per film rather than once for the pass, for `providers`' reason:
            # `observed_at` has to say when this film was read, not when the run began. `now`
            # pins it for tests.
            seen_at = now if now is not None else datetime.now(UTC)
            async with owned_session(session_factory) as s:
                observed_before = await _videos_observed(s, target.film_id)
                new_videos = await _insert_videos(
                    s, film_id=target.film_id, videos=videos, now=seen_at
                )
                await _mark_videos_observed(s, target.film_id)
                cardable = (
                    await _cardable_trailers(s, film_id=target.film_id, new_videos=new_videos)
                    if observed_before
                    else []
                )
                for trailer in cardable:
                    await _card_trailer(s, film_id=target.film_id, trailer=trailer)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.polled += 1
            result.videos += len(videos)
            result.recorded += len(new_videos)
            result.cards += len(cardable)
            result.baselined += 0 if observed_before else 1
            guard.succeeded()
            if i % log_every == 0:
                log.info("videos: %d/%d films", i, len(targets))
            continue
        except TMDBNotFound:
            # Terminal, not an outage — tombstoned rather than retried, and it touches `guard`
            # in neither direction, for the reasons `refresh_phase` gives at the same call.
            # Rarely reached in practice: the provider poll runs first over the same set and
            # tombstones there, and `load_poll_set` drops a tombstoned film.
            async with owned_session(session_factory) as s:
                await mark_film_missing(s, target.tmdb_id)
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            result.missing += 1
            log.info("videos: film %d is gone from TMDB (404); tombstoned", target.tmdb_id)
            continue
        except httpx.HTTPError as e:
            log.warning("polling videos for film %d failed: %s", target.tmdb_id, e)
        except Exception:
            # One malformed payload must not cost the rest of the poll.
            log.exception("unexpected error polling videos for film %d", target.tmdb_id)
        result.failures += 1
        if await guard.failed():
            result.aborted = True
            result.abort_error = f"aborted after {guard.consecutive} consecutive failures"
            log.error("videos: %s", result.abort_error)
            break

    log.info(
        "videos: %d polled, %d videos, %d recorded, %d baselined, %d carded, %d missing, %d failed",
        result.polled,
        result.videos,
        result.recorded,
        result.baselined,
        result.cards,
        result.missing,
        result.failures,
    )
    return result


def videos_detail(result: VideosResult) -> str:
    """The videos phase's clause of the run's `ingest_run.detail` line.

    `baselined` earns its place beside `recorded`: on a cold catalogue the two are nearly
    equal and `carded` is zero, which is the baseline rule working rather than a poll that
    lost its cards — and without the number on the line there is no way to read that from
    the outside.
    """
    line = (
        f"videos: {result.polled}/{result.selected} polled, "
        f"{result.videos} videos, {result.recorded} recorded, "
        f"{result.baselined} baselined, {result.cards} carded, "
        f"{result.missing} missing, {result.failures} failed"
    )
    if result.aborted:
        line += f"; videos aborted: {result.abort_error}"
    return line
