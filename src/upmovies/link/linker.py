"""Stage 1 link service: classify a batch of stories against a set of tracked films and
apply the confidence floor, mutating each Story's link state in place. The caller owns the
session/commit. The LLM client is injected (Completer) so this is unit-testable with a fake.

**Two request shapes live here while the cutover runs.** `build_link_request` sends the
whole active catalog as one cached roster prefix and takes back a global roster index;
`build_retrieval_link_request` sends each story its own retrieved candidate list and takes
back an index into that story's list. The roster path is the incumbent the cutover's F1 gate
measures against, so it stays untouched until it is deleted outright at M4 (NEU-1004)."""

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol
from uuid import UUID

from upmovies.link.retrieval.render import render_candidates
from upmovies.link.retrieval.select import CandidateSet
from upmovies.link.roster import Roster
from upmovies.llm.client import CallLog, CallResult, cached_system_block
from upmovies.news.models import Story

log = logging.getLogger(__name__)

_SUMMARY_MAX = 500
_MAX_TOKENS = 2048
_NOT_NEWS_CATEGORIES = {"reaction", "roundup", "streaming-move", "interview-quote", "downstream"}

_ROSTER_HEADER = """You are an entity-linking classifier for an upcoming-movies tracker.

You are given a ROSTER of tracked films (each with a numeric index) and a batch of news \
stories (each an id, headline, and short dek). For every story, decide whether it is \
primarily ABOUT exactly one of the tracked films."""

_RETRIEVAL_HEADER = """You are an entity-linking classifier for an upcoming-movies tracker.

You are given a batch of news stories (each an id, headline, and short dek). Each story \
carries its OWN numbered list of candidate tracked films: the lists differ from story to \
story, and candidate 1 in one story's list is a DIFFERENT film from candidate 1 in \
another's. For every story, decide whether it is primarily ABOUT exactly one of the films \
in that story's candidate list. A story's candidates are the only tracked films available \
to it — never answer with an index that story does not offer. Each candidate carries its \
director, top-billed cast, and collection alongside its title, year, genres and overview: \
use ALL of them to separate a candidate from a same-franchise film that is not tracked \
here. A shared title stem is not a match; a shared director or lead is not a match either."""

# The classification rules are shared verbatim by both prompts, rendered with the term the
# path uses for the set of tracked films it shows. Two copies of ~1.4k tokens of prose
# would drift, and these paragraphs are load-bearing: the franchise traps are the
# counterweight to the precision cost of narrowing the candidate set (spec §4.3), so a fix
# to one prompt's wording must never silently miss the other's. The roster terms below
# reproduce today's prompt character for character — the roster path is still the incumbent
# the cutover's F1 gate measures against, and moving its baseline mid-project would make
# that comparison meaningless.
#
# `_CLASSIFICATION_RULES` is a `str.format` template: any literal brace added to it must be
# doubled (`{{`, `}}`) or the render below raises at import time. The JSON examples live in
# the per-path tails for exactly that reason — those are plain strings.
_ROSTER_TERMS = {
    "exact_film": "the exact roster film",
    "only_candidate": "the only roster candidate",
    "in_the_set": "in the roster",
}
_RETRIEVAL_TERMS = {
    "exact_film": "the exact candidate film",
    "only_candidate": "the only candidate offered",
    "in_the_set": "among the story's candidates",
}

_CLASSIFICATION_RULES = """Definitions:
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
downstream of an earlier beat. Aspirational or "wishlist" casting is also not-news: a \
story where talent expresses a desire, hope, or campaign for a role they do not currently \
hold — they "want to play", "would love to play", "hope to play", are "gunning for", \
"eyeing", or "fan-cast" in a part — reports no production fact and is not-news (category \
"interview-quote") EVEN WHEN it names the exact film and character. A real casting beat \
reports that a deal closed or the studio/filmmakers confirmed the actor ("has been cast", \
"joins the cast", "has signed on", or trade-reported "in talks") — an actor merely saying \
they want the role is not that. \
A story that re-reports an already-known casting, role, or other prior beat through a \
milestone, "debut", celebrity-family, or human-interest angle — adding no NEW trade-confirmed \
development — is also not-news (category "downstream"): a fresh publication date does not make \
it new. Use `as_of_date` to reason about whether a beat is genuinely recent or re-circulated \
old news.
A periodic release-calendar listicle — a "This Week's" / "This Month's" (OTT &) movie \
releases post, an "Upcoming <month> releases" roundup, or any multi-film list where a \
tracked film appears as one entry among many — is not-news (category "roundup") EVEN WHEN \
it states a release date: a calendar listicle restating a film's already-scheduled date is \
not a release-date announcement. Use `as_of_date` — a date the film already holds, at or \
near today, is not a new development.
- "mention": the film is only referenced in passing (an aside, a list, a comparison, or \
an actor's other project). Mentions are NOT links.
- "no-match": the story is not about any tracked film. Most stories are no-match — \
unrelated TV, games, sports, obituaries, or already-released films. Returning no-match is \
expected and correct.

Be strict about same-titled / substring traps: the tracked film "Runner" is not \
"showrunner" or "Blade Runner". Use the year, original title, genres, and overview to \
disambiguate.

Be strict about franchise-generic casting/announcement traps: a story that refers to a \
franchise only generically — "the next Batman", "a new Spider-Man", "the next James Bond" — \
is NOT necessarily about the tracked film that happens to share that franchise. Studios run \
multiple films per franchise, and many are not tracked here. Link such a story only when it \
unambiguously identifies {exact_film} (its distinct subtitle, year, or director). \
When {only_candidate} is a DIFFERENT entry in the same franchise, return no-match — \
do not force a nearest-match. \
This includes a distinct, NAMED sibling film — a spin-off, sequel, prequel, or origin/\
companion film ("a Shrek spin-off", "the Donkey origin movie", "an untitled sequel") — that \
is a DIFFERENT entry in a tracked film's franchise and is not itself tracked {in_the_set}. \
Return no-match for it EVEN WHEN the story states a release date: a spin-off's or sequel's \
own date is not the tracked film's date. Link only when the story unambiguously identifies \
{exact_film} (its distinct subtitle, year, or director).

This franchise trap runs in BOTH directions. A story about an EARLIER or ORIGINAL film in a \
franchise — the first film, when only its sequel or continuation is tracked here — is NOT \
about the tracked sequel merely because they share a title stem (a story about "The \
Housemaid" is not about the tracked "The Housemaid's Secret") or share a lead actor. A \
trailer, review, or release for the original film is that film's own beat, not the sequel's. \
Link to the tracked sequel only when the story unambiguously identifies IT — its distinct \
subtitle, its year, or a detail unique to it; a shared franchise title stem plus a shared \
star is NOT enough. When the story's real subject is the original/parent film and only the \
sequel is tracked, return no-match (or "mention" if the tracked sequel is named only in \
passing).

Be strict about other-film developments that only name a tracked film as context: a story \
whose actual subject is a DIFFERENT film — its release-date move, casting, delay, or \
box-office plan — is NOT "about" a tracked film merely because that film is named as a \
scheduling comparison or reference point (e.g. "Film X shifted its release to avoid clashing \
with [tracked film]" is about Film X). Return "mention" for the tracked film. Classify \
"about" only when the NEW development belongs to the tracked film itself.

Be strict about medium/project mismatches: a story about a DIFFERENT production sharing the \
same characters, setting, or franchise name — a spin-off TV series, an animated series, a \
video game, a stage adaptation, or any project in a different medium — is NOT about the \
tracked film merely because it names the same characters or franchise. A character's casting \
or appearance in a TV series, game, or other adjacent project is not a casting fact about the \
tracked FILM. Return "no-match" for it (or "mention" if the tracked film is named only in \
passing) unless the story also reports a new production fact about the film itself."""

_ROSTER_TAIL = """\
The input is a JSON object `{"as_of_date": <YYYY-MM-DD>, "stories": [...]}`. `as_of_date` is \
the date this run executed (UTC); treat it as "today" when judging how recent or stale a \
story is. Classify every story in `stories`.

Return ONLY a JSON array — no prose, no markdown — one object per input story, using the \
story's id:
[{"id": "<story id>", "film": <roster index or null>, "confidence": <0.0-1.0>, "reason": \
"about" | "mention" | "no-match" | "not-news", "category": "reaction" | "roundup" | \
"streaming-move" | "interview-quote" | "downstream" | null}]

"confidence" is your probability that the story is about that exact roster film (0.0 for \
mention/no-match/not-news). "category" labels why a "not-news" story was excluded (null \
otherwise)."""

_RETRIEVAL_TAIL = """\
The input is a JSON object `{"as_of_date": <YYYY-MM-DD>, "stories": [...]}`. `as_of_date` is \
the date this run executed (UTC); treat it as "today" when judging how recent or stale a \
story is. Each story object carries its `id`, `title`, `summary`, and its own `candidates` \
list; every candidate has an `n` and the details of one tracked film. Classify every story \
in `stories`.

Return ONLY a JSON array — no prose, no markdown — one object per input story, using the \
story's id:
[{"id": "<story id>", "film": <an "n" from that story's candidate list, or null>, \
"confidence": <0.0-1.0>, "reason": "about" | "mention" | "no-match" | "not-news", \
"category": "reaction" | "roundup" | "streaming-move" | "interview-quote" | "downstream" | \
null}]

"film" MUST be an "n" that appears in that story's candidate list, or null. Candidates are \
numbered independently per story, so a number taken from another story's list — or invented \
— names no film and will be discarded.

"confidence" is your probability that the story is about that exact candidate film (0.0 for \
mention/no-match/not-news). "category" labels why a "not-news" story was excluded (null \
otherwise)."""

_INSTRUCTIONS = (
    f"{_ROSTER_HEADER}\n\n{_CLASSIFICATION_RULES.format(**_ROSTER_TERMS)}\n\n{_ROSTER_TAIL}"
)
_RETRIEVAL_INSTRUCTIONS = (
    f"{_RETRIEVAL_HEADER}\n\n{_CLASSIFICATION_RULES.format(**_RETRIEVAL_TERMS)}\n\n"
    f"{_RETRIEVAL_TAIL}"
)


class Completer(Protocol):
    """The LLM surface a stage needs: one logical call, recorded into the caller's `CallLog`.
    Token usage rides along inside the recorded `CallResult` rather than being returned
    separately, so the per-call telemetry rows and the per-stage `run_llm_usage` aggregate are
    built from the same numbers (NEU-975)."""

    async def complete_call(
        self,
        *,
        model: str,
        system: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        max_tokens: int = ...,
        calls: CallLog,
    ) -> "CallResult": ...


@dataclass
class BatchLinkResult:
    linked: int
    rejected: int


@dataclass(frozen=True)
class StoryCandidates:
    """A story paired with the films retrieval offers for it.

    The two travel together from selection through to the request and, at NEU-999, through
    to resolving the reply's index back to a film — carrying them apart would let a story be
    scored against one candidate set and answered against another."""

    story: Story
    candidates: CandidateSet


def story_dek(story: Story) -> str:
    """The short summary shown to the classifier beside a story's headline.

    Public because candidate retrieval scores the same two fields (spec §4.1): shadow mode
    measuring recall against a wider or narrower text than the one the classifier reads
    would not be measuring the path that ships."""
    if not isinstance(story.raw, dict):
        return ""
    return str(story.raw.get("summary", ""))[:_SUMMARY_MAX]


def _story_object(story: Story) -> dict[str, str]:
    """The story fields both request builders send — one definition, because retrieval
    scores the same headline and dek the classifier reads (see `story_dek`)."""
    return {"id": str(story.id), "title": story.title, "summary": story_dek(story)}


def _story_payload(stories: Sequence[Story]) -> list[dict[str, str]]:
    return [_story_object(s) for s in stories]


def _extract_json_array(text: str) -> str:
    """Pull the JSON array out of a response that may be wrapped in prose/markdown fences."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


def build_link_request(
    roster: Roster, stories: Sequence[Story], run_date: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The cached roster system block + the JSON story payload for one story chunk."""
    # `instructions + roster` prefix = ~15 193 tok — clears Haiku 4.5's 4096-tok cache floor.
    # Verified 2026-06-24: call 1 cache_creation=15 193, call 2 cache_read=15 193 (NEU-377).
    system = [cached_system_block(f"{_INSTRUCTIONS}\n\nROSTER:\n{roster.text}")]
    payload = {"as_of_date": run_date.isoformat(), "stories": _story_payload(stories)}
    messages = [{"role": "user", "content": json.dumps(payload)}]
    return system, messages


def build_retrieval_link_request(
    batch: Sequence[StoryCandidates], run_date: date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The instructions-only system block + a story payload carrying per-story candidates.

    Batches survive the rewrite — they still amortize the instruction block over 15 stories —
    but the catalog leaves the system block entirely, which is the point of the whole
    project: the prefix stopped scaling with catalog size, so it stops threatening the 200k
    context ceiling when the undated-film expansion multiplies that catalog (spec §4.2).

    **The system block is deliberately not cached.** What remains is ~1.5k tokens, below
    Haiku 4.5's 4096-token cache floor, so `cache_control` would silently no-op — and `link`
    was the only stage that cached at all. That is the anticipated consequence, not a
    regression to engineer around.
    """
    for entry in batch:
        # A zero-candidate story is rejected without a model call (ADR-0009), so one
        # reaching here is a wiring bug upstream. Sending an empty list would be worse than
        # useless: it asks the model to choose from nothing, and every index it could reply
        # with would be out of list.
        if entry.candidates.is_empty:
            raise ValueError(f"story {entry.story.id} has no candidates and must not be sent")
    system = [{"type": "text", "text": _RETRIEVAL_INSTRUCTIONS}]
    payload = {
        "as_of_date": run_date.isoformat(),
        "stories": [
            {**_story_object(entry.story), "candidates": render_candidates(entry.candidates)}
            for entry in batch
        ],
    }
    # `ensure_ascii=False` because candidates carry titles the roster prefix used to send as
    # plain text. Escaped, one CJK or Cyrillic character costs six, and `original_title` is
    # rendered precisely when it *differs* from the title — i.e. exactly the non-Latin case.
    # That would blow the ~320 tok/story the rendering was costed at (spec §4.3) on the
    # stories that can least afford it, and hand the model `\uXXXX` runs to disambiguate on.
    messages = [{"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]
    return system, messages


@dataclass(frozen=True)
class _FilmChoice:
    """What one decision's `film` field resolved to, and whether it named nothing.

    `out_of_list` is what separates *the model chose no film* — the answer the prompt asks
    for most often — from *the model named a number that indexes nothing here*. Only the
    retrieval path can tell those apart: under the global roster every index the model could
    plausibly return resolved to some film, so there was no such reply to distinguish."""

    film_id: UUID | None = None
    out_of_list: bool = False


def _apply_decisions(
    *,
    raw: str,
    stories: Sequence[Story],
    floor: float,
    choose: Callable[[Story, object], _FilmChoice],
) -> BatchLinkResult:
    """Apply the classifier's JSON decisions to each Story in place: floor/resolution rules
    plus the no-decision fallback.

    Shared by both request shapes, which differ only in how a reply's `film` names a film —
    a global roster index or an index into that story's own candidate list. Everything after
    the resolution is one set of rules on purpose: the floor, the note vocabulary, and the
    no-decision fallback are what downstream stages and `link_note` queries read, and a
    second copy of them would drift exactly as two copies of the prompt would."""
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
        choice = choose(story, decision.get("film"))
        reason = decision.get("reason")
        try:
            confidence = float(decision.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

        story.linked_at = now
        if reason == "about" and choice.film_id is not None and confidence >= floor:
            story.link_status = "linked"
            story.film_id = choice.film_id
            story.link_confidence = confidence
            story.link_note = None
            linked += 1
        else:
            story.link_status = "rejected"
            story.film_id = None
            story.link_confidence = None
            if reason == "about" and choice.film_id is not None and confidence < floor:
                story.link_note = "below-floor"
            elif reason == "about" and choice.out_of_list:
                # Its own note rather than `no-match`: the model did not reach a verdict of
                # "no tracked film", it answered with a number naming none of the films this
                # story offered. Folding the two together would hide a numbering regression
                # inside the ordinary majority outcome — in the one stage where a lost story
                # is lost for good.
                story.link_note = "out-of-list"
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


def apply_link_decisions(
    *, raw: str, stories: Sequence[Story], roster: Roster, floor: float
) -> BatchLinkResult:
    """Apply decisions naming films by their **global roster index**.

    Unchanged behaviour: the roster resolves an unusable index to no film and the reply is
    stamped `no-match`, as it always has been. It never reports `out_of_list`, because with
    one shared list there is no story-local scope for an index to fall outside of — and the
    roster path is the incumbent the cutover's F1 gate measures against, so its notes must
    not move underneath that comparison."""
    return _apply_decisions(
        raw=raw,
        stories=stories,
        floor=floor,
        choose=lambda _story, index: _FilmChoice(film_id=roster.film_id_for_index(index)),
    )


def apply_retrieval_link_decisions(
    *, raw: str, batch: Sequence[StoryCandidates], floor: float
) -> BatchLinkResult:
    """Apply decisions naming films by an index into **that story's own candidate list**.

    Each story is answered against the candidate set it was scored and sent with — the two
    travel together in `StoryCandidates` precisely so they cannot be paired up wrongly here.
    An index outside a story's list is **rejected, not coerced**: numbering restarts per
    story, so a number valid in a neighbour's list names nothing here, and quietly linking
    to the nearest candidate would turn a defective reply into a confident wrong link."""
    candidates_by_story = {str(entry.story.id): entry.candidates for entry in batch}

    def choose(story: Story, index: object) -> _FilmChoice:
        if index is None:
            return _FilmChoice()
        film_id = candidates_by_story[str(story.id)].film_id_for_index(index)
        return _FilmChoice(film_id=film_id, out_of_list=film_id is None)

    return _apply_decisions(
        raw=raw, stories=[entry.story for entry in batch], floor=floor, choose=choose
    )


def reject_zero_candidate_stories(stories: Sequence[Story]) -> int:
    """Reject every story retrieval found nothing for, and return how many. No model call.

    The zero-candidate rejection (ADR-0009), and on the measured corpus the majority of the
    stage's workload. `no-candidates` is deliberately its own note rather than folded into
    `no-match`: it is the difference between *the classifier judged this story* and *the
    classifier never saw it*, which is what keeps stories lost to a retrieval bug queryable
    and re-runnable inside the recency window.

    The count is returned rather than folded into a `BatchLinkResult` because its caller
    must keep it out of `StageCounts.processed` — these stories were decided by a lexical
    rule, and letting them stand in for classifier output would switch off the total-failure
    guard on the repo's one lossy stage."""
    now = datetime.now(UTC)
    for story in stories:
        story.link_status = "rejected"
        story.film_id = None
        story.link_confidence = None
        story.link_note = "no-candidates"
        story.linked_at = now
    return len(stories)


async def link_story_batch(
    *,
    client: Completer,
    model: str,
    roster: Roster,
    stories: Sequence[Story],
    floor: float,
    run_date: date,
    calls: CallLog,
) -> BatchLinkResult:
    """One classifier call for one batch of stories, recorded into `calls` — including the
    parse outcome, which `link` deliberately raises on rather than swallowing (spec §12)."""
    if not stories:
        return BatchLinkResult(0, 0)
    system, messages = build_link_request(roster, stories, run_date)
    result = await client.complete_call(
        model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS, calls=calls
    )
    try:
        decisions = apply_link_decisions(
            raw=result.text, stories=stories, roster=roster, floor=floor
        )
    except Exception:
        # `apply_link_decisions` does no I/O, so anything it raises is the model's output being
        # unusable — a bare JSONDecodeError when the reply isn't JSON, but equally an
        # AttributeError when it is JSON of the wrong shape. Recording only the first would
        # leave the second as a NULL, which reads as "no parse happened".
        calls.set_parse_ok(False)
        raise
    calls.set_parse_ok(True)
    return decisions


async def link_retrieval_story_batch(
    *,
    client: Completer,
    model: str,
    batch: Sequence[StoryCandidates],
    floor: float,
    run_date: date,
    calls: CallLog,
) -> BatchLinkResult:
    """One classifier call for one batch of stories and their candidate sets.

    The retrieval counterpart of `link_story_batch`, raising on unusable output on the same
    terms. `batch` carries only stories with candidates — a zero-candidate story is rejected
    by `reject_zero_candidate_stories` without ever reaching a model — so an empty batch here
    means the whole chunk was zero-candidate and there is nothing to ask about."""
    if not batch:
        return BatchLinkResult(0, 0)
    system, messages = build_retrieval_link_request(batch, run_date)
    result = await client.complete_call(
        model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS, calls=calls
    )
    try:
        decisions = apply_retrieval_link_decisions(raw=result.text, batch=batch, floor=floor)
    except Exception:
        calls.set_parse_ok(False)
        raise
    calls.set_parse_ok(True)
    return decisions
