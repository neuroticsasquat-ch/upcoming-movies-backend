"""Stage 1 link service: classify a batch of stories against the cached roster and apply
the confidence floor, mutating each Story's link state in place. The caller owns the
session/commit. The LLM client is injected (Completer) so this is unit-testable with a fake."""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from upmovies.link.roster import Roster
from upmovies.llm.client import BatchRequest, BatchResult, Usage, cached_system_block
from upmovies.news.models import Story

log = logging.getLogger(__name__)

_SUMMARY_MAX = 500
_MAX_TOKENS = 2048
_NOT_NEWS_CATEGORIES = {"reaction", "roundup", "streaming-move", "interview-quote", "downstream"}

_INSTRUCTIONS = """You are an entity-linking classifier for an upcoming-movies tracker.

You are given a ROSTER of tracked films (each with a numeric index) and a batch of news \
stories (each an id, headline, and short dek). For every story, decide whether it is \
primarily ABOUT exactly one of the tracked films.

Definitions:
- "about": the story announces or confirms something NEW about exactly one tracked film's \
production — casting, a filming start/wrap/status change, a trailer or teaser, a release \
date set or moved, a major creative/production change (director, studio, format), or a \
release-affecting distribution deal.
- "not-news": the story is primarily about a tracked film but announces nothing new about \
its production. Do NOT link these. Core test: if it reports no NEW production fact, it is \
not-news even when it is unmistakably about the film. Examples: cast/crew enthusiasm, \
praise, or fan and social-media reactions; interview color about working on the project — \
headlines where a cast member "teases", "reacts to", or "opens up on" the film, says they \
"can't wait" or are "excited for" it, or anything framed as "ahead of its release"; \
"everything we know so far" roundups and aggregators with no new information; talent \
comments on plot points that are not a formal announcement; streaming-platform or \
catalogue moves of an existing title; and any story whose news value is entirely \
downstream of an earlier beat.
- "mention": the film is only referenced in passing (an aside, a list, a comparison, or \
an actor's other project). Mentions are NOT links.
- "no-match": the story is not about any tracked film. Most stories are no-match — \
unrelated TV, games, sports, obituaries, or already-released films. Returning no-match is \
expected and correct.

Be strict about same-titled / substring traps: the tracked film "Runner" is not \
"showrunner" or "Blade Runner". Use the year, original title, genres, and overview to \
disambiguate.

Return ONLY a JSON array — no prose, no markdown — one object per input story, using the \
story's id:
[{"id": "<story id>", "film": <roster index or null>, "confidence": <0.0-1.0>, "reason": \
"about" | "mention" | "no-match" | "not-news", "category": "reaction" | "roundup" | \
"streaming-move" | "interview-quote" | "downstream" | null}]

"confidence" is your probability that the story is about that exact roster film (0.0 for \
mention/no-match/not-news). "category" labels why a "not-news" story was excluded (null \
otherwise)."""


class Completer(Protocol):
    async def complete_with_usage(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = ...,
    ) -> tuple[str, "Usage"]: ...


class BatchCompleter(Protocol):
    async def complete_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: float = ...,
        timeout: float = ...,
    ) -> dict[str, BatchResult]: ...


class LinkClient(Completer, BatchCompleter, Protocol): ...


@dataclass
class BatchLinkResult:
    linked: int
    rejected: int


def _story_payload(stories: Sequence[Story]) -> list[dict[str, str]]:
    payload: list[dict[str, str]] = []
    for s in stories:
        summary = ""
        if isinstance(s.raw, dict):
            summary = str(s.raw.get("summary", ""))[:_SUMMARY_MAX]
        payload.append({"id": str(s.id), "title": s.title, "summary": summary})
    return payload


def _extract_json_array(text: str) -> str:
    """Pull the JSON array out of a response that may be wrapped in prose/markdown fences."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def build_link_request(
    roster: Roster, stories: Sequence[Story]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The cached roster system block + the JSON story payload — shared by both the
    sequential `complete()` path and the batched `complete_batch()` path."""
    # `instructions + roster` prefix = ~15 193 tok — clears Haiku 4.5's 4096-tok cache floor.
    # Verified 2026-06-24: call 1 cache_creation=15 193, call 2 cache_read=15 193 (NEU-377).
    system = [cached_system_block(f"{_INSTRUCTIONS}\n\nROSTER:\n{roster.text}")]
    messages = [{"role": "user", "content": json.dumps(_story_payload(stories))}]
    return system, messages


def build_batch_request(
    *, custom_id: str, model: str, roster: Roster, stories: Sequence[Story]
) -> BatchRequest:
    """One Message-Batch request for a story chunk. `max_tokens` matches the sequential
    path's `_MAX_TOKENS` so the two paths are token-identical."""
    system, messages = build_link_request(roster, stories)
    return BatchRequest(
        custom_id=custom_id, model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
    )


def apply_link_decisions(
    *, raw: str, stories: Sequence[Story], roster: Roster, floor: float
) -> BatchLinkResult:
    """Apply the classifier's JSON decisions to each Story in place: floor/resolution rules
    plus the no-decision fallback. Identical for both execution paths."""
    decisions = json.loads(_extract_json_array(raw))  # raises on un-parseable output

    by_id = {str(s.id): s for s in stories}
    now = datetime.now(UTC)
    decided: set[str] = set()
    linked = rejected = 0

    for decision in decisions:
        sid = str(decision.get("id"))
        story = by_id.get(sid)
        if story is None:
            continue
        decided.add(sid)
        film_id = roster.film_id_for_index(decision.get("film"))
        reason = decision.get("reason")
        try:
            confidence = float(decision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        story.linked_at = now
        if reason == "about" and film_id is not None and confidence >= floor:
            story.link_status = "linked"
            story.film_id = film_id
            story.link_confidence = confidence
            story.link_note = None
            linked += 1
        else:
            story.link_status = "rejected"
            story.film_id = None
            story.link_confidence = None
            if reason == "about" and film_id is not None and confidence < floor:
                story.link_note = "below-floor"
            elif reason == "mention":
                story.link_note = "mention"
            elif reason == "not-news":
                category = decision.get("category")
                story.link_note = (
                    f"not-news:{category}" if category in _NOT_NEWS_CATEGORIES else "not-news"
                )
            else:
                story.link_note = "no-match"
            rejected += 1

    for sid, story in by_id.items():
        if sid not in decided:
            log.warning("linker returned no decision for story %s", sid)
            story.link_status = "rejected"
            story.link_note = "no-decision"
            story.linked_at = now
            rejected += 1

    return BatchLinkResult(linked, rejected)


async def link_story_batch(
    *,
    client: Completer,
    model: str,
    roster: Roster,
    stories: Sequence[Story],
    floor: float,
) -> tuple[BatchLinkResult, Usage]:
    if not stories:
        return BatchLinkResult(0, 0), Usage()
    system, messages = build_link_request(roster, stories)
    raw, usage = await client.complete_with_usage(
        model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
    )
    return apply_link_decisions(raw=raw, stories=stories, roster=roster, floor=floor), usage
