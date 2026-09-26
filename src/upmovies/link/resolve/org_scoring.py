"""Scoring and routing for one organisation mention (EF-12).

`scoring.py`'s second half, for studios and franchises: given the candidate set
`org_candidates.py` built for one `news.story_entity` row, decide which of them the story
named — or decide that none of them is, or that the answer is too close to call. Pure, like
the person scorer and for the same reason: every input is a value the caller has already read,
so the whole decision table is testable without a database or a network.

**The routing vocabulary, the two thresholds and the INV-6 cap are the person pass's, reused
rather than mirrored** — `Path`, `Thresholds` and `cap_confidence` are imported from
`scoring`. One operator-facing pair of numbers (`RESOLVE_ACCEPT_FLOOR`,
`RESOLVE_ACCEPT_MARGIN`) governs both arms, which is only honest if both arms put their scores
on the same scale, so the two weights below are pinned to the person weights they correspond
to rather than tuned independently.

## The score

    score = name_quality × (W_ORG_NAME + attached)

Multiplicative on the name for `scoring.py`'s reason — the name is the claim and everything
else is corroboration — and with **one** corroborating feature rather than five, because that
is all the catalog knows about an organisation on a film. A company either holds a row on the
film or it does not; there is no billing order, no department, no birth date and no
filmography to overlap. The third feature EF-12 names, the popularity prior, is a *tiebreak*
and is deliberately not a term of the score: see `rank_org_candidates`.

## The name gate

Organisations get a narrower gate than people, by three deliberate omissions.

There is **no initials tier**. "WB" for "Warner Bros." and "Uni" for "Universal" are exactly
the coercion that produces a confident wrong studio, and unlike the person side there is no
department, no filmography and no age for such a match to be rescued or contradicted by — an
initials match would decide on the popularity prior alone, which D-21 forbids.

There is **no generational-suffix tier**; "Jr." is not a thing a studio is called.

What there *is* instead is a **reduced** tier scored equal to `normalized`, which drops
punctuation and a trailing form word — "Collection", "Saga", "Series" for a franchise, "Inc",
"Ltd", "GmbH" and the other legal forms for a company. TMDB names every collection "<X>
Collection" and no trade story ever writes that word, so removing it is normalization rather
than a fuzzy match, and scoring it as a weaker one would mean the ordinary franchise mention
never cleared `ACCEPT_FLOOR`. Words like "Pictures", "Studios" and "Entertainment" are
pointedly **not** reduced: "Warner Bros. Pictures" and "Warner Bros. Television" are two
different TMDB companies, and stripping them would merge the pair into one wrong answer.
"""

from dataclasses import dataclass
from typing import Any

from upmovies.link.resolve.org_candidates import COLLECTION, COMPANY, OrgCandidate
from upmovies.link.resolve.scoring import (
    ACCEPT_FLOOR,
    ACCEPT_MARGIN,
    W_CREDITED,
    W_NAME,
    Path,
    Thresholds,
    cap_confidence,
)
from upmovies.news.subject_key import normalize_name

W_ORG_NAME = W_NAME
"""The name term, pinned to the person pass's. `ACCEPT_FLOOR` is calibrated against `W_NAME`
alone — the weakest thing that may accept is one normalized name match and nothing
contradicting it, 0.55 × 0.95 = 0.5225 — and that calibration has to hold for both arms,
because the floor is one setting."""

W_ORG_ATTACHED = W_CREDITED
"""The one corroborating term, pinned to the person pass's "already credited" weight, which it
is the exact analogue of. Larger than `ACCEPT_MARGIN` on purpose and by inheritance: one
candidate already being on the film while its namesake is not *should* be enough to separate
them."""

ORG_NAME_MATCH_NONE = "none"
"""The quality that zeroes the whole score — the name the story wrote is not this candidate's.
Named for the same reason `scoring.NAME_MATCH_NONE` is: it is the one answer that makes every
other feature unable to move the decision."""

_ORG_NAME_QUALITY = {
    "exact": 1.0,
    "normalized": 0.95,
    # Equal to `normalized`, not below it. See the module docstring: a trailing "Collection"
    # is TMDB's cataloguing convention rather than a difference between two names.
    "reduced": 0.95,
    "none": 0.0,
}

_COLLECTION_FORM_WORDS = frozenset({"collection", "saga", "series", "franchise", "trilogy"})
"""Trailing words TMDB adds to a franchise's name that no trade story writes. Deliberately
short: "Universe" is absent because "Marvel Cinematic Universe" is how the thing is named in
prose, not a catalogue suffix, and dropping it would fold it into "Marvel"."""

_COMPANY_FORM_WORDS = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "llp",
        "ltd",
        "limited",
        "co",
        "corp",
        "corporation",
        "company",
        "gmbh",
        "ag",
        "sa",
        "srl",
        "bv",
        "nv",
        "ab",
        "oy",
        "plc",
        "pty",
    }
)
"""Trailing legal forms, which a trade story omits and a TMDB record sometimes carries. Only
legal forms: a trading word like "Pictures" or "Studios" distinguishes real sibling companies
and is never removed."""

_PUNCTUATION = ".,'’&-–—/()[]:!?\"“”"


@dataclass(frozen=True)
class OrgMention:
    """What the extraction pass reported about one organisation (EF-12), as scoring reads it.

    A value rather than the `story_entity` row itself: `title_mentioned` and `event_type` live
    inside that row's `features` JSON rather than in columns of their own, and a pure module
    should not be the thing that knows how to dig them out. `scoring.Mention` without `role`
    and `department`, which are person facts, and with the `kind` that decided which catalogue
    the candidates came from.
    """

    name_as_written: str
    kind: str
    title_mentioned: str | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class ScoredOrgCandidate:
    """One candidate with its score and the features that produced it.

    `features` is kept whole rather than reduced to the number it sums to, for
    `ScoredCandidate`'s reason: `/admin/resolution` (D-25) exists to show *why* a mention went
    where it went, and a score with no breakdown behind it is as inspectable as no score.
    """

    candidate: OrgCandidate
    score: float
    features: dict[str, Any]

    @property
    def entity_id(self) -> int:
        return self.candidate.entity_id


@dataclass(frozen=True)
class OrgDecision:
    """The routing outcome for one organisation mention, ready to be written to
    `story_entity`.

    `scoring.Decision`'s shape with `entity_id` in place of `person_id` and its own ranked
    type. Not the same dataclass reused: that one's id field is named for the catalogue it
    reads, and a `person_id` carrying a company id would be a lie every reader of the
    organisation arm had to know to discount.
    """

    path: Path
    entity_id: int | None
    confidence: float
    ranked: list[ScoredOrgCandidate]
    features: dict[str, Any]

    @property
    def accepted(self) -> bool:
        return self.path is Path.ACCEPTED


def org_name_match(name_as_written: str, candidate: OrgCandidate) -> str:
    """How well the story's spelling of the name matches this candidate's, as the strongest of
    the qualities in `_ORG_NAME_QUALITY`.

    Both of TMDB's spellings are tried where it has two: a collection carries an
    original-language `original_name`, which is the form a trade quoting a foreign
    production's press release often writes. A company record carries one name only.
    """
    written = name_as_written.strip()
    spellings = [s for s in (candidate.name, candidate.original_name) if s]
    if any(written == s.strip() for s in spellings):
        return "exact"
    normalized = normalize_name(written)
    folded = [normalize_name(s) for s in spellings]
    if any(normalized == f for f in folded):
        return "normalized"
    reduced = _reduce(normalized, candidate.kind)
    if reduced and any(reduced == _reduce(f, candidate.kind) for f in folded):
        return "reduced"
    return ORG_NAME_MATCH_NONE


def score_org_candidate(candidate: OrgCandidate, *, mention: OrgMention) -> ScoredOrgCandidate:
    """Score one candidate against one organisation mention, keeping every feature that fed
    the number."""
    match_kind = org_name_match(mention.name_as_written, candidate)
    quality = _ORG_NAME_QUALITY[match_kind]
    score = max(0.0, quality * (W_ORG_NAME + (W_ORG_ATTACHED if candidate.attached else 0.0)))
    return ScoredOrgCandidate(
        candidate=candidate,
        score=round(score, 4),
        features={
            "name_match": match_kind,
            "name_quality": quality,
            "attached": candidate.attached,
            "from_search": candidate.from_search,
            # Logged as the fact it is, beside the features that scored, so a human reading
            # the queue can see the prior that ordered two equal scores.
            "catalog_reach": candidate.catalog_reach,
        },
    )


def rank_org_candidates(
    candidates: list[OrgCandidate], *, mention: OrgMention
) -> list[ScoredOrgCandidate]:
    """Every candidate scored, best first.

    Ties break on the catalog reach and then on the TMDB id: the first is EF-12's popularity
    prior, the second only so the order never depends on which source happened to yield a
    candidate first. Neither touches `score`, so neither can move a mention across a
    threshold — the separation `scoring.py` makes structural, kept structural here.
    """
    scored = [score_org_candidate(c, mention=mention) for c in candidates]
    return sorted(scored, key=lambda s: (-s.score, -s.candidate.catalog_reach, s.entity_id))


def resolve_org_mention(
    candidates: list[OrgCandidate],
    *,
    mention: OrgMention,
    link_confidence: float | None,
    search_empty: bool = False,
    thresholds: Thresholds | None = None,
) -> OrgDecision:
    """Score, rank and route one organisation mention, and cap its confidence at the story's
    (INV-6).

    `resolve_mention`'s three-way route, with one difference in the bottom branch. For a person
    a bare empty search is not enough to claim `not_in_tmdb` — a story may write a name TMDB
    knows and rank nobody for it — so that route also requires the mention to claim an
    attachment (`_claims_a_debut`). An organisation needs no such guard: `/search/company` and
    `/search/collection` match on the organisation's own name rather than on a person's, TMDB
    returns every company whose name contains the query, and an empty answer to a studio name
    means TMDB holds no such studio. So an empty search below the floor is `not_in_tmdb`
    (INV-8) and anything else below it is `unlinked`.

    """
    limits = thresholds or Thresholds()
    ranked = rank_org_candidates(candidates, mention=mention)
    best = ranked[0].score if ranked else 0.0
    # A candidate scoring zero is not a rival — its name is not the story's name — so the
    # runner-up floors at zero and a single plausible candidate is measured against nothing.
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    margin = round(best - runner_up, 4)

    if best < limits.accept_floor:
        path = Path.NOT_IN_TMDB if search_empty else Path.UNLINKED
        entity_id = None
    elif margin >= limits.accept_margin:
        path, entity_id = Path.ACCEPTED, ranked[0].entity_id
    else:
        path, entity_id = Path.TIEBREAK, None

    return OrgDecision(
        path=path,
        entity_id=entity_id,
        confidence=cap_confidence(best, link_confidence),
        ranked=ranked,
        features={
            "kind": mention.kind,
            "best_score": best,
            "runner_up_score": runner_up,
            "margin": margin,
            "accept_floor": limits.accept_floor,
            "accept_margin": limits.accept_margin,
            "search_empty": search_empty,
            "candidate_count": len(ranked),
            "cache_hit": False,
        },
    )


def org_candidate_log(scored: ScoredOrgCandidate) -> dict:
    """One candidate as `story_entity.candidates` records it: who they are, what they scored,
    and every feature behind it. `/admin/resolution` reads exactly this, which is why the
    whole shortlist is kept and not only the winner — an unlinked mention's value to a human
    is the near-misses it rejected."""
    return {
        "entity_id": scored.entity_id,
        "kind": scored.candidate.kind,
        "name": scored.candidate.name,
        "score": scored.score,
        "features": scored.features,
    }


def _reduce(normalized: str, kind: str) -> str:
    """A normalized name with its punctuation dropped and one trailing form word removed.

    One word, not every trailing form word: "Pictures Collection Collection" is not a name, and
    a loop here would keep eating real words off a short franchise title.
    """
    words = _COLLECTION_FORM_WORDS if kind == COLLECTION else _COMPANY_FORM_WORDS
    tokens = [t for t in _depunctuate(normalized).split() if t]
    if len(tokens) > 1 and tokens[-1] in words:
        tokens = tokens[:-1]
    return " ".join(tokens)


def _depunctuate(value: str) -> str:
    """Punctuation replaced by spaces, so "Warner Bros." and "Warner Bros" reduce alike and
    "Fast & Furious" does not glue into one token."""
    return "".join(" " if ch in _PUNCTUATION else ch for ch in value)


__all__ = [
    "ACCEPT_FLOOR",
    "ACCEPT_MARGIN",
    "COLLECTION",
    "COMPANY",
    "ORG_NAME_MATCH_NONE",
    "W_ORG_ATTACHED",
    "W_ORG_NAME",
    "OrgDecision",
    "OrgMention",
    "ScoredOrgCandidate",
    "org_candidate_log",
    "org_name_match",
    "rank_org_candidates",
    "resolve_org_mention",
    "score_org_candidate",
]
