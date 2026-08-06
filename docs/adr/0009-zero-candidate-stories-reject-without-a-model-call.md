# A story with no candidates is rejected without a model call

**Status:** accepted — implementation tracked in *backlotter: Entity-Linking Candidate Retrieval* (M3)

## Context

Under candidate retrieval each story is narrowed to the films scoring at or above a threshold
`T`, capped at `K`. Some stories clear nothing. What happens to them is the single most
consequential decision in the project, because it decides *who owns the `no-match` verdict*.

Today the model owns it exclusively, and owning it is most of its job — the linker prompt says
so outright: *"Most stories are no-match … Returning no-match is expected and correct."* A
threshold rule moves that verdict, for the stories it silently excludes, to a lexical
comparison the model never sees.

Two measurements frame the trade. Against the labeled fixture, **63% of stories match no film
at all** under a full-title-token rule and **29% under a 34%-overlap rule** — so this is not an
edge case, it is the majority of the stage's workload. Against the same fixture, the cost in
recall is **one item in 93**, and that one was a whitespace variant (`"Naga Bandham"` vs the
tracked `"Nagabandham"`) since fixed by the squash-fold.

The aggravating factor is that `link` is the repo's only **lossy stage** (CONTEXT.md): a story
it fails to link ages out of the recency window and is never retried. A retrieval bug does not
degrade a run, it destroys stories.

## Decision

A story whose candidate set is empty is rejected with `link_status = 'rejected'` and
`link_note = 'no-candidates'`, with **no LLM call**. The lexical rule is accepted as the
authority on `no-match` for those stories.

Three constraints make that safe enough to accept, and all three are part of the decision
rather than commentary on it:

1. **The rejection is distinguishable and therefore recoverable.** `no-candidates` is its own
   note, not folded into `no-match`, so stories lost to a retrieval bug can be found and
   re-run after a fix — within the recency window.
2. **Zero-candidate rejects do not count as `processed`.** `StageCounts.processed` for `link`
   keeps meaning *"stories the classifier decided"*. Without this, a total Anthropic outage
   would report ~30% processed, `total_failure()` would return `False`, the run would finalize
   `succeeded`, and the remaining stories would age out — silently disabling the one guard
   protecting the one lossy stage.
3. **The zero-candidate rate is guarded.** A breach past a hard threshold, subject to a
   minimum-denominator rule, finalizes the run `failed` and pings the deadman. See ADR-0010.

## Considered alternatives

- **Pure top-K with no threshold.** Always show the `K` best films however poor the match, so
  the model remains the sole `no-match` authority. Rejected: it forfeits the entire no-call
  win — the majority of stories — and hands the model `K` irrelevant films for every story
  about nothing tracked, which is a *precision* risk, not a neutral cost.
- **A minimum floor of `M` candidates.** Same objection: every story still costs a call.
- **Sweeping zero-candidate stories into one cheap title-only batch call.** Rejected: a
  compact roster prefix is exactly the artefact this project deletes, and it would grow back
  with the catalog.
- **Leaving zero-candidate stories `pending`.** Rejected as indistinguishable in practice —
  they age out of the recency window regardless — while losing the queryable `no-candidates`
  note that makes recovery possible.

## Consequences

- Most of the `link` stage's LLM calls disappear. Cost was never the justification for this
  project, but this is where the saving actually lands.
- Narrowing the candidate set raises the pull toward a **false positive**: shown one film and a
  headline about that film's untracked sibling, the model has nothing correct to point at. The
  prompt's four franchise-trap paragraphs (`link/linker.py:71-94`) are therefore **retained in
  full**, and each candidate is rendered with director, top-billed cast and collection name —
  disambiguation the 1,200-film prefix could never afford. The offline F1 gate watches
  precision specifically, not just recall.
- Retrieving *untracked* films as labeled distractors would answer the franchise trap
  structurally. It was rejected for now because `catalog.film` only holds what TMDB discover
  surfaced, so coverage would be arbitrary — the prompt's own canonical example, the released
  *"The Housemaid"* beside the tracked *"The Housemaid's Secret"*, is not in the catalog at all.
