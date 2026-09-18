"""The `resolve` stage: the closed-set multiple choice that decides the narrow band (D-22).

The third and only probabilistic step of person resolution (CONTEXT.md "Resolution").
Everything before it is arithmetic over facts: extraction emits names, candidate generation
unions three deterministic sources, and scoring routes what it can separate. What reaches
here is the residue — mentions where two or more candidates scored within `ACCEPT_MARGIN` of
each other, which in practice means TMDB knows two people by the name the story wrote and
nothing in the catalog says which one it meant. D-22 caps that band at ≤10% of mentions, and
it is the *only* place in resolution a model is asked to identify anybody.

**Closed set, and enforced as one.** The shortlist scoring already built is the whole answer
space: pick one option, or say none. A number naming no option is **rejected, not coerced** —
the same rule and the same reason as the link stage's out-of-list reply (`linker.py`): folding
a defective answer into the nearest plausible one turns a numbering bug into a confident
wrong person, which is precisely the failure M4 exists to prevent. Here rejection means the
deterministic decision stands: the mention stays `tiebreak` with nobody named, on D-25's
`/admin/resolution` queue for a human, and is not asked again.

**The prompt is prefix-stable** (`llm.types.Prompt`): the instructions are one module-level
constant and everything that varies per mention — the film, the story, the mention, the
options — is serialized into `user`. Nothing here will engage any provider's cache at today's
size (the block is well under Haiku 4.5's 4096-token floor), which is the anticipated outcome
for every stage in this repo rather than a defect; the contract costs nothing and engages by
itself if the block grows.

**What the options carry is what separates namesakes.** The name is common to all of them by
construction, so it is the *rest* of each row that decides: what TMDB says they are known
for, what they are already credited as on this very film, and whether they joined or left it
in the last fortnight. A search-hit candidate contributes its `known_for` titles; a candidate
the film's own credits produced contributes that credit, which is the sharper fact of the two.
Scores are deliberately **not** shown. The band exists because the scores were
indistinguishable, so printing them would either say nothing or invite the model to ratify an
ordering that popularity — never a primary signal (D-21) — happens to have set.

**Nothing here writes `news.resolution_cache`.** D-24 blesses caching a negative and the
pipeline's reader already understands one, but a tiebreak answer is the one resolution in the
system that a human is expected to review, and caching it would spread a single unreviewed
model judgement across every later story from that publisher naming that name on that film —
silently, and as an `accepted` cache hit that no longer looks like a tiebreak at all. The band
is ≤10% of mentions; paying for it again is cheaper than laundering it.
"""

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from upmovies.link.resolve.candidates import Candidate
from upmovies.link.resolve.scoring import Mention
from upmovies.llm.types import CallLog, Completer, Prompt

log = logging.getLogger(__name__)

# Room for the object plus its one short clause of reasoning, and no room for an essay: the
# reply is a number and a phrase, so a model that starts narrating is truncated into an
# unparseable answer the stage already knows how to reject rather than being paid for.
_MAX_TOKENS = 256

# `reason` is stored on the mention for D-25's reviewer, so it is bounded on the way in — the
# field is a clause, and a model that returns a paragraph does not get to grow the row.
_REASON_MAX = 240

_INSTRUCTIONS = """You are identifying ONE person named in a film-trade news story.

A deterministic pass has already reduced the possible answers to the numbered options you \
are given, and could not separate them: they are people TMDB knows by (or close to) the name \
the story wrote. Your only job is to say which ONE of them the story means, or that none of \
them is that person.

The options are a CLOSED SET and they are numbered per request. Answer with an "n" that \
appears in the options list, or null. A number that names no option identifies nobody and \
will be discarded — never invent one, and never answer with an index the list does not offer.

Being offered an option is not evidence about it. The options share a name with the mention, \
which is why they are here and is not a reason to pick any of them: separate them on what \
they do. Weigh, in this order, whether an option is already credited on THIS film or joined \
or left it recently, whether their department and job fit the role the story gives the \
person, and whether the work they are known for fits the story's subject. A shared name is \
never enough on its own.

Prefer null to a guess. A mention nobody is named for is recoverable and is reviewed by a \
human; a confidently wrong person becomes an alert about somebody who was never in the \
story. If two options remain genuinely indistinguishable on the evidence in front of you, \
that is null, not a coin flip.

The input is a JSON object: {"film": {...}, "story": {...}, "mention": {...}, \
"options": [...]}. "film" is the film the story has already been linked to. "story" is the \
headline and dek as published. "mention" is the name exactly as the story wrote it, with \
whatever role, department, other title and beat the extraction pass reported and the quote it \
was named in. Each option carries its "n", the person's name as TMDB spells it, what TMDB \
says they are known for, any credit they currently hold on this film, and any credit change \
on this film in the last fortnight.

Return ONLY a JSON object — no prose, no markdown:
{"option": <an "n" from the options list, or null>, "reason": "<one short clause>"}

"reason" is read by a human reviewing the decision, so cite the evidence that separated the \
options — not a restatement of the name."""


@dataclass(frozen=True)
class TiebreakQuestion:
    """What one mention's closed-set question is asked about.

    The story text, not the article body: this repo stores a headline and a dek and nothing
    more, and `linker.story_dek` is the one spelling of "the story text a model is shown" —
    reading a wider or narrower text here than the linker scored would ask about a story the
    pipeline never classified.
    """

    film_title: str
    film_year: int | None
    story_title: str
    story_text: str
    mention: Mention
    evidence_span: str | None = None


@dataclass(frozen=True)
class TiebreakReply:
    """What one closed-set answer resolved to, as four mutually exclusive outcomes.

    Two of them are usable — an option, or an explicit none — and two are not. The unusable
    pair is kept apart rather than merged into a single failure, for the same reason the link
    stage keeps `out-of-list` apart from `no-match`: a model answering with a number that
    indexes nothing is a regression in the request or the numbering, while an unparseable
    reply is a model or a truncation problem, and one hidden inside the other is a class of
    bug nobody goes looking for.
    """

    option: int | None = None
    reason: str | None = None
    answered_none: bool = False
    out_of_list: bool = False
    unparseable: bool = False

    @property
    def usable(self) -> bool:
        """Whether this reply decides the mention. A rejected reply leaves the deterministic
        `tiebreak` decision standing rather than overwriting it with a guess."""
        return self.option is not None or self.answered_none


def build_tiebreak_request(question: TiebreakQuestion, options: Sequence[Candidate]) -> Prompt:
    """The instructions-only stable prefix plus this mention's film, story and shortlist.

    Options are numbered from 1 in the order they arrive, which is the ranked order scoring
    produced: equal scores, ordered by the popularity prior that D-21 allows to decide who
    heads a shortlist and nothing else. The numbering is per request — nothing outside this
    call can interpret an "n" — which is what makes an out-of-range answer detectable at all.
    """
    if not options:
        # A mention with no candidates never routes to the band (`resolve_mention` floors an
        # empty ranking at zero and routes it `unlinked`), so an empty shortlist here is a
        # wiring bug. Asking a closed-set question with an empty answer space would spend a
        # call to be told "none" by construction.
        raise ValueError("a tiebreak needs at least one option to choose between")
    payload = {
        "film": {"title": question.film_title, "year": question.film_year},
        "story": {"title": question.story_title, "summary": question.story_text},
        "mention": {
            "name_as_written": question.mention.name_as_written,
            "role": question.mention.role,
            "department": question.mention.department,
            "title_mentioned": question.mention.title_mentioned,
            "event_type": question.mention.event_type,
            "evidence_span": question.evidence_span,
        },
        "options": [_option(n, candidate) for n, candidate in enumerate(options, start=1)],
    }
    # `ensure_ascii=False` for the same reason the linker sets it: the names most in need of
    # disambiguation are exactly the ones carrying non-Latin script in `original_name`, and
    # escaping them costs six characters apiece and hands the model `\uXXXX` runs to compare.
    return Prompt(
        stable_prefix=_INSTRUCTIONS,
        user=json.dumps(payload, ensure_ascii=False),
        max_tokens=_MAX_TOKENS,
        # No prefill: `resolve` is a stage an operator may point at an OpenAI-compatible
        # provider, where a trailing assistant turn is history rather than a continuation, and
        # this parser reads a whole object off the reply either way (`llm.types.Prompt`).
        prefill_required=False,
    )


def _option(n: int, candidate: Candidate) -> dict:
    """One numbered option: who TMDB says this is, and what ties them to this film.

    Only keys with something in them are emitted. An option padded with nulls reads as
    evidence of absence — "known for nothing", "no credits" — when what it actually means is
    that the source that would have carried the fact was not the one that produced this
    candidate. Absence of evidence is not evidence, the same rule scoring applies to its
    bonuses.
    """
    option: dict = {"n": n, "name": candidate.name}
    if candidate.original_name and candidate.original_name != candidate.name:
        option["original_name"] = candidate.original_name
    if candidate.known_for_department:
        option["known_for_department"] = candidate.known_for_department
    if candidate.known_for_titles:
        option["known_for"] = list(candidate.known_for_titles)
    if candidate.credits:
        option["credited_on_this_film"] = [
            _credit(credit.credit_type, credit.job, credit.department)
            for credit in candidate.credits
        ]
    if candidate.changes:
        option["recent_change_on_this_film"] = [
            {
                **_credit(change.credit_type, change.job, None),
                "change": change.change,
                "changed_at": change.changed_at.date().isoformat(),
            }
            for change in candidate.changes
        ]
    return option


def _credit(credit_type: str, job: str | None, department: str | None) -> dict:
    """A credit rendered the way the model is asked to read it: the job where TMDB has one,
    and the credit type where it does not. `cast` with no job is a performer, which is the
    common case and is worth saying in a word rather than in a null."""
    rendered: dict = {"job": job or ("actor" if credit_type == "cast" else credit_type)}
    if department:
        rendered["department"] = department
    return rendered


def parse_tiebreak_reply(raw: str, *, option_count: int) -> TiebreakReply:
    """Read one closed-set answer, classifying it rather than trusting it.

    Lenient about the envelope and strict about the answer. The envelope may arrive wrapped
    in prose or a markdown fence, and the option may arrive as a number or as a string
    holding one, because those are spelling differences between providers rather than
    different answers. The *range* is not negotiable: an "n" the options list does not offer
    is `out_of_list` and decides nothing.

    "None" is accepted in the spellings a model actually uses for it — a JSON `null`, and the
    words in `_NONE_WORDS` — because each is the answer the prompt asks for most often, and
    reading one of them as unparseable would push a correct decline onto the human queue. An
    *empty* option is not one of them; see `_NONE_WORDS`.
    """
    try:
        reply = json.loads(_extract_json_object(raw))
    except (ValueError, TypeError):
        return TiebreakReply(unparseable=True)
    if not isinstance(reply, dict) or "option" not in reply:
        return TiebreakReply(unparseable=True)

    reason = _reason(reply.get("reason"))
    answer = reply["option"]
    if answer is None or (isinstance(answer, str) and answer.strip().lower() in _NONE_WORDS):
        return TiebreakReply(reason=reason, answered_none=True)
    try:
        option = int(str(answer).strip())
    except ValueError:
        return TiebreakReply(reason=reason, unparseable=True)
    if not 1 <= option <= option_count:
        return TiebreakReply(reason=reason, out_of_list=True)
    return TiebreakReply(option=option, reason=reason)


# The words a model reaches for when it means "not one of these". The empty string is
# deliberately **not** among them: `{"option": ""}` is a reply that lost its answer, not a
# considered decline, and reading it as one would stamp `unlinked` and `resolved_at` on the
# mention and never ask again — coercing a defective reply into a permanent decision, which is
# the one thing this module's closed set exists to forbid. It falls through to `unparseable`.
_NONE_WORDS = frozenset({"none", "null", "nobody"})


def _reason(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_REASON_MAX]


def _extract_json_object(text: str) -> str:
    """Pull the JSON object out of a reply that may be wrapped in prose or markdown fences —
    the same belt-and-braces every other stage keeps, since `json_object` is a request no
    provider guarantees and Anthropic ignores outright (`llm.types.Prompt`)."""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end < start:
        return text
    return text[start : end + 1]


async def ask_tiebreak(
    *,
    client: Completer,
    model: str,
    question: TiebreakQuestion,
    options: Sequence[Candidate],
    calls: CallLog,
) -> TiebreakReply:
    """One closed-set call for one mention, recorded into `calls` with its parse outcome.

    A reply that arrived and could not be used is recorded `parse_ok=False` and returned, not
    raised: the mention has a deterministic decision already and it stands. A call that never
    returned raises, and the caller treats it as it treats a failed TMDB read — the mention
    keeps until the next run rather than being recorded as answered.
    """
    result = await client.complete_call(
        model=model, prompt=build_tiebreak_request(question, options), calls=calls
    )
    reply = parse_tiebreak_reply(result.text, option_count=len(options))
    calls.set_parse_ok(reply.usable)
    if not reply.usable:
        log.warning(
            "resolve: unusable tiebreak answer for %r (out_of_list=%s, raw prefix: %r)",
            question.mention.name_as_written,
            reply.out_of_list,
            result.text[:200],
        )
    return reply
