# Clustering stays with the LLM

**Status:** accepted

## Context

Grouping a film's stories into events is a similarity problem, so it was proposed
(Linear project *backlotter: Clustering Without the LLM*) to replace the Sonnet cluster
call with a self-hosted embedding + cosine-distance pipeline — motivated by determinism
and debuggability, explicitly **not** cost (the stage runs ~$0.04/day).

Designing it against the code and against production data (window 2026-06-27 → 2026-08-05)
turned up four facts that undermine the premise:

**The LLM call does not go away.** The stage's single call does seven jobs, only one of
which is similarity: it groups stories, attaches them to existing events, and assigns
`event_type`, `confidence`, `cast[]`, `claimed_date`/`region`, and `off_topic`. The last
five are reasoning, not similarity, and `off_topic` alone fired **200 times** in six weeks
as a link-stage backstop. Moving grouping to embeddings shortens the prompt; it does not
remove the nondeterministic step the project exists to remove.

**There is no clustering to speak of.** 67.7% of (film, day) pairs carry exactly one
linked story, and 164 of 199 events span zero days between first and last story. An
agglomerative clusterer would be handed a singleton two-thirds of the time.

**The observed defect rate is ~1 in six weeks, and its cause is data hygiene.** Searching
for split beats (same film, same type, ≤14 days apart) found 227 pairs, but 223 were
`other` — a type the public API never displays (`public/service.py:56`) — and three of the
remaining four were correct splits of genuinely different performers. The one real defect
(four same-day *Dune: Part Three* trailer stories distributed across two events) happened
because both target events were Google-era debris whose original stories had been purged,
left alive by `event_repair` with a frozen `occurred_at`. An embedding pipeline is the
wrong tool for that.

**The baseline corpus is about to be invalidated.** TMDB discover is gated on
`primary_release_date` (`ingest/tmdb/service.py:71-72`), so 1,249 of 1,251 catalogued films
are release-dated and the corpus skews hard toward trailers. In a hand-classified sample of
60 production-trade `no-match` stories, ~20% were production news (casting, packaging,
greenlight, acquisition) about films that would carry no TMDB date — roughly 71/week against
the ~41/week currently linked from those same outlets. *backlotter: Undated Film Discovery*
would therefore double or triple the corpus and shift its type mix toward casting, making
any threshold calibrated now obsolete before cutover.

## Decision

Cancel the project. Clustering, attach, and classification stay in the Sonnet call.

Three cheaper fixes carry most of the available value and were filed separately:
recompute `Event.occurred_at` from surviving member stories (31 of 199 events are stale by
up to 19 days), filter hidden event types out of synthesis (112 of 199 events are
summarized and never shown), and — when Undated Film Discovery lands — log cluster-stage
attach decisions as plain log lines so a future re-opening has a real baseline.

## Considered alternatives

- **Embeddings for formation and attach, with the LLM retained for classification.** The
  design we actually worked out: ONNX `bge-small-en-v1.5` in-process, vectors in a
  `float4[]` column (pgvector is unavailable on the production Postgres and 377 vectors
  occupy ~580 KB), type-partitioned `argmax` attach, validated by a shadow-mode instrument
  before cutover. Rejected on the four findings above, not on feasibility — it would have
  worked.
- **Deterministic attach rules without embeddings**, extending `subject_key` and the
  singular-beat dedup window. Rejected: it would not have caught the one real defect, which
  was an attach across two stale events rather than a similarity misjudgement.
- **Proceed and calibrate anyway.** Rejected: a 12-week observation window measured against
  a corpus that Undated Film Discovery is about to change would produce a threshold tuned to
  a distribution that no longer exists.

## Consequences

- Clustering output stays nondeterministic. Accepted: the classification fields keep it that
  way regardless, so no achievable version of this project made the stage reproducible.
- No embedding model, vector store, or calibration regime enters the codebase. The container
  image stays free of an ML runtime.
- If the defect rate rises on the post-expansion corpus, this is worth re-opening — with the
  attach logs as a baseline. The design above is recorded here so it need not be re-derived.
- Those attach logs now exist (NEU-970). `apply_cluster_decisions` emits one `cluster
  decision:` line per group the model returned, carrying `film`, `llm` (what the model asked
  for — attach or create), `outcome` (what the code did — `attach`, `create`, `dedup_attach`,
  `reject`, `hold`, `invalid`, `superseded`), `type`, `event`, `stories`, and `note`. `llm`
  and `outcome` are separate so a deterministic dedup merge is never credited to the model.
  A log line and not a table, deliberately: a schema here means the project has been
  re-opened. Reading them back, discard any `film=` that also appears in a `clustering failed
  for film` line — those decisions were rolled back with the film's session.
