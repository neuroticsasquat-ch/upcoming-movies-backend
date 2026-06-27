"""Summarization service: turn one news event (as an in-memory EventInput) into one neutral
single-sentence paraphrase via the Anthropic Messages/Batches API. Self-contained (no DB); the
caller maps ORM→EventInput and persists the returned SummaryResult. The LLM client is injected
(Completer / BatchCompleter) so this is unit-testable with fakes."""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from upmovies.llm.client import BatchRequest, BatchResult, Usage

_DEK_MAX = 500
_MAX_TOKENS = 256

# Seed the assistant turn so the model continues a JSON envelope instead of preambling or
# "thinking out loud" — Haiku occasionally narrates its reasoning when a beat type doesn't
# match the sources, which (pre-guard) leaked into a stored summary. With this prefill the
# reply is just the summary value + closing `"}`.
_PREFILL = '{"summary": "'

# A one/two-sentence blurb is well under this. Anything longer is leaked reasoning / runaway
# output, not a summary — reject it (the event is skipped, not stored as garbage).
_SUMMARY_MAX_CHARS = 400

log = logging.getLogger(__name__)

# Permissive recovery for malformed `{"summary": "..."}` envelopes (NEU-366). The greedy
# capture runs to the last quote before the closing brace, so an unescaped inner quote is
# captured as text rather than treated as the string terminator. Safe because the prompt
# mandates a single-key object.
_SUMMARY_VALUE_RE = re.compile(r'"summary"\s*:\s*"(.*)"\s*}?\s*$', re.DOTALL)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f]+")

_INSTRUCTIONS = """You write neutral, factual update blurbs for an upcoming-movies tracker.

You are given one tracked film, the TYPE of news beat being reported, and the stories that \
report it (each a headline, a short dek, and its publication's name). Write ONE sentence \
summarizing this beat for a reader who is already on this film's page and knows which film \
it is.

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
- Do NOT restate the film's title. It renders on the film's own page, so naming the film is \
redundant. Use "the film" only if a reference is grammatically necessary.
- Name only the people party to THIS beat — e.g. the cast member for a casting beat. Omit \
the director and other crew unless the beat is itself about them (e.g. an "X to direct" \
announcement).
- No relational or biographical framing — don't add who someone is related to, married to, \
or reuniting with ("daughter of …", "reuniting with his father").
- Report only the production fact. State who / what / when and stop. Exclude: fan, audience, \
or social-media reactions; whether the news "generated discussion" or "circulated on social \
media"; the talent's stated feelings about their role or the project; "ahead of" / "in \
advance of" / "approaches release" framing that adds no fact; and any meta-commentary about \
the announcement itself. If the summary could be replaced by the headline with nothing lost, \
it has too much filler — cut it back.
- Prefer the canonical shape: state the beat in the film's already-known context and nothing \
more — e.g. "{Star} has joined the cast", "{Star} has been cast as {Character}" (character \
optional), "{Star} will reprise their role", "{Star} will make their acting debut".
- ONE sentence. Add a second only if it carries a distinct production fact (not a trailer \
like "Composer X has discussed the process").

Return ONLY a JSON object, no prose or markdown:
{"summary": "<your one-sentence update>"}"""


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


def _recover_summary(candidate: str) -> str | None:
    """Permissive fallback for when strict JSON parsing fails: pull the `summary` value by
    regex (greedy to the last quote before the closing brace, so an unescaped inner quote is
    captured as text), strip ASCII control chars, and collapse whitespace. Returns the cleaned
    summary, or None if nothing usable is found."""
    match = _SUMMARY_VALUE_RE.search(candidate)
    if match is None:
        return None
    value = _CONTROL_CHARS_RE.sub(" ", match.group(1))
    value = " ".join(value.split())
    return value or None


def _parse_once(text: str) -> str:
    """Extract the JSON object, read its `summary`, and return it stripped. Raises
    `json.JSONDecodeError` when no JSON/recovery is possible, or `ValueError` when valid JSON
    carries a missing/empty `summary`."""
    candidate = _extract_json_object(text)
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        recovered = _recover_summary(candidate)
        if recovered:
            log.warning(
                "parse_summary recovered summary from malformed JSON envelope: %r", text[:500]
            )
            return recovered
        raise
    summary = obj.get("summary") if isinstance(obj, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError(f"no usable summary in model output: {text!r}")
    return summary.strip()


def parse_summary(raw: str) -> str:
    """Read the model's `summary` from its reply and return it stripped. Tolerates a full JSON
    envelope (possibly prose/markdown-wrapped) AND a prefilled continuation (the reply that
    continues our seeded `{"summary": "` assistant turn). Rejects runaway output — anything
    longer than `_SUMMARY_MAX_CHARS` or containing a code fence is leaked reasoning, not a
    summary, so we raise rather than store garbage (the caller skips the event)."""
    try:
        summary = _parse_once(raw)
    except json.JSONDecodeError:
        # Likely a prefilled continuation (no leading `{`) — rebuild the envelope and retry.
        summary = _parse_once(_PREFILL + raw)
    if len(summary) > _SUMMARY_MAX_CHARS or "```" in summary:
        raise ValueError(f"summary failed sanity checks (len={len(summary)}): {raw!r}")
    return summary


def build_summary_request(
    event: EventInput,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The plain instructions system block + the JSON event payload — shared by both the
    sequential `complete()` path and the batched `complete_batch()` path. Truncates each dek."""
    system = [{"type": "text", "text": _INSTRUCTIONS}]
    messages = [
        {"role": "user", "content": json.dumps(_event_payload(event))},
        # Assistant prefill: forces a JSON-envelope continuation (no preamble/reasoning).
        {"role": "assistant", "content": _PREFILL},
    ]
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
) -> tuple[SummaryResult, Usage]:
    """Sequential path: build the request, call `complete_with_usage` (max_tokens pinned to
    `_MAX_TOKENS` for parity with the batched path), parse the JSON envelope, and bundle the
    provenance the caller persists into `event_summary` alongside the call's token `Usage`."""
    system, messages = build_summary_request(event)
    raw, usage = await client.complete_with_usage(
        model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
    )
    result = SummaryResult(
        summary=parse_summary(raw),
        model=model,
        prompt_version=prompt_version,
        source_updated_at=event.source_updated_at,
    )
    return result, usage
