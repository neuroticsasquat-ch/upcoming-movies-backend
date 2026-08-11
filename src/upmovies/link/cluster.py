"""Stage 2: cluster a single film's unclustered linked stories into events and classify
them, attaching to recent existing events where they continue a beat. Idempotent — only
touches linked stories that have no event_story row yet; the unique story_id is the
backstop. The caller owns the session/commit."""

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.catalog.queries import field_changed_at
from upmovies.llm.types import CallLog, Completer, Prompt
from upmovies.news.catalog_events import CATALOG_EVENT_TYPES, ONCE_PER_FILM_EVENT_TYPES
from upmovies.news.models import Event, EventStory, Story
from upmovies.news.source_quality import (
    best_tier,
    domain_for_story,
    downgrade_confidence,
    effective_tier,
    get_source_domains,
)

log = logging.getLogger(__name__)


class ClusterParseError(Exception):
    """Raised when the cluster LLM response cannot be parsed into cluster groups."""


_VALID_TYPES = {
    "announced",
    "casting",
    "production_start",
    "production_wrap",
    "release_date",
    "trailer",
    "first_look",
    "other",
}
_STALE_EVENT_TYPES = {"announced", "casting", "production_start", "production_wrap"}
_SINGULAR_BEAT_TYPES = {"trailer", "first_look"}
_WRAPPED_STATUSES = {"Post Production", "Released"}


def is_stale_stage(
    event_type: str,
    film_status: str | None,
    release_date: date | None,
    as_of_date: date,
) -> bool:
    """A new event is 'stale-stage' when an early-or-mid production beat (casting/announced/
    production_start/production_wrap) is reported for a film that has already wrapped, released,
    or whose release date has already passed as of the run date. A film in Post Production has
    by definition already wrapped, so a fresh production_wrap event for it is stale (NEU-444,
    extending NEU-367). A film whose release_date is in the past is already out even if TMDB's
    status string lags, so early-production beats for it are re-circulated old news (NEU-449).
    Such events are dropped at clustering. NULL/unknown status with no past release date is
    never stale."""
    if event_type not in _STALE_EVENT_TYPES:
        return False
    if film_status in _WRAPPED_STATUSES:
        return True
    return release_date is not None and release_date < as_of_date


def _normalize_name(name: str) -> str:
    """Deterministic casting-identity key: NFKC-fold, casefold, collapse whitespace.
    String-based (not TMDB-person-id) — imperfect on aliases/typos, but stable and
    dependency-free, which fits breaking-cast news where TMDB credits lag."""
    folded = unicodedata.normalize("NFKC", name).casefold().strip()
    return " ".join(folded.split())


_SUMMARY_MAX = 500
_DEFAULT_MAX_TOKENS = 4096
_HEADLINES_PER_EVENT = 3

_INSTRUCTIONS = """You group a single film's news stories into distinct EVENTS — real beats \
in its life (casting, a trailer, a release-date change, production milestones, etc.) — and \
classify each. You are given the FILM, its EXISTING recent events (numbered from 1), and NEW \
stories to place (each with an integer id "n").

For each new story, either attach it to an existing event (it continues a beat already \
logged) or assign it to a new event (a beat not yet logged). Group new stories that report \
the SAME beat into ONE new event; split different beats into separate events. Five outlets \
reporting the same casting is one event. A single trailer or first-look reveal reported \
across several days by many outlets is ONE event — attach later stories to the existing \
event rather than opening a new one.

The same applies to casting: when a new story names a performer who already appears in an \
EXISTING casting event — alone or alongside others — attach it there. A fuller cast list, an \
additional outlet, or a repeat report of the same signing is a continuation of that casting \
beat, not a new one. Open a new casting event only for a genuinely different performer or role.

Classify each new event by the story's DOMINANT, headline beat — the development the coverage \
is really about. Incidental details never change the type:

- "trailer" means a promotional VIDEO that has been RELEASED for the public to watch right \
now (a trailer or teaser the audience can view today). Naming cast does not change this. \
Coverage that a trailer is merely COMING — "expected to arrive", "will drop", "teased", \
"coming soon", with no watchable video yet — is NOT a trailer; classify it as "other".
- "first_look" is any OTHER visual reveal that is NOT a released video: footage screened or \
described at an event or presentation, concept art, animated or CGI character designs, \
first-look photos, or a promotional still of an actor in costume. Naming cast does not \
change this.
- A bare role announcement with no imagery or footage is "casting".
- A release date mentioned in passing inside a casting story stays "casting".
- A "casting" event requires an actual performer's name. A story that reports a role or \
character joining without naming who plays them, or that says casting is "expected", \
"forthcoming", "yet to be announced", or otherwise teases future announcements with nobody \
confirmed, is not a casting beat — classify it "other" (or "off_topic" if it reports no fact \
about this film at all).
- For a "casting" event, list in "cast" the exact performer name(s) the story reports as \
joining THIS film — the people actually cast, not the director or characters.

Every event must be a beat in THIS film's own life. If a new story's actual subject is a \
DIFFERENT film — even a spin-off, sequel, or prequel of this film's franchise, the ORIGINAL \
or EARLIER film that this one continues (a story about "The Housemaid" is not an event for \
the tracked "The Housemaid's Secret" merely because they share a title stem or a lead actor), \
or one that names this film only as context or a scheduling comparison (e.g. "another film \
moved its date to avoid clashing with this one", or "a spin-off sets its own release date") — \
do not log it as this film's event: put it in its own group with "type": "off_topic" and \
"confidence": null so it is dropped rather than recorded.

Split only when a story genuinely reports two co-equal beats.

New events carry:
- "type": one of announced, casting, production_start, production_wrap, release_date, \
trailer, first_look, other, off_topic
- "confidence": "confirmed" if reported as fact, "rumored" if speculation/unconfirmed.
- "region": for a "release_date" event ONLY, the ISO 3166-1 alpha-2 code (e.g. "IN" for \
India, "US" for the United States) of the country the date applies to; null when the date is \
worldwide/global or no country is named. For every non-release_date event, null.

The payload includes `as_of_date`, today's date (UTC). Use it to reason about whether an \
event is recent, upcoming, or already past.

A "release_date" event requires a story announcing a NEW or CHANGED release date. A \
"release_date" event is about the FILM's own theatrical, streaming, or home-video release — \
never a tie-in or companion product (a video game, soundtrack, book, or other merchandise \
release) even when the story uses the word "release" or ties its timing to the film's release \
date. Such tie-in news, if newsworthy at all, is "other" — never "release_date". A story \
that merely restates the film's already-known release date (given as `film.release_date` in \
the payload), or lists it in a calendar / roundup context, is NOT a new release_date beat — \
put it in its own group with "type": "off_topic" and "confidence": null so it is dropped \
rather than recorded. Compare the story's claimed date against `film.release_date` in the \
payload: if the story frames the date as new, changed, or "moved" but the date given matches \
the film's already-known release_date, this is not a new beat regardless of how the headline \
frames it — put it in its own group with "type": "off_topic" and "confidence": null, same as a \
plain restatement.

For a "release_date" event, put the exact date the story asserts in "claimed_date" as \
YYYY-MM-DD (null if the story gives no concrete date). For every non-release_date event, null.

Return ONLY JSON — no prose, no markdown:
{"events": [{"existing": <existing event number or null>, "type": <type or null>, \
"confidence": "confirmed" | "rumored" | null, "region": <ISO 3166-1 alpha-2 or null>, \
"cast": [<performer name>, ...] for a casting event, else null, \
"claimed_date": <YYYY-MM-DD or null>, \
"stories": [<story number n>, ...]}]}

When "existing" is a number, attach its "stories" to that event ("type"/"confidence" may \
be null). Otherwise it is a new event and "type"/"confidence" are required. "existing" \
refers to an EXISTING event's number; "stories" lists NEW story numbers "n". Every new \
story's "n" must appear in exactly one group."""


@dataclass
class ClusterResult:
    events_created: int
    stories_clustered: int
    stories_rejected: int = 0


@dataclass
class ClusterPlan:
    film_id: UUID
    existing_event_ids: list[UUID]
    unclustered_story_ids: list[UUID]
    film_status: str | None = None
    film_release_date: date | None = None
    run_date: date | None = None


@dataclass
class ClusterGroup:
    existing: int | None
    event_type: str | None
    confidence: str | None
    story_indices: list[int]
    region: str | None = None
    claimed_date: date | None = None
    cast: list[str] | None = None


def parse_cluster_groups(raw: str, *, n_stories: int) -> list[ClusterGroup] | None:
    """Pure parse of the cluster LLM response. Returns None when the JSON is unparseable
    (the caller decides what a None means). Validates story indices are ints within
    1..n_stories and de-duplicates them *within a group*; cross-group dedup and
    type/confidence validation stay in apply_cluster_decisions."""
    try:
        data = json.loads(_extract_json_object(raw))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return []
    groups: list[ClusterGroup] = []
    for group in data.get("events", []):
        seen: set[int] = set()
        indices: list[int] = []
        for n in group.get("stories") or []:
            if not isinstance(n, int) or not (1 <= n <= n_stories) or n in seen:
                continue
            seen.add(n)
            indices.append(n)
        existing = group.get("existing")
        region_raw = group.get("region")
        region = (
            region_raw.upper()
            if isinstance(region_raw, str) and re.fullmatch(r"[A-Za-z]{2}", region_raw)
            else None
        )
        claimed_raw = group.get("claimed_date")
        claimed_date = None
        if isinstance(claimed_raw, str):
            try:
                claimed_date = date.fromisoformat(claimed_raw)
            except ValueError:
                claimed_date = None
        cast_raw = group.get("cast")
        cast = (
            [c for c in cast_raw if isinstance(c, str) and c.strip()]
            if isinstance(cast_raw, list)
            else None
        )
        groups.append(
            ClusterGroup(
                existing=existing if isinstance(existing, int) else None,
                event_type=group.get("type"),
                confidence=group.get("confidence"),
                story_indices=indices,
                region=region,
                claimed_date=claimed_date,
                cast=cast,
            )
        )
    return groups


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def assemble_cluster_payload(
    *,
    film_title: str,
    film_year: int | None,
    film_release_date: date | None,
    existing_payload: list[dict[str, Any]],
    new_payload: list[dict[str, Any]],
    run_date: date,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> Prompt:
    """Pure prompt assembly shared by build_cluster_request (production) and the
    validate_clustering harness. No DB, no LLM.

    The instructions are the stable prefix and the per-film payload is what varies; that this
    stage has never actually cached (the instructions sit under Sonnet 4.6's 2048-token floor,
    NEU-377) is now the adapter's business rather than a fact encoded here."""
    user: dict[str, Any] = {
        "as_of_date": run_date.isoformat(),
        "film": {
            "title": film_title,
            "year": film_year,
            "release_date": film_release_date.isoformat() if film_release_date else None,
        },
        "existing_events": existing_payload,
        "new_stories": new_payload,
    }
    return Prompt(stable_prefix=_INSTRUCTIONS, user=json.dumps(user), max_tokens=max_tokens)


async def build_cluster_request(
    session: AsyncSession,
    *,
    film_id: UUID,
    attach_limit: int,
    run_date: date,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
) -> tuple[Prompt, ClusterPlan] | None:
    """Read half: load unclustered stories and recent events, build the `Prompt` and a
    ClusterPlan. Returns None when there is nothing to cluster (no film or no unclustered
    stories). Makes no writes and calls no LLM."""
    film = (await session.execute(select(Film).where(Film.id == film_id))).scalar_one_or_none()
    if film is None:
        return None

    already_clustered = exists().where(EventStory.story_id == Story.id)
    unclustered = (
        (
            await session.execute(
                select(Story).where(
                    Story.film_id == film_id, Story.link_status == "linked", ~already_clustered
                )
            )
        )
        .scalars()
        .all()
    )
    if not unclustered:
        return None

    # Most-recent N events by occurred_at, age-independent (NEU-372), then reversed to
    # oldest->newest so the 1-based positional indices the model uses stay stable.
    existing_events = list(
        reversed(
            (
                await session.execute(
                    select(Event)
                    .where(Event.film_id == film_id)
                    .order_by(Event.occurred_at.desc(), Event.id.desc())
                    .limit(attach_limit)
                )
            )
            .scalars()
            .all()
        )
    )

    existing_payload = []
    for i, event in enumerate(existing_events, start=1):
        headlines = (
            (
                await session.execute(
                    select(Story.title)
                    .join(EventStory, EventStory.story_id == Story.id)
                    .where(EventStory.event_id == event.id)
                    .order_by(func.coalesce(Story.published_at, Story.fetched_at).desc())
                    .limit(_HEADLINES_PER_EVENT)
                )
            )
            .scalars()
            .all()
        )
        existing_payload.append(
            {
                "event": i,
                "type": event.event_type,
                "confidence": event.confidence,
                "headlines": list(headlines),
            }
        )

    new_payload = [
        {
            "n": i,
            "title": s.title,
            "summary": (str(s.raw.get("summary", "")) if isinstance(s.raw, dict) else "")[
                :_SUMMARY_MAX
            ],
        }
        for i, s in enumerate(unclustered, start=1)
    ]

    prompt = assemble_cluster_payload(
        film_title=film.title,
        film_year=film.release_date.year if film.release_date else None,
        film_release_date=film.release_date,
        existing_payload=existing_payload,
        new_payload=new_payload,
        run_date=run_date,
        max_tokens=max_tokens,
    )
    plan = ClusterPlan(
        film_id=film_id,
        existing_event_ids=[e.id for e in existing_events],
        unclustered_story_ids=[s.id for s in unclustered],
        film_status=film.status,
        film_release_date=film.release_date,
        run_date=run_date,
    )
    return prompt, plan


async def _load_events_in_order(session: AsyncSession, event_ids: list[UUID]) -> list[Event]:
    """Re-load events by the given IDs, preserving the supplied order for positional
    index stability (the LLM refers to events by 1-based position)."""
    if not event_ids:
        return []
    rows = (await session.execute(select(Event).where(Event.id.in_(event_ids)))).scalars().all()
    by_id = {e.id: e for e in rows}
    return [by_id[eid] for eid in event_ids if eid in by_id]


async def _recorded_cast_names(session: AsyncSession, film_id: UUID) -> set[str]:
    """All normalized performer names already represented by this film's casting events
    (dedicated query — not limited to the attach-window candidate set)."""
    rows = (
        (
            await session.execute(
                select(Event.subject_key).where(
                    Event.film_id == film_id,
                    Event.event_type == "casting",
                    Event.subject_key.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    names: set[str] = set()
    for key in rows:
        names.update(key or [])
    return names


async def _catalog_dedup_target(
    session: AsyncSession,
    *,
    film_id: UUID,
    event_type: str,
    changed_at: datetime | None,
    release_change_window_days: int,
) -> Event | None:
    """The catalog-sourced event this group would duplicate, if there is one (ADR-0014).

    ADR-0002's corroboration rule is untouched — this runs only *after* a group has cleared
    it, and only decides whether the beat forms a new event or joins the one TMDB's own
    change already produced. The sweep runs ~2h ahead of the daily chain, so catalog-first is
    the normal ordering: without this, a held story finally corroborated by change *C* would
    card *C* a second time, next to the card the sweep wrote from *C* hours earlier.

    - `release_date` — the most recent catalog event within the corroboration window either
      side of the corroborating change. The window, rather than an exact `occurred_at ==
      changed_at` match, because `field_changed_at` only ever returns the film's *latest*
      change: a date that moved twice inside `W` leaves the story corroborated by the second
      move and the card raised from the first, and an exact match would miss it and open a
      second card — the very outcome this exists to prevent.
    - `production_start` / `production_wrap` — a film enters production once, so the film's
      catalog event of that type is the target however old it is. Bounding this by a window
      would mean a trade story running a month behind TMDB's status flip opens a second card
      for the same milestone.

    Scoped to `provenance='catalog'`: two *story*-triggered events of one type on one film is
    pre-existing behaviour, governed by the LLM's own attach decision, and not this rule's to
    change. The mirror is `ingest.sweep.field_events._already_carded`, which decides whether a
    change cards at all; the two are one rule read from opposite ends and must move together.
    """
    if event_type not in CATALOG_EVENT_TYPES:
        return None
    stmt = select(Event).where(
        Event.film_id == film_id,
        Event.event_type == event_type,
        Event.provenance == "catalog",
    )
    if event_type not in ONCE_PER_FILM_EVENT_TYPES:
        if changed_at is None:
            return None
        window = timedelta(days=release_change_window_days)
        stmt = stmt.where(Event.occurred_at.between(changed_at - window, changed_at + window))
    stmt = stmt.order_by(Event.occurred_at.desc(), Event.id.desc()).limit(1)
    return (await session.execute(stmt)).scalars().first()


_LOG_FIELD_MAX = 40


def _log_field(value: object) -> str:
    """Render one decision-log value. `type` can be whatever the model put in the JSON, and
    the fields are read back by splitting on spaces — so whitespace is squashed and the
    result capped rather than allowed to break the line into unparseable pieces."""
    text = "_".join(str(value).split())[:_LOG_FIELD_MAX]
    return text or "-"


def _log_decision(
    film_id: UUID,
    *,
    llm: str,
    outcome: str,
    event_type: str | None,
    event_id: UUID | None,
    stories: int,
    note: str | None = None,
) -> None:
    """One line per group the cluster LLM returned, so the effect of the undated-film
    expansion on clustering quality can be measured rather than guessed (NEU-970).

    `llm` is what the model asked for (attach vs create); `outcome` is what the code did,
    so a deterministic dedup merge (`dedup_attach`) is never credited to the model. Rejected
    stories have their `film_id` nulled, so for those the line is the only surviving record
    of which film the drop was decided against.

    Written as the decision is made, before the caller commits. A film whose clustering then
    fails rolls back whole (`link/pipeline.py`), so a reader of these lines must discard any
    `film=` that also appears in a `clustering failed for film` line."""
    log.info(
        "cluster decision: film=%s llm=%s outcome=%s type=%s event=%s stories=%d note=%s",
        film_id,
        llm,
        outcome,
        _log_field(event_type) if event_type else "-",
        event_id or "-",
        stories,
        _log_field(note) if note else "-",
    )


async def apply_cluster_decisions(
    session: AsyncSession,
    *,
    plan: ClusterPlan,
    raw: str,
    unresolved_tier: str = "acceptable",
    dedup_days: int = 14,
    release_change_window_days: int = 14,
) -> ClusterResult:
    """Write half: re-load events/stories from the plan, parse the LLM JSON, and
    create/attach events. The caller owns the session/commit."""
    existing_events = await _load_events_in_order(session, plan.existing_event_ids)

    stories = (
        (await session.execute(select(Story).where(Story.id.in_(plan.unclustered_story_ids))))
        .scalars()
        .all()
    )
    by_id = {s.id: s for s in stories}
    story_ids = plan.unclustered_story_ids  # n (1-based) -> story_ids[n - 1]

    # Source-quality tiers for the stories in this plan (NEU-454). One query; the gate reads
    # `resolved_url or url` and skips unresolved Google redirects (neutral default).
    domain_by_sid = {
        s.id: domain_for_story(url=s.url, resolved_url=s.resolved_url) for s in stories
    }
    tier_rows = await get_source_domains(session, [d for d in domain_by_sid.values() if d])

    def _tier_for(sid: UUID) -> str:
        domain = domain_by_sid.get(sid)
        row = tier_rows.get(domain) if domain else None
        return effective_tier(
            llm_tier=row.llm_tier if row else None,
            admin_override=row.admin_override if row else "none",
            unresolved_default=unresolved_tier,
        )

    groups = parse_cluster_groups(raw, n_stories=len(story_ids))
    if groups is None:
        raise ClusterParseError(f"unparseable cluster response for film {plan.film_id}")

    now = datetime.now(UTC)
    as_of_date = plan.run_date or now.date()
    window_floor = as_of_date - timedelta(days=dedup_days)
    dedup_targets: dict[str, Event] = {}
    for ev in existing_events:
        if ev.event_type in _SINGULAR_BEAT_TYPES and ev.occurred_at.date() >= window_floor:
            prev = dedup_targets.get(ev.event_type)
            if prev is None or ev.occurred_at > prev.occurred_at:
                dedup_targets[ev.event_type] = ev
    recorded_cast = await _recorded_cast_names(session, plan.film_id)
    assigned: set[UUID] = set()
    events_created = stories_clustered = stories_rejected = 0

    def _reject_group(sids: list[UUID], *, llm: str, event_type: str | None, note: str) -> int:
        """Drop a whole group: unlink its stories with `note` and log the decision.
        Returns the number rejected. Shared by every drop path so they cannot drift."""
        _log_decision(
            plan.film_id,
            llm=llm,
            outcome="reject",
            event_type=event_type,
            event_id=None,
            stories=len(sids),
            note=note,
        )
        for sid in sids:
            story = by_id[sid]
            story.link_status = "rejected"
            story.film_id = None
            story.link_confidence = None
            story.link_note = note
            assigned.add(sid)
        return len(sids)

    for group in groups:
        group_sids: list[UUID] = []
        for n in group.story_indices:
            sid = story_ids[n - 1]
            if sid not in by_id or sid in assigned:
                continue
            group_sids.append(sid)
        # What the model asked for, recorded separately from what the code did below.
        llm_intent = "attach" if group.existing is not None else "create"
        if not group_sids:
            # An earlier group already claimed every story this one names — the model
            # over-grouped. Still one line, so the decision counts reconcile against the
            # group count instead of silently under-reporting exactly where it went wrong.
            _log_decision(
                plan.film_id,
                llm=llm_intent,
                outcome="superseded",
                event_type=group.event_type,
                event_id=None,
                stories=len(group.story_indices),
            )
            continue
        if group.existing is not None and 1 <= group.existing <= len(existing_events):
            event = existing_events[group.existing - 1]
            event.updated_at = now
            outcome = "attach"
        else:
            etype = group.event_type
            conf = group.confidence
            if etype == "off_topic":
                # Backstop for cross-film mis-attribution (NEU-453): a story whose real
                # subject is a different film reaches clustering only if LINK mis-linked it.
                # Drop it rather than record it as this film's event (mirrors is_stale_stage).
                stories_rejected += _reject_group(
                    group_sids, llm=llm_intent, event_type=etype, note="off-topic"
                )
                continue
            if etype not in _VALID_TYPES or conf not in ("confirmed", "rumored"):
                log.warning(
                    "cluster: invalid new event for film %s: type=%r confidence=%r",
                    plan.film_id,
                    etype,
                    conf,
                )
                _log_decision(
                    plan.film_id,
                    llm=llm_intent,
                    outcome="invalid",
                    event_type=etype,
                    event_id=None,
                    stories=len(group_sids),
                )
                continue
            if is_stale_stage(etype, plan.film_status, plan.film_release_date, as_of_date):
                stories_rejected += _reject_group(
                    group_sids, llm=llm_intent, event_type=etype, note=f"stale-stage:{etype}"
                )
                continue
            new_cast: list[str] | None = None
            if etype == "casting":
                names = list(
                    dict.fromkeys(
                        _normalize_name(c)
                        for c in (group.cast or [])
                        if isinstance(c, str) and _normalize_name(c)
                    )
                )
                new_cast = [n for n in names if n not in recorded_cast]
                if not new_cast:
                    stories_rejected += _reject_group(
                        group_sids, llm=llm_intent, event_type=etype, note="casting-recorded"
                    )
                    continue
            changed_at: datetime | None = None
            if etype == "release_date":
                # NEU-718: a release_date event may form only when TMDB's own primary
                # release_date actually changed within the corroboration window (a first
                # date, null -> date, counts as a change). The LLM's classification alone
                # never creates the event.
                changed_at = await field_changed_at(session, plan.film_id, "release_date")
                corroborated = (
                    changed_at is not None
                    and (as_of_date - changed_at.date()).days <= release_change_window_days
                )
                if not corroborated:
                    # TMDB has not (yet) recorded a matching change. Use the LLM's
                    # claimed_date only to triage: a story claiming a date TMDB has not
                    # caught up to is HELD — left linked + unclustered so a later run can
                    # corroborate it once TMDB updates — until it ages past the window.
                    # A plain restatement or a dateless story is dropped now.
                    if (
                        group.claimed_date is not None
                        and group.claimed_date != plan.film_release_date
                    ):
                        oldest = min(
                            (by_id[sid].published_at or by_id[sid].fetched_at) for sid in group_sids
                        )
                        if (as_of_date - oldest.date()).days <= release_change_window_days:
                            # hold: re-evaluated on a later run
                            _log_decision(
                                plan.film_id,
                                llm=llm_intent,
                                outcome="hold",
                                event_type=etype,
                                event_id=None,
                                stories=len(group_sids),
                            )
                            continue
                        note = "release-date-uncorroborated"
                    elif group.claimed_date is not None:
                        note = "release-date-restated"
                    else:
                        note = "release-date-unchanged"
                    stories_rejected += _reject_group(
                        group_sids, llm=llm_intent, event_type=etype, note=note
                    )
                    continue
            target = dedup_targets.get(etype) if etype in _SINGULAR_BEAT_TYPES else None
            if target is not None:
                event = target
                event.updated_at = now
                outcome = "dedup_attach"
            elif (
                catalog_target := await _catalog_dedup_target(
                    session,
                    film_id=plan.film_id,
                    event_type=etype,
                    changed_at=changed_at,
                    release_change_window_days=release_change_window_days,
                )
            ) is not None:
                event = catalog_target
                event.updated_at = now
                outcome = "catalog_attach"
            else:
                conf = downgrade_confidence(
                    conf, best_tier((_tier_for(sid) for sid in group_sids), default=unresolved_tier)
                )
                occurred = min(
                    (by_id[sid].published_at or by_id[sid].fetched_at) for sid in group_sids
                )
                event = Event(
                    film_id=plan.film_id,
                    event_type=etype,
                    confidence=conf,
                    occurred_at=occurred,
                    region=group.region if etype == "release_date" else None,
                    subject_key=new_cast if etype == "casting" else None,
                )
                session.add(event)
                await session.flush()
                events_created += 1
                outcome = "create"
                if etype in _SINGULAR_BEAT_TYPES:
                    dedup_targets[etype] = event
                if etype == "casting" and new_cast:
                    recorded_cast.update(new_cast)
        _log_decision(
            plan.film_id,
            llm=llm_intent,
            outcome=outcome,
            event_type=event.event_type,
            event_id=event.id,
            stories=len(group_sids),
        )
        for sid in group_sids:
            session.add(EventStory(event_id=event.id, story_id=sid))
            assigned.add(sid)
            stories_clustered += 1

    return ClusterResult(events_created, stories_clustered, stories_rejected)


async def cluster_film_events(
    session: AsyncSession,
    *,
    client: Completer,
    model: str,
    film_id: UUID,
    attach_limit: int,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    unresolved_tier: str = "acceptable",
    dedup_days: int = 14,
    release_change_window_days: int = 14,
    run_date: date,
    calls: CallLog,
) -> ClusterResult:
    """One clustering call for one film, recorded into `calls` — including the parse outcome,
    which `cluster` turns into a `ClusterParseError` the pipeline isolates per film."""
    built = await build_cluster_request(
        session,
        film_id=film_id,
        attach_limit=attach_limit,
        run_date=run_date,
        max_tokens=max_tokens,
    )
    if built is None:
        return ClusterResult(0, 0)
    prompt, plan = built
    result = await client.complete_call(model=model, prompt=prompt, calls=calls)
    try:
        clustered = await apply_cluster_decisions(
            session,
            plan=plan,
            raw=result.text,
            unresolved_tier=unresolved_tier,
            dedup_days=dedup_days,
            release_change_window_days=release_change_window_days,
        )
    except ClusterParseError:
        calls.set_parse_ok(False)
        raise
    calls.set_parse_ok(True)
    return clustered
