"""The `resolve` pass: score every unresolved person mention and record where it went.

Runs after clustering, inside the same `link` run — clustering is what *writes* the mentions
this reads (D-20), so the two cannot be reordered, and a pass with nothing to resolve is a
no-op. It carries no run row of its own: one daily chain link stage is one row on
`/admin/runs`, and its counts ride the same detail line as the link and cluster counts.

Per mention, in order:

1. **`news.resolution_cache` first** (D-24). The same trade naming the same person on the
   same film is the commonest shape a per-film feed produces, and every miss costs a TMDB
   request. A hit skips scoring entirely.
2. Otherwise **gather** (`candidates.py`), read `/person/{id}` for the candidates whose name
   the story actually wrote (`_with_person_dates`), and **score and route** (`scoring.py`).
3. When routing lands in the narrow band and a `resolve` completer was supplied, **ask the
   closed-set tiebreak** (`tiebreak.py`, D-22) and fold its answer in. This is the only step
   that reaches a model, and only ≤10% of mentions are meant to.
4. **Persist** `person_id`, `confidence`, `path`, the features and the candidates, and stamp
   `resolved_at` — which is what takes the mention out of this pass's backlog.
5. On an accept, **write the cache** and make sure `catalog.person` holds the accepted id.

**Failures are isolated per mention and the row is left untouched.** A mention whose
`path` is still NULL is simply re-selected next run, which is the right resting state for a
TMDB blip: nothing is lost by waiting, and half-writing a decision from a failed gather would
put a wrong answer somewhere only a human re-reading `/admin/resolution` would ever catch. A
provider blip on the tiebreak call is the same class of thing and rests the same way — but a
tiebreak *answer* that arrived and could not be used is a decision, not a blip, and leaves the
deterministic route standing rather than buying another call every run (`_with_tiebreak`).

**`RESOLVE_MENTIONS_PER_RUN` bounds the pass**, because each miss is TMDB requests and the
backlog on the first run after deploy is every mention clustering has ever extracted. The
remainder is not dropped — it is the next run's backlog, oldest first.

The requests a miss costs are one `/search/person` for the name, plus one `/person/{id}` per
candidate whose name matched — the age/alive feature's only input (NEU-1400).

**The budget, since it is the reason this pass has a limit at all.** The bound is the number
of *name-matching* candidates, not the cap of ten: a candidate whose name is not the story's
scores zero whatever its dates say (the score multiplies through the name), and a shortlist is
mostly the film's own director and top billing, who are not called what the story called this
person. So the worst case is one mention's cap — ten, when TMDB returns ten people by one
name, which is also the case the feature is worth the most on — and the ordinary case is the
one or two people the name really could be. Against `RESOLVE_MENTIONS_PER_RUN` = 500 and
TMDB's 40-per-10s window that is ~500-1,000 extra requests, 2-4 minutes, on a pass that
already spends 500 searches; the theoretical ceiling of 5,000 would be ~21 minutes, on the
first run after deploy only. `catalog.person.details_observed_at` makes each request permanent
for anybody the catalog holds, so steady state is the genuinely new people a run's stories
name — tens, not hundreds.

The narrower option D-21's ticket costed — buying dates only inside the `ACCEPT_MARGIN` band —
was **not** taken, and the difference is deliberate: the band is by definition two candidates
who already tie, so it would never catch the *lone* long-dead candidate who has no rival to
tie with, and accepting that person is the more expensive failure (a wrong `person_id` on a
story, alerting the followers of somebody who died in 1998). Paying on the accepts is what
buys that.

A candidate the catalog has *never* held — the wrong namesake TMDB's search turned up — has
nowhere to stamp, and is deliberately left that way rather than given a `catalog.person` row:
that table is user-facing (person search, the onboarding grid), and filling it with people
this pass rejected would make them followable. They are re-read on a later run instead, and
`_dates` keeps it to once within one.
"""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film, Person
from upmovies.ingest.runs import record_llm_calls, record_llm_usage, record_progress
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.upsert import ensure_person_details, upsert_people
from upmovies.link.linker import story_dek
from upmovies.link.resolve.candidates import (
    CANDIDATE_CAP,
    CHANGE_STREAM_WINDOW_DAYS,
    Candidate,
    CandidateSet,
    gather_candidates,
)
from upmovies.link.resolve.scoring import (
    NAME_MATCH_NONE,
    Decision,
    Mention,
    Path,
    ScoredCandidate,
    Thresholds,
    cap_confidence,
    name_match,
    resolve_mention,
)
from upmovies.link.resolve.tiebreak import TiebreakQuestion, TiebreakReply, ask_tiebreak
from upmovies.llm.types import CallLog, Completer, StageGateway, Usage
from upmovies.news.models import PERSON_KIND, ResolutionCache, Story, StoryPerson
from upmovies.news.source_quality import domain_for_story

log = logging.getLogger(__name__)

SessionFactory = Callable[[], AsyncSession]

DEFAULT_MENTIONS_PER_RUN = 500

# Mirrors `RESOLVE_MODEL` in `config.py`, which carries the derivation. A default here rather
# than a required argument for the same reason `run_link_ingest` defaults `source_judge_model`:
# the pipelines are called by tests and by the admin re-runner as well as by `pipeline_run`,
# and a stage nobody's band reaches never spends it.
DEFAULT_RESOLVE_MODEL = "claude-sonnet-4-6"


@dataclass(frozen=True)
class PersonDates:
    """What `/person/{id}` said about one candidate's birth and death, or nothing.

    A value rather than the `catalog.person` row it usually comes from, because it is cached
    for the length of a pass and each mention works in its own session — an ORM object would
    be handed across the session it was loaded in. Both NULL means "the dates cannot say",
    which is what a failed fetch, a tombstoned id and a person TMDB holds no dates for all
    amount to as far as scoring is concerned; the pass remembers that answer too, so an
    endpoint failing does not cost one request per mention naming the same person.
    """

    birthday: date | None = None
    deathday: date | None = None


NO_DATES = PersonDates()


@dataclass
class ResolutionResult:
    """One pass's counters, one per route plus the two that are not routes.

    `cache_hits` counts mentions that took the cached answer rather than scoring, so it
    overlaps `accepted` deliberately: the two answer different questions — where mentions
    went, and how much of the pass was paid for in TMDB requests.

    The four `tiebreak_*` counters overlap the routes for the same reason, and are what D-22's
    "≤10% of mentions" is measured against: `tiebreak_asked` is how many mentions reached the
    model at all, and the three below it are what came back. A mention the model named somebody
    for stays on the `tiebreak` route — the decision was not deterministic and D-25 has a human
    read it — so `tiebreak` alone cannot say how many of those were actually decided.
    """

    accepted: int = 0
    tiebreak: int = 0
    unlinked: int = 0
    not_in_tmdb: int = 0
    cache_hits: int = 0
    failed: int = 0
    tiebreak_asked: int = 0
    tiebreak_decided: int = 0
    tiebreak_declined: int = 0
    tiebreak_rejected: int = 0
    usage: Usage = field(default_factory=Usage)

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

    def detail(self, unit: str = "mentions") -> str | None:
        """This pass's clause of the link run's detail line, or None when it had nothing to
        do — an empty backlog says nothing worth a clause of its own, and a "resolved 0"
        printed on every quiet day is one the eye stops seeing.

        `unit` names what was resolved, because the organisation arm (EF-12) is a second pass
        with the same counters on a different backlog and two "resolved N mentions" clauses on
        one line would be unreadable."""
        if not self.resolved and not self.failed:
            return None
        line = (
            f"resolved {self.resolved} {unit} "
            f"({self.accepted} accepted, {self.tiebreak} tiebreak, "
            f"{self.unlinked} unlinked, {self.not_in_tmdb} not in tmdb; "
            f"{self.cache_hits} cached, {self.failed} failed)"
        )
        # Appended rather than folded into the parenthetical above: the band is empty on most
        # runs, and a "0 asked" on every line is one the eye stops seeing — the same reason
        # the link run's saturation note is a clause it only sometimes carries.
        if self.tiebreak_asked:
            line += (
                f"; {self.tiebreak_asked} tiebreaks asked "
                f"({self.tiebreak_decided} decided, {self.tiebreak_declined} none, "
                f"{self.tiebreak_rejected} rejected)"
            )
        return line


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
    gateway: StageGateway | None = None,
    resolve_model: str = DEFAULT_RESOLVE_MODEL,
) -> ResolutionResult:
    """Resolve up to `limit` unresolved mentions, oldest first. Does not finalize the run —
    the `link` pipeline owns that row and this pass's counters are one clause of its detail
    line.

    The `gateway` is the `resolve` stage (D-22), and it is optional in the same way and for
    the same reason the whole pass is optional to `run_link_ingest`: without one the narrow
    band is written as the scoring pass routed it — `tiebreak`, nobody named — which is
    exactly the resting state D-25's queue is for. Nothing else about the pass changes, so a
    caller with no model to hand still gets every deterministic decision.

    A gateway rather than a completer because the stage is resolved **where the band is**, not
    here: most runs never ask a tiebreak, and `Gateway.for_stage` builds a client on first use,
    so a pass whose names all separated on the arithmetic opens no connection pool and needs no
    credential to have been configured for a provider it never reaches.
    """
    limits = thresholds or Thresholds()
    # One pass's worth of person dates, keyed by TMDB id. A run's mentions repeat names —
    # several stories about one casting is the shape a per-film feed produces — and the
    # candidates a name yields repeat with them, so this is what keeps a candidate the catalog
    # has no row to stamp from costing one `/person/{id}` per mention that names them.
    dates: dict[int, PersonDates] = {}
    async with session_factory() as s:
        pending = await _pending_mention_ids(s, limit=limit)

    async def resolve_one(
        session: AsyncSession, mention_id: UUID, result: ResolutionResult, calls: CallLog
    ) -> Path | None:
        return await _resolve_one(
            session,
            client,
            mention_id=mention_id,
            thresholds=limits,
            result=result,
            now=now,
            window_days=window_days,
            cap=cap,
            gateway=gateway,
            resolve_model=resolve_model,
            calls=calls,
            dates=dates,
        )

    return await run_mention_pass(
        session_factory=session_factory,
        run_id=run_id,
        pending=pending,
        resolve_one=resolve_one,
        gateway=gateway,
        resolve_model=resolve_model,
        unit="mentions",
    )


ResolveOne = Callable[[AsyncSession, UUID, ResolutionResult, CallLog], Awaitable[Path | None]]
"""What one mention's whole decision looks like to `run_mention_pass`: a session, the row to
decide, the counters to tick and the call log to record into."""


async def run_mention_pass(
    *,
    session_factory: SessionFactory,
    run_id: UUID,
    pending: Sequence[UUID],
    resolve_one: ResolveOne,
    gateway: StageGateway | None,
    resolve_model: str,
    unit: str,
    carried_usage: Usage | None = None,
) -> ResolutionResult:
    """Work one backlog of mentions, one session and one isolated failure per row.

    The loop both resolution arms share (EF-12): the person arm over `story_person` and the
    organisation arm over `story_entity` differ in what one decision *is* — a different table,
    a different catalogue, a different scorer — and in nothing about how a pass is run. Failure
    isolation, the progress heartbeat, the per-call `ingest.llm_call` rows and the one
    aggregate usage row are that second thing, and are spelled once here so the two arms cannot
    drift into reporting a run differently.

    `unit` names what is being resolved, for the log line and the run's detail clause.

    `carried_usage` is what an *earlier* pass in the same run already spent on this stage, and
    it exists because `record_llm_usage` overwrites the (run, stage) row rather than adding to
    it. The two arms share one `resolve` stage, so the second one to ask a tiebreak has to
    write the total or it would silently erase the first one's cost from `/admin/runs`. A pass
    that asks no tiebreak writes nothing and leaves whatever row is there standing, which is
    the right answer in the other three combinations.
    """
    result = ResolutionResult()
    for mention_id in pending:
        # Owned by the loop rather than by the decision, so a call that crashed the mention
        # still becomes an `ingest.llm_call` row: the `finally` below writes whatever was
        # recorded through its own session, which the failed mention's rollback cannot take
        # with it (NEU-975, the same arrangement `source_stage` uses).
        calls = CallLog()
        try:
            async with session_factory() as s:
                path = await resolve_one(s, mention_id, result, calls)
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
            log.exception("resolution failed for %s %s", unit, mention_id)
            async with session_factory() as s:
                await record_progress(s, run_id, failed_delta=1)
                await s.commit()
            result.failed += 1
        finally:
            if calls.results and gateway is not None:
                result.usage += calls.usage
                async with session_factory() as s:
                    await record_llm_calls(
                        s,
                        run_id,
                        stage="resolve",
                        provider=gateway.provider_for("resolve"),
                        model=resolve_model,
                        results=calls.results,
                    )
                    await s.commit()
    if result.tiebreak_asked and gateway is not None:
        # One aggregate row per (run, stage), written once the band is worked rather than per
        # mention: `record_llm_usage` UPSERTs, so a per-mention write would be correct and
        # would also be one round trip per ambiguous name for a number only the total means.
        # Both arms write it, and both UPSERT into the same (run, stage) row, which is what
        # makes one run's `resolve` cost one number however many backlogs paid into it.
        async with session_factory() as s:
            await record_llm_usage(
                s,
                run_id,
                stage="resolve",
                provider=gateway.provider_for("resolve"),
                model=resolve_model,
                usage=(result.usage if carried_usage is None else carried_usage + result.usage),
            )
            await s.commit()
    if result.resolved or result.failed:
        log.info("resolution: %s", result.detail(unit))
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
    gateway: StageGateway | None = None,
    resolve_model: str = DEFAULT_RESOLVE_MODEL,
    calls: CallLog | None = None,
    dates: dict[int, PersonDates] | None = None,
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
        await _with_person_dates(
            session,
            client,
            gathered=gathered,
            mention=mention,
            dates=dates if dates is not None else {},
        ),
        mention=mention,
        link_confidence=story.link_confidence,
        mentioned_tmdb_ids=await _mentioned_tmdb_ids(session, mention.title_mentioned),
        story_date=_story_date(story),
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
            calls=calls if calls is not None else CallLog(),
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


async def _with_person_dates(
    session: AsyncSession,
    client: TMDBClient,
    *,
    gathered: CandidateSet,
    mention: Mention,
    dates: dict[int, PersonDates],
) -> list[Candidate]:
    """The gathered candidates with `birthday`/`deathday` attached to the ones whose name the
    story actually wrote — the age/alive feature's input (D-21, NEU-1400).

    **Gated on the name, because the score is.** `scoring` multiplies everything by the name
    quality, so a candidate the name gate zeroes cannot be moved by any feature and its dates
    would be a request spent on an answer that changes nothing. A mention's ten candidates are
    mostly the film's own director and top billing, who are not called what the story called
    this person; what is left is the one or two people the name really could be, which is also
    the pair the feature exists to separate.

    Here rather than in `candidates.py` because it needs both halves: the gate is
    `scoring.name_match`, and that module holds no name comparison by design. This is the
    seam between them, which is what `pipeline.py` is.
    """
    out: list[Candidate] = []
    for candidate in gathered.candidates:
        if name_match(mention.name_as_written, candidate) == NAME_MATCH_NONE:
            out.append(candidate)
            continue
        found = dates.get(candidate.person_id)
        if found is None:
            found = await _read_person_dates(session, client, candidate.person_id)
            dates[candidate.person_id] = found
        if found == NO_DATES:
            # Nothing to attach, so the candidate keeps whatever the catalog already gave it:
            # "the dates cannot say" must not be able to *unsay* a stored date, which is what
            # overwriting with a failed fetch's empty answer would do.
            out.append(candidate)
            continue
        out.append(replace(candidate, birthday=found.birthday, deathday=found.deathday))
    return out


async def _read_person_dates(
    session: AsyncSession, client: TMDBClient, person_id: int
) -> PersonDates:
    """One candidate's dates, from `catalog.person` if the catalog holds them and from
    `/person/{id}` if it does not. Named apart from the sweep's own `_person_dates`, which
    answers the neighbouring question with a `catalog.person` row (`ingest.sweep.
    credit_events`).

    Two paths because only one of them has somewhere to remember the answer.
    `ensure_person_details` stamps `details_observed_at` and so is asked once *ever* for a
    person the catalog holds — which is every candidate either film-anchored source produced,
    both joining `catalog.person`, and every person a previous run accepted. A candidate only
    TMDB's name search knows has no row, and is deliberately not given one: `catalog.person`
    is read by the person search and the onboarding grid, so writing the namesakes this pass
    rejects would put people with no upcoming credits in front of users. That one is read
    straight off the client and remembered for the rest of the pass instead.

    **A TMDB failure costs the feature and nothing else.** The dates are a *weight* on a
    decision the other five features can still make, so an outage here demotes to "the dates
    cannot say" rather than failing the mention — which would leave it in the backlog to be
    re-gathered, at the price of the `/search/person` request that already succeeded. A 404 is
    `ensure_person_details`' business on the stored path (it tombstones the id) and is the same
    "cannot say" on the other.
    """
    try:
        if await session.get(Person, person_id) is None:
            details = await client.person_details(person_id)
            return PersonDates(details.birthday, details.deathday)
        person = await ensure_person_details(session, client, person_id)
        if person is None:
            return NO_DATES
        return PersonDates(person.birthday, person.deathday)
    except httpx.HTTPError:
        log.warning("person %s details unavailable; age plausibility not scored", person_id)
        return NO_DATES


def _story_date(story: Story) -> date:
    """The day the story ran, which the age feature reads its candidates against.

    `published_at` when the feed carried one, and `fetched_at` when it did not: retrieval is
    within a day or two of publication, and the feature's bar is stated in whole years, so the
    fallback is well inside the precision it is read at. Leaving it None instead would silence
    the feature on every story whose feed omitted a date, which is the one input it cannot do
    without.
    """
    return (story.published_at or story.fetched_at).astimezone(UTC).date()


async def _break_the_tie(
    session: AsyncSession,
    *,
    client: Completer,
    model: str,
    story: Story,
    row: StoryPerson,
    mention: Mention,
    decision: Decision,
    result: ResolutionResult,
    calls: CallLog,
) -> Decision:
    """Ask the `resolve` stage which of this mention's shortlist the story named (D-22).

    Called with the scored decision in hand rather than from a second pass over stored
    `tiebreak` rows, because everything the question needs is here and nowhere else: the
    shortlist the model is shown is the ranked `Candidate` objects, and `story_person.
    candidates` keeps a log of them for a human, not the known-for titles and credit facts a
    closed-set prompt is built from. A second pass would have to re-gather — one more TMDB
    request per ambiguous name — to ask a question this one can already ask for free.

    The film is read for its title: the shortlist is film-scoped end to end, and a model asked
    to choose between two people without being told what they would be choosing them *for* is
    being asked a different, harder question than the one the pipeline knows the answer to.
    """
    film = await session.get(Film, story.film_id)
    if film is None:
        raise ValueError(f"story {story.id} is linked to a film that is not in the catalog")
    result.tiebreak_asked += 1
    reply = await ask_tiebreak(
        client=client,
        model=model,
        question=TiebreakQuestion(
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
    decision: Decision,
    reply: TiebreakReply,
    *,
    link_confidence: float | None,
    model: str,
    result: ResolutionResult,
) -> Decision:
    """The scored decision, with the model's answer folded into it — three outcomes.

    **An option keeps the `tiebreak` route and gains a person.** The mention is accepted in
    every sense that matters downstream — `person_id` and `confidence` are written, and INV-6
    still caps the second against the story's link — but `path` records how it was decided,
    which is the whole of D-25: a resolution a model made inside the ambiguous band is the one
    a human is meant to be able to find and check afterwards, and spelling it `accepted` would
    bury it among the decisions arithmetic made on its own.

    **"None" is `unlinked`**, the ordinary outcome for a mention nothing could be pinned to.

    **A rejected answer changes nothing.** An out-of-list number or an unparseable reply
    leaves the deterministic decision exactly as scoring wrote it — `tiebreak`, nobody named —
    with what happened recorded in `features`. Coercing such a reply to its nearest plausible
    option is the one thing the closed set exists to forbid (`tiebreak.py`), and re-asking is
    not on offer either: the route is written, so the mention leaves the backlog and sits on
    the queue a human works rather than buying another call every night forever.

    The confidence on an accept is the **chosen** candidate's score, not the top-ranked one's:
    the model may well have picked the runner-up, and reporting the winner's number for
    somebody else's row would overstate a decision the arithmetic explicitly could not make.
    """
    asked: dict[str, Any] = {"asked": True, "model": model, "reason": reply.reason}
    if reply.option is not None:
        chosen = decision.ranked[reply.option - 1]
        result.tiebreak_decided += 1
        return replace(
            decision,
            person_id=chosen.person_id,
            confidence=cap_confidence(chosen.score, link_confidence),
            features={
                **decision.features,
                "tiebreak": {**asked, "answer": reply.option, "person_id": chosen.person_id},
            },
        )
    if reply.answered_none:
        result.tiebreak_declined += 1
        return replace(
            decision,
            path=Path.UNLINKED,
            person_id=None,
            features={**decision.features, "tiebreak": {**asked, "answer": None}},
        )
    result.tiebreak_rejected += 1
    return replace(
        decision,
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

    `kind` joined the cache's key in EF-12, and this arm pins it to `person`: the same name on
    the same film is a different question asked of `catalog.person` and of
    `catalog.production_company`, and the organisation arm's answer must not be read back here.
    """
    if domain is None:
        return None
    cached = await session.get(ResolutionCache, (domain, name_as_written, film_id, PERSON_KIND))
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
        kind=PERSON_KIND,
        person_id=decision.person_id,
        confidence=decision.confidence,
        resolved_at=resolved_at,
    )
    await session.execute(
        stmt.on_conflict_do_update(
            index_elements=["source_domain", "name_as_written", "film_id", "kind"],
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
