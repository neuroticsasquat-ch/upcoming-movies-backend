"""Stage 2: cluster a single film's unclustered linked stories into events and classify
them, attaching to recent existing events where they continue a beat. Idempotent — only
touches linked stories that have no event_story row yet; the unique story_id is the
backstop. The caller owns the session/commit."""

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.catalog.models import Film
from upmovies.link.linker import Completer
from upmovies.llm.client import cached_system_block
from upmovies.news.models import Event, EventStory, Story

log = logging.getLogger(__name__)

_VALID_TYPES = {
    "announced",
    "casting",
    "production_start",
    "production_wrap",
    "release_date",
    "trailer",
    "other",
}
_SUMMARY_MAX = 500
_MAX_TOKENS = 1500

_INSTRUCTIONS = """You group a single film's news stories into distinct EVENTS — real beats \
in its life (casting, a trailer, a release-date change, production milestones, etc.) — and \
classify each. You are given the FILM, its EXISTING recent events, and NEW stories to place.

For each new story, either attach it to an existing event (it continues a beat already \
logged) or assign it to a new event (a beat not yet logged). Group new stories that report \
the SAME beat into ONE new event; split different beats into separate events. Five outlets \
reporting the same casting is one event.

New events carry:
- "type": one of announced, casting, production_start, production_wrap, release_date, \
trailer, other
- "confidence": "confirmed" if reported as fact, "rumored" if speculation/unconfirmed.

Return ONLY JSON — no prose, no markdown:
{"events": [{"existing": <existing event number or null>, "type": <type or null>, \
"confidence": "confirmed" | "rumored" | null, "stories": ["<story id>", ...]}]}

When "existing" is a number, attach its "stories" to that event ("type"/"confidence" may \
be null). Otherwise it is a new event and "type"/"confidence" are required. Every new \
story id must appear in exactly one group."""


@dataclass
class ClusterResult:
    events_created: int
    stories_clustered: int


def _extract_json_object(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


async def cluster_film_events(
    session: AsyncSession,
    *,
    client: Completer,
    model: str,
    film_id: UUID,
    recency_days: int,
) -> ClusterResult:
    film = (await session.execute(select(Film).where(Film.id == film_id))).scalar_one_or_none()
    if film is None:
        return ClusterResult(0, 0)

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
        return ClusterResult(0, 0)

    cutoff = datetime.now(UTC) - timedelta(days=recency_days)
    existing_events = (
        (
            await session.execute(
                select(Event)
                .where(Event.film_id == film_id, Event.updated_at >= cutoff)
                .order_by(Event.occurred_at, Event.id)
            )
        )
        .scalars()
        .all()
    )

    existing_payload = []
    for i, event in enumerate(existing_events, start=1):
        headlines = (
            (
                await session.execute(
                    select(Story.title)
                    .join(EventStory, EventStory.story_id == Story.id)
                    .where(EventStory.event_id == event.id)
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

    by_id = {str(s.id): s for s in unclustered}
    new_payload = [
        {
            "id": sid,
            "title": s.title,
            "summary": (str(s.raw.get("summary", "")) if isinstance(s.raw, dict) else "")[
                :_SUMMARY_MAX
            ],
        }
        for sid, s in by_id.items()
    ]

    user = {
        "film": {
            "title": film.title,
            "year": film.release_date.year if film.release_date else None,
        },
        "existing_events": existing_payload,
        "new_stories": new_payload,
    }
    raw = await client.complete(
        model=model,
        system=[cached_system_block(_INSTRUCTIONS)],
        messages=[{"role": "user", "content": json.dumps(user)}],
        max_tokens=_MAX_TOKENS,
    )
    data = json.loads(_extract_json_object(raw))

    now = datetime.now(UTC)
    assigned: set[str] = set()
    events_created = stories_clustered = 0

    for group in data.get("events", []):
        sids = [sid for sid in (group.get("stories") or []) if sid in by_id and sid not in assigned]
        if not sids:
            continue
        existing_idx = group.get("existing")
        if isinstance(existing_idx, int) and 1 <= existing_idx <= len(existing_events):
            event = existing_events[existing_idx - 1]
            event.updated_at = now
        else:
            etype = group.get("type")
            conf = group.get("confidence")
            if etype not in _VALID_TYPES or conf not in ("confirmed", "rumored"):
                log.warning(
                    "cluster: invalid new event for film %s: type=%r confidence=%r",
                    film_id,
                    etype,
                    conf,
                )
                continue
            occurred = min((by_id[sid].published_at or by_id[sid].fetched_at) for sid in sids)
            event = Event(film_id=film_id, event_type=etype, confidence=conf, occurred_at=occurred)
            session.add(event)
            await session.flush()
            events_created += 1
        for sid in sids:
            session.add(EventStory(event_id=event.id, story_id=UUID(sid)))
            assigned.add(sid)
            stories_clustered += 1

    return ClusterResult(events_created, stories_clustered)
