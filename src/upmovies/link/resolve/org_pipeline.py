"""The organisation half of the `resolve` pass: score every unresolved studio and franchise
mention and record where it went (EF-12).

`pipeline.py` for `news.story_entity`. It runs in the same link run, immediately after the
person arm, over a backlog the same cluster stage wrote — and it shares that module's loop
(`run_mention_pass`), its counters (`ResolutionResult`), its routing vocabulary (`Path`), its
two thresholds and its INV-6 cap. What is its own is what one decision *is*.

Per mention, in order:

1. **`news.resolution_cache` first** (D-24), keyed by `(domain, name, film, kind)`. A trade
   naming the same studio on the same film twice is at least as common as it naming the same
   person twice, and every miss costs a TMDB request.
2. Otherwise **gather** (`org_candidates.py`) and **score and route** (`org_scoring.py`). One
   request per mention — `/search/company` or `/search/collection` — and no per-candidate
   request at all, which is the one place this arm is cheaper than the person arm: there is no
   organisation equivalent of `/person/{id}`'s dates to buy.
3. When routing lands in the narrow band and a `resolve` gateway was supplied, **ask the
   closed-set tiebreak** (`tiebreak.ask_org_tiebreak`, D-22) and fold its answer in.
4. **Persist** `entity_id`, `confidence`, `path`, the features and the candidates, and stamp
   `resolved_at` — which is what takes the mention out of this pass's backlog.
5. On an accept, **write the cache** and make sure the catalog holds the accepted id.

**Failures are isolated per mention and the row is left untouched**, on `pipeline.py`'s terms
in full: a `path` still NULL is re-selected next run, which is the right resting state for a
TMDB blip, and a tiebreak *answer* that arrived and could not be used leaves the deterministic
route standing rather than buying another call every run.

**This pass does not card anything and does not stamp anything.** A resolved mention is a fact
about a story, and turning it into an alert a studio's followers receive — or into the stamp
that stops a catalog change raising a second card — is EF-13's first-association builder
(NEU-1446), which reads these rows. Until it lands the rows accumulate and are visible on
`/admin/resolution`, which is the intended resting state for the two tickets deploying
together.
"""

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.upsert import upsert_organisation
from upmovies.link.linker import story_dek
from upmovies.link.resolve.org_candidates import ORG_CANDIDATE_CAP, gather_org_candidates
from upmovies.link.resolve.org_scoring import (
    OrgDecision,
    OrgMention,
    org_candidate_log,
    resolve_org_mention,
)
from upmovies.link.resolve.pipeline import (
    DEFAULT_MENTIONS_PER_RUN,
    DEFAULT_RESOLVE_MODEL,
    ResolutionResult,
    SessionFactory,
    run_mention_pass,
)
from upmovies.link.resolve.scoring import Path, Thresholds, cap_confidence
from upmovies.link.resolve.tiebreak import OrgTiebreakQuestion, TiebreakReply, ask_org_tiebreak
from upmovies.llm.types import CallLog, Completer, StageGateway, Usage
from upmovies.news.models import ResolutionCache, Story, StoryEntity
from upmovies.news.source_quality import domain_for_story

log = logging.getLogger(__name__)


async def run_org_resolution(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    thresholds: Thresholds | None = None,
    limit: int = DEFAULT_MENTIONS_PER_RUN,
    now: datetime | None = None,
    cap: int = ORG_CANDIDATE_CAP,
    gateway: StageGateway | None = None,
    resolve_model: str = DEFAULT_RESOLVE_MODEL,
    carried_usage: Usage | None = None,
) -> ResolutionResult:
    """Resolve up to `limit` unresolved organisation mentions, oldest first.

    `run_resolution`'s contract throughout, including the optional `gateway`: without one the
    narrow band is written as the scorer routed it — `tiebreak`, nobody named — which is the
    resting state D-25's queue is for.

    `limit` is the person pass's `RESOLVE_MENTIONS_PER_RUN`, deliberately reused rather than
    given a setting of its own. It bounds TMDB requests, and a run's organisation backlog is a
    fraction of its person backlog: a story names a handful of people and at most one or two
    studios. A second knob would be a second thing to tune with no evidence to tune it on.

    `carried_usage` is the person pass's spend on this stage — see `run_mention_pass`.
    """
    limits = thresholds or Thresholds()
    async with session_factory() as s:
        pending = await _pending_organisation_ids(s, limit=limit)

    async def resolve_one(
        session: AsyncSession, mention_id: UUID, result: ResolutionResult, calls: CallLog
    ) -> Path | None:
        return await _resolve_one_organisation(
            session,
            client,
            mention_id=mention_id,
            thresholds=limits,
            result=result,
            now=now,
            cap=cap,
            gateway=gateway,
            resolve_model=resolve_model,
            calls=calls,
        )

    return await run_mention_pass(
        session_factory=session_factory,
        run_id=run_id,
        pending=pending,
        resolve_one=resolve_one,
        gateway=gateway,
        resolve_model=resolve_model,
        unit="organisations",
        carried_usage=carried_usage,
    )


async def _pending_organisation_ids(session: AsyncSession, *, limit: int) -> list[UUID]:
    """The unresolved organisation mentions this pass may work, oldest first.

    `_pending_mention_ids`' rule exactly: `path IS NULL` is the backlog marker rather than
    `entity_id IS NULL`, because three of the four routes end with no id and re-resolving an
    `unlinked` mention every run forever would spend the whole budget on the names that are
    hardest to resolve. Joined to the story because resolution is film-scoped end to end.
    """
    stmt = (
        select(StoryEntity.id)
        .join(Story, Story.id == StoryEntity.story_id)
        .where(
            StoryEntity.path.is_(None),
            Story.link_status == "linked",
            Story.film_id.is_not(None),
        )
        .order_by(StoryEntity.created_at, StoryEntity.id)
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def _resolve_one_organisation(
    session: AsyncSession,
    client: TMDBClient,
    *,
    mention_id: UUID,
    thresholds: Thresholds,
    result: ResolutionResult,
    now: datetime | None,
    cap: int,
    gateway: StageGateway | None,
    resolve_model: str,
    calls: CallLog,
) -> Path | None:
    """Resolve one organisation mention and write its decision. Returns the route taken, or
    None when the mention is no longer resolvable — its story was rejected, or another pass
    got there first — which is not a failure and is counted as nothing."""
    row = await session.get(StoryEntity, mention_id)
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
        kind=row.kind,
        link_confidence=story.link_confidence,
    )
    if cached is not None:
        result.cache_hits += 1
        _write_decision(row, cached, now=now)
        return cached.path

    mention = _mention_from(row)
    gathered = await gather_org_candidates(
        session,
        client,
        kind=row.kind,
        film_id=story.film_id,
        name_as_written=row.name_as_written,
        cap=cap,
    )
    decision = resolve_org_mention(
        gathered.candidates,
        mention=mention,
        link_confidence=story.link_confidence,
        search_empty=not gathered.search_hits,
        thresholds=thresholds,
    )
    if decision.path is Path.TIEBREAK and gateway is not None:
        decision = await _break_the_tie(
            session,
            client=gateway.for_stage("resolve"),
            model=resolve_model,
            story=story,
            row=row,
            mention=mention,
            decision=decision,
            result=result,
            calls=calls,
        )
    if decision.entity_id is not None:
        # Before the row that references it, as on the person side — except that
        # `story_entity.entity_id` carries no FK, so this is about the *reader* rather than
        # about the write succeeding: NEU-1446 joins these ids to `catalog.production_company`
        # and `catalog.collection`, and an accepted id with no row there resolves to a card
        # nobody can be shown.
        hit = gathered.hit_for(decision.entity_id)
        if hit is not None:
            await upsert_organisation(session, hit)
    _write_decision(row, decision, now=now)
    if decision.accepted and domain is not None:
        await _cache_decision(
            session,
            domain=domain,
            name_as_written=row.name_as_written,
            film_id=story.film_id,
            kind=row.kind,
            decision=decision,
            now=now,
        )
    return decision.path


async def _break_the_tie(
    session: AsyncSession,
    *,
    client: Completer,
    model: str,
    story: Story,
    row: StoryEntity,
    mention: OrgMention,
    decision: OrgDecision,
    result: ResolutionResult,
    calls: CallLog,
) -> OrgDecision:
    """Ask the `resolve` stage which of this mention's shortlist the story named (D-22).

    `pipeline._break_the_tie` in full, including reading the film for its title: a model asked
    to choose between two studios without being told what they would be choosing them *for* is
    being asked a harder question than the one the pipeline knows the answer to.
    """
    film = await session.get(Film, story.film_id)
    if film is None:
        raise ValueError(f"story {story.id} is linked to a film that is not in the catalog")
    result.tiebreak_asked += 1
    reply = await ask_org_tiebreak(
        client=client,
        model=model,
        question=OrgTiebreakQuestion(
            film_title=film.title,
            film_year=film.release_date.year if film.release_date else None,
            story_title=story.title,
            story_text=story_dek(story),
            mention=mention,
            evidence_span=row.evidence_span,
        ),
        options=[scored.candidate for scored in decision.ranked],
        calls=calls,
    )
    return _with_tiebreak(
        decision, reply, link_confidence=story.link_confidence, model=model, result=result
    )


def _with_tiebreak(
    decision: OrgDecision,
    reply: TiebreakReply,
    *,
    link_confidence: float | None,
    model: str,
    result: ResolutionResult,
) -> OrgDecision:
    """The scored decision with the model's answer folded in — `pipeline._with_tiebreak`'s
    three outcomes, unchanged and for the same reasons.

    An option keeps the `tiebreak` route and gains an id, so D-25's reviewer can still find the
    decisions a model made inside the ambiguous band. "None" is `unlinked`. A rejected answer
    changes nothing, with what happened recorded in `features`.
    """
    asked: dict = {"asked": True, "model": model, "reason": reply.reason}
    if reply.option is not None:
        chosen = decision.ranked[reply.option - 1]
        result.tiebreak_decided += 1
        return OrgDecision(
            path=decision.path,
            entity_id=chosen.entity_id,
            # The chosen candidate's score, not the top-ranked one's: the model may well have
            # picked the runner-up, and reporting the winner's number for somebody else's row
            # would overstate a decision the arithmetic explicitly could not make.
            confidence=cap_confidence(chosen.score, link_confidence),
            ranked=decision.ranked,
            features={
                **decision.features,
                "tiebreak": {**asked, "answer": reply.option, "entity_id": chosen.entity_id},
            },
        )
    if reply.answered_none:
        result.tiebreak_declined += 1
        return OrgDecision(
            path=Path.UNLINKED,
            entity_id=None,
            confidence=decision.confidence,
            ranked=decision.ranked,
            features={**decision.features, "tiebreak": {**asked, "answer": None}},
        )
    result.tiebreak_rejected += 1
    return OrgDecision(
        path=decision.path,
        entity_id=decision.entity_id,
        confidence=decision.confidence,
        ranked=decision.ranked,
        features={
            **decision.features,
            "tiebreak": {
                **asked,
                "answer": None,
                "out_of_list": reply.out_of_list,
                "unparseable": reply.unparseable,
            },
        },
    )


def _mention_from(row: StoryEntity) -> OrgMention:
    """The scorer's view of a stored mention. `title_mentioned` and `event_type` come out of
    `features`, where the extraction pass put them for want of columns of their own."""
    features = row.features or {}
    return OrgMention(
        name_as_written=row.name_as_written,
        kind=row.kind,
        title_mentioned=features.get("title_mentioned"),
        event_type=features.get("event_type"),
    )


def _write_decision(row: StoryEntity, decision: OrgDecision, *, now: datetime | None) -> None:
    """Persist one decision onto the mention row.

    `features` is **merged, never replaced**: `title_mentioned` and `event_type` are the only
    record of what the extraction pass saw, written once at clustering and never regenerated,
    and NEU-1446's first-association predicate reads `event_type` out of exactly this column.
    Everything computed here goes under a single `resolution` key so the two halves stay told
    apart by shape and not by memory.
    """
    row.entity_id = decision.entity_id
    row.confidence = decision.confidence
    row.path = decision.path.value
    row.features = {**(row.features or {}), "resolution": decision.features}
    if decision.ranked:
        row.candidates = [org_candidate_log(scored) for scored in decision.ranked]
    row.resolved_at = now or datetime.now(UTC)


async def _cached_decision(
    session: AsyncSession,
    *,
    domain: str | None,
    name_as_written: str,
    film_id: UUID,
    kind: str,
    link_confidence: float | None,
) -> OrgDecision | None:
    """The cached answer for this (domain, name, film, kind), as an `OrgDecision` the write
    path cannot tell from a freshly scored one.

    No domain, no cache: an unresolved Google-News redirect has no publisher to key on, and
    keying those on `google.com` would pool every outlet's house style for a name into one
    entry. `kind` is in the key because "Blumhouse" the studio and "Blumhouse" the franchise
    are two questions with two answers, and one entry could only hold one of them.

    A cached row with no id is a cached *negative* (INV-8, D-24) and is as much an answer as a
    hit. This pass writes none — it caches accepts only — but the shape is blessed and reading
    one back as `not_in_tmdb` keeps a future write from arriving as a crash.
    """
    if domain is None:
        return None
    cached = await session.get(ResolutionCache, (domain, name_as_written, film_id, kind))
    if cached is None:
        return None
    return OrgDecision(
        path=Path.ACCEPTED if cached.entity_id is not None else Path.NOT_IN_TMDB,
        entity_id=cached.entity_id,
        # Re-capped against *this* story rather than trusted from the cached row: INV-6 bounds
        # a resolution by the link it rests on, and the story that filled the cache is not the
        # story being resolved now. A weaker link means a weaker answer, cache or no.
        confidence=cap_confidence(cached.confidence, link_confidence),
        ranked=[],
        features={
            "kind": kind,
            "cache_hit": True,
            "source_domain": domain,
            "cached_at": cached.resolved_at.isoformat(),
            "cached_confidence": cached.confidence,
        },
    )


async def _cache_decision(
    session: AsyncSession,
    *,
    domain: str,
    name_as_written: str,
    film_id: UUID,
    kind: str,
    decision: OrgDecision,
    now: datetime | None,
) -> None:
    """Remember an accept for this (domain, name, film, kind) — an upsert, because the same
    trade naming the same studio on the same film is exactly what the cache is for and a
    second story about it must refresh the entry rather than raise."""
    resolved_at = now or datetime.now(UTC)
    stmt = pg_insert(ResolutionCache).values(
        source_domain=domain,
        name_as_written=name_as_written,
        film_id=film_id,
        kind=kind,
        entity_id=decision.entity_id,
        confidence=decision.confidence,
        resolved_at=resolved_at,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["source_domain", "name_as_written", "film_id", "kind"],
            set_={
                "entity_id": stmt.excluded.entity_id,
                "confidence": stmt.excluded.confidence,
                "resolved_at": stmt.excluded.resolved_at,
            },
        )
    )


__all__ = ["run_org_resolution"]
