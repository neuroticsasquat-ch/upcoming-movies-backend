"""The `resolve` pass: score every unresolved person mention and record where it went.

Runs after clustering, inside the same `link` run — clustering is what *writes* the mentions
this reads (D-20), so the two cannot be reordered, and a pass with nothing to resolve is a
no-op. It carries no run row of its own: one daily chain link stage is one row on
`/admin/runs`, and its counts ride the same detail line as the link and cluster counts.

Per mention, in order:

1. **`news.resolution_cache` first** (D-24). The same trade naming the same person on the
   same film is the commonest shape a per-film feed produces, and every miss costs a TMDB
   request. A hit skips scoring entirely.
2. Otherwise **gather** (`candidates.py`) and **score and route** (`scoring.py`).
3. **Persist** `person_id`, `confidence`, `path`, the features and the candidates, and stamp
   `resolved_at` — which is what takes the mention out of this pass's backlog.
4. On an accept, **write the cache** and make sure `catalog.person` holds the accepted id.

**Failures are isolated per mention and the row is left untouched.** A mention whose
`path` is still NULL is simply re-selected next run, which is the right resting state for a
TMDB blip: nothing is lost by waiting, and half-writing a decision from a failed gather would
put a wrong answer somewhere only a human re-reading `/admin/resolution` would ever catch.

**`RESOLVE_MENTIONS_PER_RUN` bounds the pass**, because each miss is one `/search/person`
request and the backlog on the first run after deploy is every mention clustering has ever
extracted. The remainder is not dropped — it is the next run's backlog, oldest first.
"""

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, Person
from upmovies.ingest.runs import record_progress
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.upsert import upsert_people
from upmovies.link.resolve.candidates import (
    CANDIDATE_CAP,
    CHANGE_STREAM_WINDOW_DAYS,
    gather_candidates,
)
from upmovies.link.resolve.scoring import (
    Decision,
    Mention,
    Path,
    ScoredCandidate,
    Thresholds,
    cap_confidence,
    resolve_mention,
)
from upmovies.news.models import ResolutionCache, Story, StoryPerson
from upmovies.news.source_quality import domain_for_story

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

DEFAULT_MENTIONS_PER_RUN = 500


@dataclass
class ResolutionResult:
    """One pass's counters, one per route plus the two that are not routes.

    `cache_hits` counts mentions that took the cached answer rather than scoring, so it
    overlaps `accepted` deliberately: the two answer different questions — where mentions
    went, and how much of the pass was paid for in TMDB requests.
    """

    accepted: int = 0
    tiebreak: int = 0
    unlinked: int = 0
    not_in_tmdb: int = 0
    cache_hits: int = 0
    failed: int = 0

    @property
    def resolved(self) -> int:
        return self.accepted + self.tiebreak + self.unlinked + self.not_in_tmdb

    def record(self, path: Path) -> None:
        match path:
            case Path.ACCEPTED:
                self.accepted += 1
            case Path.TIEBREAK:
                self.tiebreak += 1
            case Path.UNLINKED:
                self.unlinked += 1
            case Path.NOT_IN_TMDB:
                self.not_in_tmdb += 1

    def detail(self) -> str | None:
        """This pass's clause of the link run's detail line, or None when it had nothing to
        do — an empty backlog says nothing worth a clause of its own, and a "resolved 0"
        printed on every quiet day is one the eye stops seeing."""
        if not self.resolved and not self.failed:
            return None
        return (
            f"resolved {self.resolved} mentions "
            f"({self.accepted} accepted, {self.tiebreak} tiebreak, "
            f"{self.unlinked} unlinked, {self.not_in_tmdb} not in tmdb; "
            f"{self.cache_hits} cached, {self.failed} failed)"
        )


async def run_resolution(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    thresholds: Thresholds | None = None,
    limit: int = DEFAULT_MENTIONS_PER_RUN,
    now: datetime | None = None,
    window_days: int = CHANGE_STREAM_WINDOW_DAYS,
    cap: int = CANDIDATE_CAP,
) -> ResolutionResult:
    """Resolve up to `limit` unresolved mentions, oldest first. Does not finalize the run —
    the `link` pipeline owns that row and this pass's counters are one clause of its detail
    line."""
    limits = thresholds or Thresholds()
    result = ResolutionResult()
    async with session_factory() as s:
        pending = await _pending_mention_ids(s, limit=limit)
    for mention_id in pending:
        try:
            async with session_factory() as s:
                path = await _resolve_one(
                    s,
                    client,
                    mention_id=mention_id,
                    thresholds=limits,
                    result=result,
                    now=now,
                    window_days=window_days,
                    cap=cap,
                )
                # The link run's counters are already a whole-run total across units — the
                # link stage counts stories, the cluster stage films (`link/pipeline.py`) —
                # so a third unit changes nothing about how they are read, and the guard
                # that matters reads its own in-memory counts. What this is really for is
                # the heartbeat: `record_progress` ticks `last_progress_at`, and a pass that
                # can spend several hundred TMDB requests without one would look to
                # `mark_stale_runs_cancelled` exactly like a run orphaned by a crash.
                await record_progress(s, run_id, processed_delta=1)
                await s.commit()
            if path is not None:
                result.record(path)
        except Exception:
            log.exception("resolution failed for mention %s", mention_id)
            async with session_factory() as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            result.failed += 1
    if result.resolved or result.failed:
        log.info("resolution: %s", result.detail())
    return result


async def _pending_mention_ids(session: AsyncSession, *, limit: int) -> Sequence[UUID]:
    """The unresolved mentions this pass may work, oldest first.

    `path IS NULL` is the backlog marker rather than `person_id IS NULL`: three of the four
    routes end with no person, and re-resolving an `unlinked` mention every run forever would
    spend the whole budget on the names that are hardest to resolve. Joined to the story
    because resolution is film-scoped end to end — candidates come from the linked film and
    the cache is keyed by it — and a mention whose story was later rejected has had its
    `film_id` nulled, so there is nothing left to resolve it against.
    """
    stmt = (
        select(StoryPerson.id)
        .join(Story, Story.id == StoryPerson.story_id)
        .where(
            StoryPerson.path.is_(None),
            Story.link_status == "linked",
            Story.film_id.is_not(None),
        )
        .order_by(StoryPerson.created_at, StoryPerson.id)
        .limit(limit)
    )
    return (await session.execute(stmt)).scalars().all()


async def _resolve_one(
    session: AsyncSession,
    client: TMDBClient,
    *,
    mention_id: UUID,
    thresholds: Thresholds,
    result: ResolutionResult,
    now: datetime | None,
    window_days: int,
    cap: int,
) -> Path | None:
    """Resolve one mention and write its decision. Returns the route taken, or None when the
    mention is no longer resolvable — its story was rejected, or another pass got there
    first — which is not a failure and is counted as nothing."""
    row = await session.get(StoryPerson, mention_id)
    if row is None or row.path is not None:
        return None
    story = await session.get(Story, row.story_id)
    if story is None or story.film_id is None or story.link_status != "linked":
        return None

    domain = domain_for_story(url=story.url, resolved_url=story.resolved_url)
    cached = await _cached_decision(
        session,
        domain=domain,
        name_as_written=row.name_as_written,
        film_id=story.film_id,
        link_confidence=story.link_confidence,
    )
    if cached is not None:
        result.cache_hits += 1
        _write_decision(row, cached, now=now)
        return cached.path

    mention = _mention_from(row)
    gathered = await gather_candidates(
        session,
        client,
        film_id=story.film_id,
        name_as_written=row.name_as_written,
        now=now,
        window_days=window_days,
        cap=cap,
    )
    decision = resolve_mention(
        gathered.candidates,
        mention=mention,
        link_confidence=story.link_confidence,
        mentioned_tmdb_ids=await _mentioned_tmdb_ids(session, mention.title_mentioned),
        search_empty=not gathered.search_hits,
        thresholds=thresholds,
    )
    if decision.person_id is not None:
        # Before the row that references it: `story_person.person_id` is an FK into
        # `catalog.person`, and the candidate TMDB's name search just found is exactly the
        # one the catalog may never have held. `upsert_people` is the same write every other
        # path uses, which is what keeps them agreeing on `tmdb_missing_at` (NEU-1361).
        hit = gathered.hit_for(decision.person_id)
        if hit is not None:
            await upsert_people(session, [hit])
        elif await session.get(Person, decision.person_id) is None:
            raise ValueError(f"accepted person {decision.person_id} is not in catalog.person")
    _write_decision(row, decision, now=now)
    if decision.accepted and domain is not None:
        await _cache_decision(
            session,
            domain=domain,
            name_as_written=row.name_as_written,
            film_id=story.film_id,
            decision=decision,
            now=now,
        )
    return decision.path


def _mention_from(row: StoryPerson) -> Mention:
    """The scorer's view of a stored mention. `title_mentioned` and `event_type` come out of
    `features`, where the extraction pass put them for want of columns of their own."""
    features = row.features or {}
    return Mention(
        name_as_written=row.name_as_written,
        role=row.role,
        department=row.department,
        title_mentioned=features.get("title_mentioned"),
        event_type=features.get("event_type"),
    )


def _write_decision(row: StoryPerson, decision: Decision, *, now: datetime | None) -> None:
    """Persist one decision onto the mention row.

    `features` is **merged, never replaced**: `title_mentioned` and `event_type` are the only
    record of what the extraction pass saw, written once at clustering and never regenerated,
    and this pass's own scoring inputs include them. Everything computed here goes under a
    single `resolution` key so the two halves stay told apart by shape and not by memory.
    """
    row.person_id = decision.person_id
    row.confidence = decision.confidence
    row.path = decision.path.value
    row.features = {**(row.features or {}), "resolution": decision.features}
    if decision.ranked:
        row.candidates = [_candidate_log(scored) for scored in decision.ranked]
    row.resolved_at = now or datetime.now(UTC)


def _candidate_log(scored: ScoredCandidate) -> dict:
    """One candidate as `story_person.candidates` records it: who they are, what they scored,
    and every feature behind it. D-25's `/admin/resolution` page reads exactly this, which is
    why the whole shortlist is kept and not only the winner — an unlinked mention's value to
    a human is the near-misses it *rejected*."""
    return {
        "person_id": scored.person_id,
        "name": scored.candidate.name,
        "score": scored.score,
        "features": scored.features,
    }


async def _cached_decision(
    session: AsyncSession,
    *,
    domain: str | None,
    name_as_written: str,
    film_id: UUID,
    link_confidence: float | None,
) -> Decision | None:
    """The cached answer for this (domain, name, film), as a `Decision` the write path cannot
    tell from a freshly scored one.

    No domain, no cache: an unresolved Google-News redirect has no publisher to key on
    (`domain_for_story`), and keying those on `google.com` would pool every outlet's house
    style for a name into one entry. Such a mention is scored every run, which is the cost of
    not knowing who published it.

    A cached row with no `person_id` is a cached *negative* (INV-8, D-24) and is as much an
    answer as a hit. This pass writes none — it caches accepts only — but D-24 blesses the
    shape and the resolve stage may, so reading one back as `not_in_tmdb` costs a line and
    keeps a future write from arriving as a crash.
    """
    if domain is None:
        return None
    cached = await session.get(ResolutionCache, (domain, name_as_written, film_id))
    if cached is None:
        return None
    return Decision(
        path=Path.ACCEPTED if cached.person_id is not None else Path.NOT_IN_TMDB,
        person_id=cached.person_id,
        # Re-capped against *this* story rather than trusted from the cached row: INV-6
        # bounds a resolution by the link it rests on, and the story that filled the cache is
        # not the story being resolved now. A weaker link means a weaker answer, cache or no.
        confidence=cap_confidence(cached.confidence, link_confidence),
        ranked=[],
        features={
            "cache_hit": True,
            "source_domain": domain,
            "cached_at": _iso(cached.resolved_at),
            "cached_confidence": cached.confidence,
        },
    )


async def _cache_decision(
    session: AsyncSession,
    *,
    domain: str,
    name_as_written: str,
    film_id: UUID,
    decision: Decision,
    now: datetime | None,
) -> None:
    """Remember an accept for this (domain, name, film) — an upsert, because the same trade
    naming the same person on the same film is exactly what the cache is for and a second
    story about it must refresh the entry rather than raise."""
    resolved_at = now or datetime.now(UTC)
    stmt = pg_insert(ResolutionCache).values(
        source_domain=domain,
        name_as_written=name_as_written,
        film_id=film_id,
        person_id=decision.person_id,
        confidence=decision.confidence,
        resolved_at=resolved_at,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["source_domain", "name_as_written", "film_id"],
            set_={
                "person_id": stmt.excluded.person_id,
                "confidence": stmt.excluded.confidence,
                "resolved_at": stmt.excluded.resolved_at,
            },
        )
    )


async def _mentioned_tmdb_ids(session: AsyncSession, title: str | None) -> frozenset[int]:
    """TMDB ids for the other title the article named alongside this person (D-21).

    Case-insensitive equality against the catalog's own two title columns, which is a
    deliberately narrow match: this feeds a *bonus*, so a title the catalog spells differently
    costs the candidate a corroboration it never had rather than mis-crediting one. The
    catalog is the upcoming-film spine, so a person's back catalogue is mostly absent from it
    either way — the overlap this finds is with another upcoming title, which is the one an
    article naming two projects is usually drawing.
    """
    if not title or not title.strip():
        return frozenset()
    needle = title.strip().casefold()
    stmt = select(Film.tmdb_id).where(
        or_(func.lower(Film.title) == needle, func.lower(Film.original_title) == needle)
    )
    return frozenset((await session.execute(stmt)).scalars().all())


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
