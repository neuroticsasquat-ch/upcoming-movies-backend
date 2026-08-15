"""Scoring and selection of a story's candidate films. Pure — no I/O.

Given a headline, a dek, and the index built once per run by `index.py`, this returns the
candidate set the model will be shown for that story: every film scoring at or above a
threshold **T**, capped at the **K** best.

**Scoring** is the fraction of a title's significant tokens present in the story text,
taken at the film's best title — primary, original, or alternative. The squash-fold grants
full credit when a title's folded form (of at least `SQUASH_FOLD_MIN_CHARS`) appears as a
substring of the folded story text; that is what reaches tracked "Nagabandham" from a
headline that writes "Naga Bandham", which shares no token with it (ADR-0008).

The fraction is taken over a title's **distinct** tokens. A title that repeats a word is
not twice as hard to match, and a list-based denominator would make it so.

**An empty result is a legitimate outcome**, not an error: it is a zero-candidate
rejection downstream — no model call, `link_note = 'no-candidates'` (ADR-0009). That path
is the majority of the stage's workload on the measured corpus, so the empty set is the
common case rather than the edge one.

**Ranking is deliberately plain.** Misses are score-zero misses rather than ranking
failures, so there is nothing for a more elaborate ranker to recover. What the order does
have to be is *deterministic* — score, then title, then id — because the offline harnesses
record the rank the expected film landed at, and a rank that moved between runs for no
reason would be unreadable.

**The constants below are tuned, not assumed** — retuned at NEU-1135 after the writers
tranche and the 2026-08-13 sweep took the catalog from 1,997 to **2,695 active films**,
1.35x on top of NEU-1088's 1.6x. Two specs bear on this and they are different documents:
the *candidate-retrieval design spec* §5.14 holds the tuning record and the method (§5.13 is
NEU-1088's), and the *undated-film discovery project spec* §7.2 is what made the retune a
ticket in that project rather than a follow-on.

The measurement is `scripts/tune_retrieval.py` over **two nested windows**, as NEU-1088 ran
it: **4,820 stories / 175 picks** in the trailing 21 days, and **87,103 stories / 337 picks**
in the trailing 45. The 21-day window is the decision grid — its story mix is the one
production now sees — and the 45-day window exists to confirm the deepest-pick rank, which is
the one number a short window could get wrong by luck:

- **T = 0.5 stays, but it is a cliff edge rather than the top of a flat region.** That
  reframing is the finding. Recall is 0.983 at every T from 0.25 to 0.5, and *every* T above
  0.5 — 0.501, 0.55, 0.571, 0.6 alike — lands on the same collapsed measurement: mean set
  size 17.83 to 2.19, zero-candidate 0.2% to **18.4%**, which breaches the 10% hard ceiling
  outright. There is no intermediate T, because the score is a fraction of a title's
  *distinct* tokens: the dominant mass of candidates scores exactly 0.5 (one token of a
  two-token title), and stepping past 0.5 by any amount discards all of them at once. The
  45-day window agrees: flat at 0.988 to T=0.5, then 23.9% zero-candidate at 0.6.
- **So T is not a lever against catalog growth**, which is what NEU-1135 opened by assuming
  it was — a higher threshold does admit fewer collisions, but the only threshold available
  above 0.5 costs 18.4% of the corpus its candidates. **K is the only live lever**, and K
  bounds the tail rather than the bulk. Do not re-derive this by re-running the coarse grid;
  it reads as a gap between 0.5 and 0.6 that a finer sweep might fill, and a finer sweep was
  run here and does not.
- **K = 47 is the deepest observed pick, rank 43, plus the same named margin of 4.** Both
  windows put that pick at rank 43 — they are nested, so that is one observation confirmed
  against five times the traffic, not two independent ones, which is exactly the standing
  NEU-1088's matching 21/45-day agreement on rank 31 had. The rule
  is unchanged — smallest K that loses no pick, then margin, then price the margin. The
  margin costs **+9 input tokens per story**: K=43 offers 17.58 candidates per story against
  K=47's 17.68, at **92 tokens per rendered candidate** counted against the tokenizer (not
  the four-characters-per-token estimate, which reads 22% low here). The whole move from
  K=35 costs **+45 tok/story**.

**Rank 43 is a lone outlier and is honoured deliberately.** Pick ranks are median 1, p90 3,
p95 4, p99 8, and the deepest twelve run 4, 4, 4, 4, 4, 5, 5, 5, 5, 6, 8, **43**. A rule that
trimmed it would put K near 12. It survives because of *what* it is: a story about "The
Daniels' new sci-fi movie" linked to **Untitled Daniels Event Film**, scoring the floor 0.500
in a 47-film set and ranking below coincidental full-credit matches like "Once". It was
linked 2026-07-25, before the retrieval cutover, so it is roster-authored ground truth rather
than a verdict retrieval wrote itself (§3.4). And a title of the form "Untitled <Director>
Project" carries no distinctive tokens by construction, so it *always* scores at the floor and
ranks at the bottom of whatever collision set the headline draws. **138 films now carry such a
title, 126 of them undated** — this is the population the undated-film expansion exists to
admit, so picks of this shape get commoner as the sweep runs, not rarer. Trimming the outlier
would be discarding the signal the project was built to produce.

**Headroom is now almost pure tail control, and cheap for it.** The uncapped mean is 17.83
candidates per story, so K=47 already delivers **99.2%** of what no cap at all would offer:
raising K no longer buys prompt size across the corpus, only on the shrinking minority of
stories that saturate. That is the opposite of the position at NEU-1088, where the same move
was priced as a real cost, and it is why the recall rule survives a deepest-pick rank that
moved 31 to 43. Saturation is still **reported, not chased**: over the same corpus and the
same catalog, K=47 saturates **1.89%** of stories where K=35 saturates **6.83%**, and that
number is watched rather than targeted (`retrieval/health.py`'s soft tier). Compare those two
and nothing else — NEU-1088's 1.8% is a different catalog *and* a different K, so reading
1.8% against 1.89% as "the floor barely moved" would be crossing two variables at once. What
is true is narrower and is the reason the warn rate survives untouched: the floor K happens
to buy landed in nearly the same place twice, for different reasons each time.

**The grid's saturation figure is better calibrated than NEU-1088 concluded.** That module
warned it runs optimistic by half again, on the one comparison then available. A second,
cleaner comparison exists now: at K=35 over this catalog the grid reads 6.83% where the
2026-08-13 09:05 production run recorded **7.61%** (n=184) — optimistic by ~11%, not ~55%.
The earlier gap was a 16-hour story slice sampling the tail differently, not a standing bias.

That 7.61% is the **pre-retune breach that opened NEU-1135**, running at the old K=35 — not a
post-retune reading, and emphatically not the green one the ticket's last acceptance criterion
waits on. Expect prod near **2.1%** at K=47; until a 09:05 run over the post-writers catalog
actually returns it, the cast tranche stays closed.

**Expect to do this again.** The cast tranche (NEU-1090, +2,341 films) roughly doubles the
catalog and will move K again — but note what it cannot move: T has no next step, so a fourth
pass has one lever and not two. These constants are set from today's catalog rather than from
an extrapolation of that one, deliberately: the growth curve is a predictor, not a promise,
and setting live constants from it is what NEU-1001 existed to stop doing (spec §5.13).
"""

from dataclasses import dataclass
from uuid import UUID

from upmovies.link.retrieval.index import CandidateIndex, IndexedFilm
from upmovies.link.retrieval.normalize import significant_tokens, squash_fold

# Tuned against production traffic — see the module docstring. `config.Settings` carries the
# same two values so they can be moved without a deploy; a test pins the pair together.
# Last re-derived at NEU-1135, over the post-writers-tranche catalog.
DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_CANDIDATE_LIMIT = 47


@dataclass(frozen=True)
class ScoredCandidate:
    """One film offered for a story, with the score that earned it its place.

    The whole `IndexedFilm` is carried, not just its id: it holds the display fields
    rendering needs, so no second round-trip to the catalog is required."""

    film: IndexedFilm
    score: float


@dataclass(frozen=True)
class CandidateSet:
    """A story's retrieval result: everything that cleared T, and the K actually offered.

    The full scored list is the stored state and `candidates` is a view onto it, rather
    than the two being carried side by side. That is what keeps cap saturation observable
    — the offered set alone cannot tell a story with exactly K matches from one with a
    hundred — and it means the type cannot be constructed into disagreeing with itself."""

    scored: tuple[ScoredCandidate, ...]
    limit: int

    @property
    def candidates(self) -> tuple[ScoredCandidate, ...]:
        """The films actually offered to the model, best-first — `scored` under the cap."""
        return self.scored[: self.limit]

    @property
    def is_empty(self) -> bool:
        """True when nothing was retrieved — a zero-candidate rejection (ADR-0009)."""
        return not self.scored

    @property
    def over_threshold(self) -> int:
        """How many films cleared the threshold, counted *before* the cap."""
        return len(self.scored)

    @property
    def saturated(self) -> bool:
        """True when the cap discarded a film that had cleared the threshold.

        The retrieval-health signal: a story whose candidate set is being truncated is
        one where the model may never see the right film, however well it scored."""
        return len(self.scored) > self.limit

    @property
    def film_ids(self) -> tuple[UUID, ...]:
        """The offered films, best-first — the set the model's reply indexes into."""
        return tuple(c.film.film_id for c in self.candidates)

    def film_id_for_index(self, index: object) -> UUID | None:
        """The film at 1-based `index` in *this story's* offered set, or None.

        Story-local where the deleted roster's index was global, which is why an
        out-of-list reply is expressible at all: numbering restarts at 1 per story, so a
        number that overruns this set names no film here even when it names one in another story's
        (spec §4.2). None is a rejection upstream, never a coercion to a neighbouring
        candidate — `link` is lossy, and a wrong link is worse than no link.

        `bool` is excluded explicitly: `True` is an `int` in Python and would otherwise
        resolve to the first candidate, turning a malformed reply into a confident link.
        The cap applies — a film the model was never shown must not be nameable."""
        if isinstance(index, bool) or not isinstance(index, int):
            return None
        if 1 <= index <= len(self.candidates):
            return self.candidates[index - 1].film.film_id
        return None

    def rank_of(self, film_id: UUID) -> int | None:
        """The 1-based position of `film_id` in the offered set, or None if not offered.

        Rank is a position in what the model was actually shown, so a film the cap
        discarded ranks nowhere even though it scored."""
        for rank, candidate in enumerate(self.candidates, start=1):
            if candidate.film.film_id == film_id:
                return rank
        return None

    def score_of(self, film_id: UUID) -> float | None:
        """The score of `film_id`, or None if it never cleared the threshold.

        Answers for films the cap discarded as well as for those offered — that pairing
        is what the recall harnesses need. An expected film with a score but no rank was
        lost to the cap; one with neither was a lexical miss, which is a different failure
        with a different fix (§3.1, §4.5)."""
        for candidate in self.scored:
            if candidate.film.film_id == film_id:
                return candidate.score
        return None


def score_film(film: IndexedFilm, story_tokens: frozenset[str], *, rescued: bool) -> float:
    """`film`'s score against a story: its best title's token fraction, or 1.0 if rescued.

    `rescued` is the squash-fold verdict, resolved once per story against the index's
    rescue folds rather than re-folded per film here — a fold match is full credit, so
    there is no better token score to look for once it holds."""
    if rescued:
        return 1.0
    best = 0.0
    for title in film.titles:
        distinct = frozenset(title.tokens)
        # A title tokenizing to nothing ("The A of") has no fraction to take. 0/0 is not
        # full credit — a title that says nothing must not match every story.
        if not distinct:
            continue
        best = max(best, len(distinct & story_tokens) / len(distinct))
    return best


def select_candidates(
    index: CandidateIndex,
    *,
    headline: str,
    dek: str | None = None,
    threshold: float = DEFAULT_SCORE_THRESHOLD,
    limit: int = DEFAULT_CANDIDATE_LIMIT,
) -> CandidateSet:
    """The candidate set for one story: films scoring ≥ `threshold`, best `limit` kept."""
    # Tokenization splits on word boundaries, so the fields are safe to join for it.
    story_tokens = frozenset(significant_tokens(f"{headline} {dek}" if dek else headline))
    # The fold is *not* safe to join: `squash_fold` strips whitespace, so folding the
    # joined text fuses the headline's last word onto the dek's first and invents a
    # substring that neither field contains — a headline ending "shot at night" beside a
    # dek opening "Watchmakers assemble" would rescue "Nightwatch" at full credit.
    # Folding each field on its own keeps every match inside one real run of text.
    story_folds = tuple(squash_fold(part) for part in (headline, dek) if part)

    # Both routes into the catalog, resolved once. The token index narrows to films
    # sharing a word with the story; the rescue folds catch the films that share none,
    # which is exactly the hole token overlap cannot see.
    rescued = {
        f.film_id
        for f in index.rescue_folds
        if any(f.folded in story_fold for story_fold in story_folds)
    }
    scorable_film_ids = index.films_for_tokens(story_tokens) | rescued

    scored: list[ScoredCandidate] = []
    for film_id in scorable_film_ids:
        film = index.film(film_id)
        if film is None:  # pragma: no cover — ids come from the index itself
            continue
        score = score_film(film, story_tokens, rescued=film_id in rescued)
        if score >= threshold:
            scored.append(ScoredCandidate(film=film, score=score))

    # Title then id after score, so the order never depends on set iteration — the recall
    # harnesses record ranks, and a rank that moves between runs is unreadable.
    scored.sort(key=lambda c: (-c.score, c.film.title, str(c.film.film_id)))
    return CandidateSet(scored=tuple(scored), limit=limit)
