"""Stage 2: cluster a single film's unclustered linked stories into events and classify
them, attaching to recent existing events where they continue a beat. Idempotent — only
touches linked stories that have no event_story row yet; the unique story_id is the
backstop. The caller owns the session/commit."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.link.linker import Completer
from upmovies.llm.client import BatchRequest, Usage
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
reporting the same casting is one event.

Classify each new event by the story's DOMINANT, headline beat — the development the coverage \
is really about. Incidental details never change the type:

- "trailer" means a promotional VIDEO that has been RELEASED for the public to watch (a \
trailer or teaser the audience can view now). Naming cast does not change this.
- "first_look" is any OTHER visual reveal that is NOT a released video: footage screened or \
described at an event or presentation, concept art, animated or CGI character designs, \
first-look photos, or a promotional still of an actor in costume. Naming cast does not \
change this.
- A bare role announcement with no imagery or footage is "casting".
- A release date mentioned in passing inside a casting story stays "casting".

Every event must be a beat in THIS film's own life. If a new story's actual subject is a \
DIFFERENT film — even one that names this film only as context or a scheduling comparison \
(e.g. "another film moved its date to avoid clashing with this one") — do not log it as this \
film's event: put it in its own group with "type": "off_topic" and "confidence": null so it \
is dropped rather than recorded.

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

A "release_date" event requires a story announcing a NEW or CHANGED release date. A story \
that merely restates the film's already-known release date (given as `film.release_date` in \
the payload), or lists it in a calendar / roundup context, is NOT a new release_date beat — \
put it in its own group with "type": "off_topic" and "confidence": null so it is dropped \
rather than recorded.

Return ONLY JSON — no prose, no markdown:
{"events": [{"existing": <existing event number or null>, "type": <type or null>, \
"confidence": "confirmed" | "rumored" | null, "region": <ISO 3166-1 alpha-2 or null>, \
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
        groups.append(
            ClusterGroup(
                existing=existing if isinstance(existing, int) else None,
                event_type=group.get("type"),
                confidence=group.get("confidence"),
                story_indices=indices,
                region=region,
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Pure prompt assembly shared by build_cluster_request (production) and the
    validate_clustering harness. No DB, no LLM. NEU-377: plain system block (cluster
    instructions are below Sonnet's 2048-token cache floor, so no cache_control)."""
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
    system = [{"type": "text", "text": _INSTRUCTIONS}]
    messages = [{"role": "user", "content": json.dumps(user)}]
    return system, messages


async def build_cluster_request(
    session: AsyncSession,
    *,
    film_id: UUID,
    attach_limit: int,
    run_date: date,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], ClusterPlan] | None:
    """Read half: load unclustered stories and recent events, build the system + messages
    payload and a ClusterPlan. Returns None when there is nothing to cluster (no film or
    no unclustered stories). Makes no writes and calls no LLM."""
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

    system, messages = assemble_cluster_payload(
        film_title=film.title,
        film_year=film.release_date.year if film.release_date else None,
        film_release_date=film.release_date,
        existing_payload=existing_payload,
        new_payload=new_payload,
        run_date=run_date,
    )
    plan = ClusterPlan(
        film_id=film_id,
        existing_event_ids=[e.id for e in existing_events],
        unclustered_story_ids=[s.id for s in unclustered],
        film_status=film.status,
        film_release_date=film.release_date,
        run_date=run_date,
    )
    return system, messages, plan


async def _load_events_in_order(session: AsyncSession, event_ids: list[UUID]) -> list[Event]:
    """Re-load events by the given IDs, preserving the supplied order for positional
    index stability (the LLM refers to events by 1-based position)."""
    if not event_ids:
        return []
    rows = (await session.execute(select(Event).where(Event.id.in_(event_ids)))).scalars().all()
    by_id = {e.id: e for e in rows}
    return [by_id[eid] for eid in event_ids if eid in by_id]


async def apply_cluster_decisions(
    session: AsyncSession,
    *,
    plan: ClusterPlan,
    raw: str,
    unresolved_tier: str = "acceptable",
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
    assigned: set[UUID] = set()
    events_created = stories_clustered = stories_rejected = 0

    for group in groups:
        group_sids: list[UUID] = []
        for n in group.story_indices:
            sid = story_ids[n - 1]
            if sid not in by_id or sid in assigned:
                continue
            group_sids.append(sid)
        if not group_sids:
            continue
        if group.existing is not None and 1 <= group.existing <= len(existing_events):
            event = existing_events[group.existing - 1]
            event.updated_at = now
        else:
            etype = group.event_type
            conf = group.confidence
            if etype == "off_topic":
                # Backstop for cross-film mis-attribution (NEU-453): a story whose real
                # subject is a different film reaches clustering only if LINK mis-linked it.
                # Drop it rather than record it as this film's event (mirrors is_stale_stage).
                for sid in group_sids:
                    story = by_id[sid]
                    story.link_status = "rejected"
                    story.film_id = None
                    story.link_confidence = None
                    story.link_note = "off-topic"
                    assigned.add(sid)
                    stories_rejected += 1
                continue
            if etype not in _VALID_TYPES or conf not in ("confirmed", "rumored"):
                log.warning(
                    "cluster: invalid new event for film %s: type=%r confidence=%r",
                    plan.film_id,
                    etype,
                    conf,
                )
                continue
            if is_stale_stage(etype, plan.film_status, plan.film_release_date, as_of_date):
                for sid in group_sids:
                    story = by_id[sid]
                    story.link_status = "rejected"
                    story.film_id = None
                    story.link_confidence = None
                    story.link_note = f"stale-stage:{etype}"
                    assigned.add(sid)
                    stories_rejected += 1
                continue
            conf = downgrade_confidence(
                conf, best_tier((_tier_for(sid) for sid in group_sids), default=unresolved_tier)
            )
            occurred = min((by_id[sid].published_at or by_id[sid].fetched_at) for sid in group_sids)
            event = Event(
                film_id=plan.film_id,
                event_type=etype,
                confidence=conf,
                occurred_at=occurred,
                region=group.region if etype == "release_date" else None,
            )
            session.add(event)
            await session.flush()
            events_created += 1
        for sid in group_sids:
            session.add(EventStory(event_id=event.id, story_id=sid))
            assigned.add(sid)
            stories_clustered += 1

    return ClusterResult(events_created, stories_clustered, stories_rejected)


async def build_cluster_batch_request(
    session: AsyncSession,
    *,
    custom_id: str,
    model: str,
    film_id: UUID,
    attach_limit: int,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    run_date: date,
) -> tuple[BatchRequest, ClusterPlan] | None:
    """Wrap build_cluster_request into a BatchRequest ready for the Anthropic Batch API."""
    built = await build_cluster_request(
        session, film_id=film_id, attach_limit=attach_limit, run_date=run_date
    )
    if built is None:
        return None
    system, messages, plan = built
    return (
        BatchRequest(
            custom_id=custom_id,
            model=model,
            system=system,
            messages=messages,
            max_tokens=max_tokens,
        ),
        plan,
    )


async def cluster_film_events(
    session: AsyncSession,
    *,
    client: Completer,
    model: str,
    film_id: UUID,
    attach_limit: int,
    max_tokens: int = _DEFAULT_MAX_TOKENS,
    unresolved_tier: str = "acceptable",
    run_date: date,
) -> tuple[ClusterResult, Usage]:
    built = await build_cluster_request(
        session, film_id=film_id, attach_limit=attach_limit, run_date=run_date
    )
    if built is None:
        return ClusterResult(0, 0), Usage()
    system, messages, plan = built
    raw, usage = await client.complete_with_usage(
        model=model,
        system=system,
        messages=messages,
        max_tokens=max_tokens,
    )
    return await apply_cluster_decisions(
        session, plan=plan, raw=raw, unresolved_tier=unresolved_tier
    ), usage
