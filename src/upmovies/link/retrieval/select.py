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

**The constants below are tuned, not assumed** — retuned at NEU-1088 (design spec §5.13)
after the directors tranche took the catalog from 1,226 to **1,997 active films**, 1.6x.
The measurement is `scripts/tune_retrieval.py` over **4,841 stories** in the trailing
21 days and **191 picks** still in the active catalog:

- **T = 0.5 still tops the flat region.** Recall is identical (0.984) at every T from 0.25
  to 0.5 and falls at 0.6, so the flat region did *not* move with the catalog — it is a
  property of the scoring function, not of catalog size. Taking the highest T that loses
  nothing is what resists catalog growth: what degrades as the catalog expands is
  candidate-set size, and a higher threshold admits fewer collisions. T=0.6 would also take
  the zero-candidate rate from 0.5% to 25.6%, breaching the hard tier outright.
- **K = 35 is the deepest observed pick, rank 31, plus a named margin of 4.** The rule is
  recall, not saturation: find the smallest K that loses no pick, then add margin over it
  and price the margin. Both the 21- and 45-day windows put the deepest pick at rank 31
  (they are nested, so that is one observation, not two). The margin costs **+10 input
  tokens per story** at the measured mean — K=31 offers 12.67 candidates per story against
  K=35's 12.77, at ~98 tokens per rendered candidate. The whole move from K=25 costs
  **+43 tok/story**.

**Headroom is no longer nearly free, which is why the rule is stated in recall.** At
NEU-1001 the mean offered set was 4.23 candidates; at the *same* K=25 it is now 12.33, so at
fixed K the candidate block has roughly tripled — about 415 to 1,210 tokens per story, at the
~98 tokens per rendered candidate measured here. Meanwhile p99 set
size scales with the catalog without bound. Chasing saturation with K would therefore buy
prompt size indefinitely to reach picks that are not there — so **saturation is reported,
not chased**: K=35 saturates 1.8% of stories, down from 7.6% at K=25, and that number is
watched rather than targeted (`retrieval/health.py`'s soft tier). **In production it came in
at 2.79%** on the first run at K=35 (2026-08-12, n=215) — the move off K=25 is confirmed, but
take the grid's saturation figure as a floor rather than an estimate; see that module's note.

**Expect to do this again.** The writers tranche (NEU-1089, +270 films) is small enough
that it likely moves nothing, but the cast tranche (NEU-1090, +2,341) roughly doubles the
catalog and is expected to force a third pass. These constants are set from today's
catalog rather than from an extrapolation of that one, deliberately: the growth curve is a
predictor, not a promise, and setting live constants from it is what NEU-1001 existed to
stop doing (spec §5.13).
"""

from dataclasses import dataclass
from uuid import UUID

from upmovies.link.retrieval.index import CandidateIndex, IndexedFilm
from upmovies.link.retrieval.normalize import significant_tokens, squash_fold

# Tuned against production traffic — see the module docstring. `config.Settings` carries the
# same two values so they can be moved without a deploy; a test pins the pair together.
# Last re-derived at NEU-1088, over the post-directors-tranche catalog.
DEFAULT_SCORE_THRESHOLD = 0.5
DEFAULT_CANDIDATE_LIMIT = 35


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
