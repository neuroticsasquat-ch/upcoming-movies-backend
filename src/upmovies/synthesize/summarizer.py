"""Summarization service: turn one news event (as an in-memory EventInput) into one neutral
2–3 sentence paraphrase via the Anthropic Messages/Batches API. Self-contained (no DB); the
caller maps ORM→EventInput and persists the returned SummaryResult. The LLM client is injected
(Completer / BatchCompleter) so this is unit-testable with fakes."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from upmovies.llm.client import BatchRequest, BatchResult

_DEK_MAX = 500
_MAX_TOKENS = 256

_INSTRUCTIONS = """You write neutral, factual update blurbs for an upcoming-movies tracker.

You are given one tracked film, the TYPE of news beat being reported, and the stories that \
report it (each a headline, a short dek, and its publication's name). Write ONE update of \
2–3 sentences summarizing this beat for a reader following the film.

Rules:
- Strict paraphrase. State the facts (who / what / when) entirely in your own words. \
Reproduce no phrasing from the headlines or deks — no quoted fragments, no distinctive \
wording lifted verbatim.
- Neutral register. Report what happened plainly; no hype, opinion, clickbait, or \
editorializing.
- Preserve certainty. Never present a report or rumor as established fact. If the sources \
hedge ("reportedly", "is said to"), hedge; if they confirm, state it plainly. Never upgrade \
the sources' level of certainty.
- Stay within the sources. Add no facts, dates, names, or speculation not present in the \
provided material.
- Refer to the film by its title where natural. 2–3 sentences, one paragraph.

Return ONLY a JSON object, no prose or markdown:
{"summary": "<your 2–3 sentence update>"}"""


@dataclass(frozen=True)
class StoryInput:
    title: str
    dek: str
    source: str


@dataclass(frozen=True)
class EventInput:
    event_type: str
    film_title: str
    source_updated_at: datetime
    stories: Sequence[StoryInput]


@dataclass(frozen=True)
class SummaryResult:
    summary: str
    model: str
    prompt_version: str
    source_updated_at: datetime


class Completer(Protocol):
    async def complete(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = ...,
    ) -> str: ...


class BatchCompleter(Protocol):
    async def complete_batch(
        self,
        requests: list[BatchRequest],
        *,
        poll_interval: float = ...,
        timeout: float = ...,
    ) -> dict[str, BatchResult]: ...


class SummaryClient(Completer, BatchCompleter, Protocol): ...


def _event_payload(event: EventInput) -> dict[str, Any]:
    return {
        "film": event.film_title,
        "event_type": event.event_type,
        "stories": [
            {"title": s.title, "dek": s.dek[:_DEK_MAX], "source": s.source} for s in event.stories
        ],
    }


def _extract_json_object(text: str) -> str:
    """Pull the JSON object out of a response that may be wrapped in prose/markdown fences."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def parse_summary(raw: str) -> str:
    """Extract the JSON object, read its `summary`, and return it stripped. Raises on
    un-parseable output (`json.JSONDecodeError`) or a missing/empty `summary` (`ValueError`)."""
    obj = json.loads(_extract_json_object(raw))
    summary = obj.get("summary") if isinstance(obj, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f"no usable summary in model output: {raw!r}")
    return summary.strip()


def build_summary_request(
    event: EventInput,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The plain instructions system block + the JSON event payload — shared by both the
    sequential `complete()` path and the batched `complete_batch()` path. Truncates each dek."""
    system = [{"type": "text", "text": _INSTRUCTIONS}]
    messages = [{"role": "user", "content": json.dumps(_event_payload(event))}]
    return system, messages


def build_summary_batch_request(*, custom_id: str, model: str, event: EventInput) -> BatchRequest:
    """One Message-Batch request for one event. `custom_id` is the event id (the caller
    supplies it). `max_tokens` matches the sequential path's `_MAX_TOKENS` for parity."""
    system, messages = build_summary_request(event)
    return BatchRequest(
        custom_id=custom_id, model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
    )


async def summarize_event(
    *, client: Completer, model: str, prompt_version: str, event: EventInput
) -> SummaryResult:
    """Sequential path: build the request, call `complete` (max_tokens pinned to `_MAX_TOKENS`
    for parity with the batched path), parse the JSON envelope, and bundle the provenance the
    caller persists into `event_summary`."""
    system, messages = build_summary_request(event)
    raw = await client.complete(
        model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
    )
    return SummaryResult(
        summary=parse_summary(raw),
        model=model,
        prompt_version=prompt_version,
        source_updated_at=event.source_updated_at,
    )
