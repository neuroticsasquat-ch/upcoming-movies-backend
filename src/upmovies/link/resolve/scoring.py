"""Scoring and routing for one person mention (D-21, D-23, D-24).

The second half of person resolution: given the candidate set `candidates.py` built for one
`news.story_person` mention, decide which of them the story named — or decide that nobody
here is, or that the answer is too close to call. Pure, and deliberately so: every input is
a value the caller has already read, so the whole decision table is testable without a
database or a network, and `pipeline.py` is left with reading, persisting and counting.

**Deterministic Python, no model.** D-21 is explicit that candidates and scoring are
arithmetic over facts, not a judgement call handed to an LLM. The narrow band where the
arithmetic genuinely cannot separate two people is the *only* part that reaches a model, and
it reaches it later: this module routes that band to `tiebreak` and stops (NEU-1364 consumes
the queue). Scoring something it cannot decide would not make the decision better, only
unreviewable.

## The score

A weighted sum, gated on the name:

    score = name_quality × (W_NAME + credited + in_change_stream + department + filmography)

Multiplicative rather than additive because **the name is the claim**. Everything else is
corroboration, and corroboration of a name the story never wrote is not evidence about this
mention at all: a film's director is `credited` and `in_change_stream` and matches the
extracted department, and none of that makes them the person a story about their film named.
A candidate whose name does not match scores zero however well the rest lines up.

The bonuses are evidence-positive: a feature contributes only when the fact it reads is
actually present. An extraction that emitted no `department` earns no department bonus rather
than a neutral half of one — absence of evidence is not evidence — which is also what lets
`ACCEPT_FLOOR` be calibrated against `W_NAME` alone. The department feature is the one that
can go negative, because a *contradiction* is evidence: "Ludwig Göransson, the film's
composer" against a candidate whose only tie to the film is an acting credit is a reason to
believe less, not merely no reason to believe more.

**Popularity is not in the sum.** D-21 calls it a tiebreak and never a primary signal, so it
is kept structurally out of the score rather than kept small: it orders candidates that score
*identically*, deciding who heads the shortlist a human or the resolve stage reads, and it
can never carry a candidate over the floor or widen a margin. Two same-named people are
exactly the case where the popular one is most likely to be the wrong answer — the
wrong-Chris-Evans failure M4 exists to make impossible — and there the tie is what routes the
mention to `tiebreak`, popularity or no popularity.

## Routing

`ACCEPT_FLOOR` and `ACCEPT_MARGIN` are two different questions and both must be answered:

- Nothing at or above the floor → `unlinked`: no candidate is plausibly this person.
- Above the floor, and clear of the runner-up by `ACCEPT_MARGIN` → `accepted`.
- Above the floor, but inside that band → `tiebreak`, with `person_id` left NULL. The
  candidates and their features are persisted so the decision can be made later from what
  this pass saw rather than re-derived.

The *floor* is not what protects against the wrong Chris Evans — two people with the same
name score the same, which is high — the *margin* is. A lone exact-name match with no rival
accepts on a fairly modest score, which is right: TMDB knows one person by that name and a
trade story about a film they are plausibly on named them.

`not_in_tmdb` is the one route that claims something positive about an absence (INV-8): the
name search came back empty **and** the story named this person as attached to the film. A
person a trade describes as joining a production is a working professional, so TMDB having
nobody by that name is a fact about TMDB's coverage — the debut case — and not the same
answer as "we could not tell which of these people it is". Without the attachment clause it
would also swallow every misspelling and every incidental name (an executive, a rival
producer quoted in passing), which are ordinary `unlinked` mentions.

## INV-6

`resolve_mention` applies the cap itself — `confidence = min(score, story.link_confidence)`
(D-23) — rather than returning a raw score for the caller to remember to bound. A resolution
cannot be more certain than the link it rests on: if we are only 0.7 sure this story is about
this film, we cannot be 0.9 sure about who it named *on that film*.

Absent: the age/alive plausibility feature D-21 lists. It has no input in this repo —
neither `catalog.person` nor anything fetched from TMDB holds a birthday or deathday, which
is why `candidates.py` carries no such fact — so there is nothing here to score. It attaches
at `Candidate` when a schema change supplies one; a feature that reads two always-NULL
columns would report "implausible" for everybody and quietly cost every mention its margin.
"""

import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from upmovies.link.resolve.candidates import Candidate
from upmovies.news.subject_key import normalize_name

# The weights of the four corroborating features, and the name's own share. They sum to 1.0,
# so a perfect name match corroborated every way scores exactly 1.0 and nothing has to be
# clamped from above. Their *relative* sizes are the claim: being credited on the film is the
# strongest of the four (a story about a film naming someone on it is the ordinary case), the
# change stream next, and department agreement and filmography overlap are weaker because
# both are satisfied by a great many people who are not the one the story named.
W_NAME = 0.55
W_CREDITED = 0.15
W_CHANGE_STREAM = 0.10
W_DEPARTMENT = 0.10
W_FILMOGRAPHY = 0.10

# Defaults for the two thresholds, mirrored by `RESOLVE_ACCEPT_FLOOR` /
# `RESOLVE_ACCEPT_MARGIN` in `config.py` — settings, because they are the one part of this
# module an operator may need to move without a deploy, and because M4 ships before there is
# a corpus of resolved mentions to tune them against.
#
# The floor sits just under `W_NAME` × a normalized name match (0.55 × 0.95 = 0.52), which is
# the weakest thing that may accept: TMDB returned one person by that name and nothing
# contradicted it. Anything below that is a name that did not really match.
#
# The margin is deliberately larger than any single corroborating feature is wide relative to
# two same-scoring names, and deliberately smaller than `W_CREDITED`: one candidate being
# credited on the film while their namesake is not *should* be enough to separate them.
ACCEPT_FLOOR = 0.5
ACCEPT_MARGIN = 0.12

# The beats that name a person as attached to the film, which — alongside an extracted role —
# is what makes an empty name search mean `not_in_tmdb` rather than `unlinked`. Every one is a
# type `link.cluster._VALID_TYPES` actually offers the model, because `_mention_event_type`
# drops anything outside that vocabulary before it reaches `features`: a beat listed here that
# the prompt cannot emit would be a route that silently never fires. `crew_attached` is
# deliberately **not** here for that reason — `news.Event`'s CHECK constraint allows it, but
# the extraction prompt does not offer it, so a crew attachment arrives as `announced` or as
# no beat at all and is caught by the role clause instead.
ATTACHMENT_EVENT_TYPES = frozenset({"casting", "announced", "production_start"})

# Generational suffixes, which trade style treats as optional: "Kenneth Branagh Jr." and
# "Kenneth Branagh" are one person written two ways far more often than they are two people.
# Scored below a normalized match rather than equal to it, because occasionally they are two
# people — a father and a son in the same industry is precisely where this form shows up.
_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

_NAME_QUALITY = {
    "exact": 1.0,
    "normalized": 0.95,
    "suffix": 0.85,
    # Well below the others on purpose: "J. Smith" matches every Smith with a J, so an
    # initials match alone scores 0.55 × 0.6 = 0.33 and cannot clear the floor. It accepts
    # only once something else about the candidate corroborates it.
    "initials": 0.6,
    "none": 0.0,
}


class Path(StrEnum):
    """Where a mention ends up — `news.story_person.path`'s vocabulary (D-24)."""

    ACCEPTED = "accepted"
    TIEBREAK = "tiebreak"
    UNLINKED = "unlinked"
    NOT_IN_TMDB = "not_in_tmdb"


@dataclass(frozen=True)
class Thresholds:
    """The two numbers routing turns on, passed as one value so a caller cannot thread the
    floor through and forget the margin."""

    accept_floor: float = ACCEPT_FLOOR
    accept_margin: float = ACCEPT_MARGIN


@dataclass(frozen=True)
class Mention:
    """What the extraction pass reported about one person (D-20), as scoring reads it.

    A value rather than the `story_person` row itself: `title_mentioned` and `event_type`
    live inside that row's `features` JSON rather than in columns of their own, and a pure
    module should not be the thing that knows how to dig them out.
    """

    name_as_written: str
    role: str | None = None
    department: str | None = None
    title_mentioned: str | None = None
    event_type: str | None = None


@dataclass(frozen=True)
class ScoredCandidate:
    """One candidate with its score and the features that produced it.

    `features` is kept whole rather than reduced to the number it sums to: D-25's
    `/admin/resolution` page exists to show *why* a mention went where it went, and a score
    with no breakdown behind it is exactly as inspectable as no score at all.
    """

    candidate: Candidate
    score: float
    features: dict[str, Any]

    @property
    def person_id(self) -> int:
        return self.candidate.person_id


@dataclass(frozen=True)
class Decision:
    """The routing outcome for one mention, ready to be written to `story_person`."""

    path: Path
    person_id: int | None
    confidence: float
    ranked: list[ScoredCandidate]
    features: dict[str, Any]

    @property
    def accepted(self) -> bool:
        return self.path is Path.ACCEPTED


def score_candidate(
    candidate: Candidate,
    *,
    mention: Mention,
    mentioned_tmdb_ids: frozenset[int] = frozenset(),
) -> ScoredCandidate:
    """Score one candidate against one mention, keeping every feature that fed the number."""
    match_kind = name_match(mention.name_as_written, candidate)
    quality = _NAME_QUALITY[match_kind]
    department = department_agreement(mention, candidate)
    overlap = sorted(set(candidate.filmography_tmdb_ids) & mentioned_tmdb_ids)
    bonus = (
        (W_CREDITED if candidate.credited else 0.0)
        + (W_CHANGE_STREAM if candidate.in_change_stream else 0.0)
        + (W_DEPARTMENT * department)
        + (W_FILMOGRAPHY if overlap else 0.0)
    )
    # Clamped from below only: the weights sum to 1.0, so the sum can only exceed it if a
    # future feature is added without re-checking them, while a department contradiction can
    # legitimately drive the corroboration term negative on a weak name match.
    score = max(0.0, quality * (W_NAME + bonus))
    return ScoredCandidate(
        candidate=candidate,
        score=round(score, 4),
        features={
            "name_match": match_kind,
            "name_quality": quality,
            "credited": candidate.credited,
            "in_change_stream": candidate.in_change_stream,
            "from_search": candidate.from_search,
            "department_agreement": department,
            "filmography_overlap": overlap,
            "popularity": candidate.popularity,
        },
    )


def rank_candidates(
    candidates: list[Candidate],
    *,
    mention: Mention,
    mentioned_tmdb_ids: frozenset[int] = frozenset(),
) -> list[ScoredCandidate]:
    """Every candidate scored, best first.

    Ties break on popularity and then on person id: the first is D-21's tiebreak prior, the
    second only so the order never depends on which source happened to yield a candidate
    first. Neither touches `score`, so neither can move a mention across a threshold — see
    the module docstring on why that separation is structural.
    """
    scored = [
        score_candidate(c, mention=mention, mentioned_tmdb_ids=mentioned_tmdb_ids)
        for c in candidates
    ]
    return sorted(scored, key=lambda s: (-s.score, -(s.candidate.popularity or 0.0), s.person_id))


def resolve_mention(
    candidates: list[Candidate],
    *,
    mention: Mention,
    link_confidence: float | None,
    mentioned_tmdb_ids: frozenset[int] = frozenset(),
    search_empty: bool = False,
    thresholds: Thresholds | None = None,
) -> Decision:
    """Score, rank and route one mention, and cap its confidence at the story's (INV-6).

    `search_empty` is the caller's — it knows whether `/search/person` returned nothing,
    which the candidate set alone cannot say: an empty set may equally be a film with no
    credits and a name nobody holds, and a full one may have evicted every search hit at the
    cap. Only the `not_in_tmdb` route reads it.
    """
    limits = thresholds or Thresholds()
    ranked = rank_candidates(candidates, mention=mention, mentioned_tmdb_ids=mentioned_tmdb_ids)
    best = ranked[0].score if ranked else 0.0
    # A candidate scoring zero is not a rival — its name is not the story's name — so the
    # runner-up floors at zero and a single plausible candidate is measured against nothing.
    runner_up = ranked[1].score if len(ranked) > 1 else 0.0
    margin = round(best - runner_up, 4)

    if best < limits.accept_floor:
        path = Path.NOT_IN_TMDB if _claims_a_debut(mention, search_empty) else Path.UNLINKED
        person_id = None
    elif margin >= limits.accept_margin:
        path, person_id = Path.ACCEPTED, ranked[0].person_id
    else:
        path, person_id = Path.TIEBREAK, None

    return Decision(
        path=path,
        person_id=person_id,
        confidence=cap_confidence(best, link_confidence),
        ranked=ranked,
        features={
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


def cap_confidence(score: float, link_confidence: float | None) -> float:
    """INV-6: a resolution is never more certain than the link it rests on (D-23).

    A story with no `link_confidence` is capped by nothing rather than capped to zero — the
    linker records one on every story it links, so the absent case is a story linked before
    that column carried a value, and treating it as "certainty zero" would silently unlink
    every mention on it.
    """
    if link_confidence is None:
        return round(score, 4)
    return round(min(score, link_confidence), 4)


def name_match(name_as_written: str, candidate: Candidate) -> str:
    """How well the story's spelling of the name matches this candidate's, as the strongest
    of the qualities in `_NAME_QUALITY`.

    Both of TMDB's spellings are tried. `original_name` is the one that carries a
    non-anglicized or native-script form, and a trade quoting a foreign production's press
    release often writes that one while `name` holds the form TMDB displays.
    """
    written = name_as_written.strip()
    spellings = [s for s in (candidate.name, candidate.original_name) if s]
    if any(written == s.strip() for s in spellings):
        return "exact"
    normalized = normalize_name(written)
    folded = [normalize_name(s) for s in spellings]
    if any(normalized == f for f in folded):
        return "normalized"
    if any(_strip_suffix(normalized) == _strip_suffix(f) for f in folded):
        return "suffix"
    if any(_initials_match(normalized, f) for f in folded):
        return "initials"
    return "none"


def department_agreement(mention: Mention, candidate: Candidate) -> float:
    """+1 when the story's department or role lines up with what this candidate does, -1 when
    it contradicts it, 0 when either side said nothing.

    The story's `role` is checked first and separately from its `department`, because for
    crew it is the sharper signal: "director" against a `Director` credit on *this film* is a
    far narrower agreement than "Directing" against a Directing department. For a performer
    the role is a character name, which matches no job and simply leaves the role clause
    silent — the department clause is what speaks for them.

    A contradiction needs the candidate's departments to be *known and disjoint* from the
    story's. Credits on this film are the evidence where they exist; `known_for_department`
    stands in when they do not, and it is much the weaker of the two — a working actor who
    directs one film is `known_for_department='Acting'` — so it can agree but never
    contradict.
    """
    jobs = {c.job.casefold() for c in candidate.credits if c.job}
    jobs |= {c.job.casefold() for c in candidate.changes if c.job}
    if mention.role and mention.role.casefold() in jobs:
        return 1.0

    claimed = (mention.department or "").strip().casefold()
    if not claimed:
        return 0.0
    departments = {_credit_department(c.credit_type, c.department) for c in candidate.credits}
    departments.discard(None)
    if departments:
        return 1.0 if claimed in departments else -1.0
    known_for = (candidate.known_for_department or "").strip().casefold()
    return 1.0 if known_for and claimed == known_for else 0.0


def _credit_department(credit_type: str, department: str | None) -> str | None:
    """A credit's department, casefolded. TMDB leaves it off cast entries often enough that
    reading it raw would make a performer's department unknowable; `Acting` is what the
    credit type already means."""
    if department:
        return department.strip().casefold()
    return "acting" if credit_type == "cast" else None


def _claims_a_debut(mention: Mention, search_empty: bool) -> bool:
    """Whether an empty name search means TMDB has no entry for this person (INV-8) rather
    than that we could not identify them.

    The search coming back empty is what rules out a person TMDB holds. What makes their
    absence worth recording is the story claiming they have a job on this film — a trade does
    not announce a casting, or name a director, for somebody who does not exist.

    Either field may carry that claim, and neither takes priority over the other. The
    **extracted role** is the one D-21 names, and any role the extraction reported is such a
    claim by construction: the cluster prompt asks for "the job for crew" or "a character
    name for a performer", both of which say this person works on this film. The **beat**
    catches the case the role misses — a casting story that names somebody without saying
    what part they have — and it is the addition to what the ticket asked for, not a
    narrowing of it. A person named incidentally, with neither a role nor an attachment beat,
    is an ordinary `unlinked` mention: a misspelling, or an executive quoted in passing, whose
    absence from TMDB says nothing.
    """
    if not search_empty:
        return False
    return mention.role is not None or mention.event_type in ATTACHMENT_EVENT_TYPES


def _strip_suffix(normalized: str) -> str:
    """The name without a trailing generational suffix. Punctuation goes with it, so "jr."
    and "jr" are the same token."""
    tokens = normalized.split()
    while len(tokens) > 1 and _depunctuate(tokens[-1]) in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _initials_match(a: str, b: str) -> bool:
    """Whether two normalized names are the same name with one side's given names
    abbreviated — "j.k. simmons" against "jonathan kimble simmons".

    Requires the surname to match outright and at least one side to actually be abbreviated:
    without that clause "john smith" and "james smith" both reduce to initials `j` and would
    match, which is a different person rather than a different spelling. The shorter initial
    sequence may be a prefix of the longer, because a middle name the story omits is the
    ordinary case rather than a disagreement.
    """
    a_tokens, b_tokens = _initial_tokens(a), _initial_tokens(b)
    if not a_tokens or not b_tokens:
        return False
    if a_tokens[-1] != b_tokens[-1] or len(a_tokens[-1]) == 1:
        return False
    a_given, b_given = a_tokens[:-1], b_tokens[:-1]
    if not a_given or not b_given:
        return False
    if not (_is_abbreviated(a_given) or _is_abbreviated(b_given)):
        return False
    a_initials = [t[0] for t in a_given]
    b_initials = [t[0] for t in b_given]
    shorter, longer = sorted((a_initials, b_initials), key=len)
    return longer[: len(shorter)] == shorter


def _initial_tokens(normalized: str) -> list[str]:
    """Name tokens with dotted initial runs split apart, so "j.k." becomes "j", "k" and the
    two sides of a comparison count the same number of given names."""
    tokens: list[str] = []
    for token in _strip_suffix(normalized).split():
        stripped = _depunctuate(token)
        if not stripped:
            continue
        if "." in token and len(stripped) > 1:
            tokens.extend(stripped)
        else:
            tokens.append(stripped)
    return tokens


def _is_abbreviated(given: list[str]) -> bool:
    """Whether a given-name sequence is written as initials rather than in full."""
    return any(len(token) == 1 for token in given)


def _depunctuate(token: str) -> str:
    """The token's letters and digits only — drops the dots, hyphens and apostrophes that
    differ between one outlet's house style and TMDB's."""
    return "".join(c for c in token if unicodedata.category(c)[0] in {"L", "N"})
