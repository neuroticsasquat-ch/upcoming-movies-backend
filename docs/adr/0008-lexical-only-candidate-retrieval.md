# Candidate retrieval is lexical only — no embeddings

**Status:** accepted — implementation tracked in *backlotter: Entity-Linking Candidate Retrieval* (M1)

## Context

Entity linking carries the whole tracked-film catalog in a cached prompt prefix (measured
**44,724 → 51,525 `cache_write` tokens** across 2026-07-21 → 07-31, ~+680 tok/day). The prefix
scales with catalog size, not news volume, so *backlotter: Undated Film Discovery* — which
plausibly multiplies the catalog 5–20x — breaks it outright against the 200k context window.
The fix is to narrow each story to a handful of plausible films *before* the model sees it.

"Retrieval" in 2026 reads as embeddings by default, so the absence of a vector store here will
look like an oversight unless recorded. Two facts argue otherwise.

**pgvector is not available.** The Postgres 17.8 image backing `tbc_postgresql_db` offers
`pg_trgm`, `unaccent` and `fuzzystrmatch`; it has no `vector` extension. Embeddings would mean
a new image on shared infrastructure, an embedding provider, a backfill, and re-embedding on
every TMDB title change — before the first story is linked.

**Lexical matching is already sufficient, measured.** Against the 301-item labeled fixture
(`tests/fixtures/link/validation_set.json`, 93 `about` items over 32 films in a 249-film
catalog), a token-overlap scorer over titles plus a punctuation/whitespace **squash-fold**
returns the correct film in **90 of 93** cases at K=10. All three residuals are mislabeled
fixture rows — synthetic not-news examples whose `expected_film_tmdb_id` points at an unrelated
tracked film. **On correctly-labeled items the score is 90/90.**

Recall@1 equals recall@40: misses are score-zero misses, never ranking failures. There is no
long tail for a semantic model to pick up.

## Decision

Retrieve candidates by lexical match only, scored in pure Python over an in-memory inverted
index rebuilt once per run. Signals are `film.title`, `film.original_title` and
`catalog.film_alternative_title`, normalized with a squash-fold. No embeddings, no vector
store, no `pg_trgm` install.

## Considered alternatives

- **Hybrid lexical + embeddings.** Rejected on evidence, not principle: the corpus shows no
  gap for embeddings to close, and the infrastructure cost lands before any measurement could
  justify it.
- **Postgres `pg_trgm` + GIN, queried per story.** Rejected: needs an extension install on a
  shared database, a migration, index maintenance, and DB-backed tests — to replace a pure
  function that `news/title_match.py` already sets precedent for. It scales past any catalog
  size, which is its real argument; revisit if the per-run index build becomes painful.
- **An LLM query-extraction pass before lookup.** Rejected: reintroduces a per-story model call
  in front of the call this project exists to avoid.
- **Talent, collection name, and other signals in the score.** Measured and rejected — see
  the ablation in the design spec. Each added candidate-set size with **zero** recall gain;
  talent standing alone built 19 extra candidate sets for none. Alt titles are kept despite
  also showing no measured gain, because they are the *same mechanism* as titles and cover
  localized/renamed films that a 32-film corpus cannot exercise.

## Consequences

- The catalog+alt-titles load happens once per run. At a 20x catalog (~24k films) that is a
  larger query but not a per-story one; the inverted index keeps scoring proportional to tokens
  in the headline, not to catalog size.
- A story that names its film only obliquely — by star, director, or an unlisted nickname — is
  unreachable. That is the accepted recall floor, watched by the shadow-mode measurement rather
  than assumed away.
- Reversing this is cheap on the read side (the scorer is a pure function behind a stable
  candidate-set interface) and expensive on the infrastructure side. The trigger for revisiting
  is a measured recall drop after the undated-film expansion, not a preference for vectors.
