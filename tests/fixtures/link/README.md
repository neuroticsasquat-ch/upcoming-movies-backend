# Link / Cluster Validation Fixture

`validation_set.json` is the **ground truth** for the NEU-279 accuracy baseline. It is
produced by hand-labeling a sample of real stories drawn from the current corpus.

## Building the set (candidate-assisted)

Labeling 490 rows from scratch is slow, so the workflow is review-and-correct:

1. `scripts/export_link_validation_draft.py` → `validation_draft.json` (rows with
   `relation: "TODO"`).
2. `scripts/propose_validation_labels.py` reads the draft and writes
   `validation_candidates.json` with proposed `relation` / `expected_film_tmdb_id` /
   `event_type`.
3. `scripts/build_review_html.py` turns the candidates into `validation_review.html` — open
   it in a browser to **review every proposal** with full text + a searchable film picker,
   then download the corrected `validation_set.json` (keep ~150–200 rows).
4. `scripts/validate_linking.py` runs the live Stage-1 baseline;
   `scripts/diagnose_linking.py` explains the misses and sweeps the confidence floor.

`validation_candidates.json` and `validation_review.html` are regenerable intermediates
(gitignored); only `validation_set.json` is committed.

**Anchoring caveat:** candidates are proposed by a *stronger* model (Sonnet) than the
production Stage-1 linker (Haiku, `link_model`), so the set measures the linker against an
independent, human-corrected ground truth rather than its own output. The proposer still
has blind spots — don't rubber-stamp. Scrutinize the **about/mention boundary** and the
**film id** most (those are where it errs). `event_group` is left `null` for you to fill,
since clustering is cross-story.

## Schema

Each item in the JSON array has the following fields:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `url` | string | yes | Canonical story URL (primary key; embedded so the set is portable) |
| `source` | string | yes | Feed source name (e.g. `"Deadline"`) |
| `title` | string | yes | Story headline |
| `summary` | string | no (default `""`) | Lead paragraph or RSS description |
| `relation` | `"about"` \| `"mention"` \| `"none"` | yes | See below |
| `expected_film_tmdb_id` | integer \| null | required for `about` | TMDB film id — stable across databases |
| `event_type` | string \| null | required for `about` | e.g. `"trailer"`, `"casting"`, `"release_date"` |
| `event_group` | string \| null | no | Short shared label across stories about the same news beat (e.g. `"runner-trailer-1"`). Used for cluster scoring. |
| `untracked_film` | bool | no | `true` marks a `none` row that is real movie news about a film **not in the roster** (typically undated / not-yet-ingested). Ignored by the harness (`load_validation_set` drops unknown fields) — captured purely as coverage-gap evidence for NEU-285 (undated capture) / NEU-284 (credits). Omitted when false. |

## Relation labels

- **`about`** — the story is *primarily* about one of our tracked films. Set
  `expected_film_tmdb_id` to the TMDB id of that film and `event_type` to the event
  category. The linker prompt uses the same definition.
- **`mention`** — a tracked film is mentioned, but the story is not primarily about it
  (e.g. a top-10 list that includes the film). No `expected_film_tmdb_id` or `event_type`.
- **`none`** — the story does not reference any tracked film in our roster.

## TMDB-id keying

Items are keyed to films by **TMDB id** (the `tmdb_id` column on `Film`), not by local
UUID. This makes the fixture portable across local databases and environments.

## Sampling protocol

The labeled set should contain:
- All stories in the export window that plausibly match a tracked film (the `about` and
  `mention` candidates).
- A representative reject sample (`none` items) — roughly equal in size to the positive
  sample, drawn randomly from the remainder.
- ~150–200 items total is sufficient for a reliable baseline.

## Event group convention

`event_group` is an optional free-text label that links multiple stories about the *same
news beat* (e.g. several outlets all covering the same trailer drop). The NEU-279 accuracy
harness uses it to score cluster quality: stories sharing a group label should end up in
the same cluster.

Use a short, dash-separated slug: `"<film-slug>-<event>-<n>"`, e.g.
`"runner-trailer-1"`. Leave it `null` if the story stands alone.

## Corpus date

This fixture was drawn from the story corpus as of the labeling date. It is a point-in-time
snapshot and does not update automatically as new stories are ingested.
