# Link / Cluster Validation Fixture

`validation_set.json` is the **ground truth** for the NEU-279 accuracy baseline. It is
produced by hand-labeling a sample of real stories drawn from the current corpus.

**Except for 9 synthetic rows.** NEU-358 and NEU-367 added hand-written `about` examples to
exercise the production-news axis where the real corpus was thin. They are identifiable by an
`https://example.test/{ticket}/...` url, and they are labeled by exactly the same rules as the
real rows — but they were never anchored to a real story, which is how the three defects in
*Label audit (NEU-989)* below got in.

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
| `event_group` | string \| null | no | Gold beat-grouping label, film-namespaced `"{tmdb_id}-{beat}"` (e.g. `"1003596-doctor-doom-casting"`). Used for cluster scoring — see "Event group convention". |
| `is_production_news` | bool \| null | no (about only) | `false` marks an `about` story that is **not** production news (should not link). `null` = treated as production news (expected to link). |
| `exclusion_category` | string \| null | no | One of `reaction｜roundup｜streaming-move｜interview-quote｜downstream｜other`. Set only when `is_production_news` is `false`. Diagnoses *why* a story was excluded. |
| `untracked_film` | bool | no | `true` marks a `none` row that is real movie news about a film **not in the roster** (typically undated / not-yet-ingested). Ignored by the harness (`load_validation_set` drops unknown fields) — captured purely as coverage-gap evidence for NEU-285 (undated capture) / NEU-284 (credits). Omitted when false. |

## Relation labels

- **`about`** — the story is *primarily* about one of our tracked films. Set
  `expected_film_tmdb_id` to the TMDB id of that film and `event_type` to the event
  category. The linker prompt uses the same definition.
- **`mention`** — a tracked film is mentioned, but the story is not primarily about it
  (e.g. a top-10 list that includes the film). No `expected_film_tmdb_id` or `event_type`.
- **`none`** — the story does not reference any tracked film in our roster.

## Production-news axis (NEU-358)

Orthogonal to about/mention/none: an `about` story can still fail the *production-news*
test (it talks about the film but announces nothing new — a reaction, a roundup, interview
color, a streaming-catalogue move, or a downstream piece). For scoring, an `about` row is
**expected to link** iff `is_production_news is not False`; rows with `is_production_news:
false` are expected to be rejected by the Stage-1 linker. Run `scripts/validate_linking.py`
to see news-value precision/recall and the leak-by-category table.

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

`event_group` is the gold beat-grouping key for the Stage-2 cluster-purity baseline
(NEU-300, harness `scripts/validate_clustering.py`). It links multiple stories about the
*same news beat* (e.g. several outlets covering the same trailer drop) so the harness can
score cluster quality: stories sharing a group label should end up in the same cluster.

Set it **only** on linkable `about` rows (`relation == "about"` AND `is_production_news` not
`false`); leave it `null` elsewhere. Slugs are **film-namespaced** —
`"{expected_film_tmdb_id}-{beat-slug}"` (e.g. `"1003596-doctor-doom-casting"`) — so they are
globally unique, which lets `compute_cluster_metrics` pool predicted clusters across films
without cross-film pair leakage. Draft with `scripts/propose_event_groups.py` (Opus), then
hand-correct; score with `scripts/validate_clustering.py`.

## Corpus date

This fixture was drawn from the story corpus as of the labeling date. It is a point-in-time
snapshot and does not update automatically as new stories are ingested.

**This matters for retrieval scoring.** Candidate retrieval searches only *active* films
(`active_film_clause` — released and canceled titles are out of scope), so the catalog the
fixture is scored against shrinks over time as its films reach release. As of 2026-08-07,
**40 of the 93** `about` rows (9 of the 33 distinct films) name a film that has since gone
inactive and is no longer retrievable at all. Any absolute recall number
is therefore only meaningful alongside the as-of date it was measured at; the fixture is a
label oracle, not a catalog snapshot.

## Label audit (NEU-989)

The `expected_film_tmdb_id` column was audited in full on 2026-08-07, after three rows were
found pointing at an unrelated tracked film. All three were synthetic not-news rows
(`is_production_news: false`) that reused an arbitrary film id — they are rejected either
way, so the end-to-end score never moved, but a wrong film id makes the fixture unusable as
a **retrieval oracle**, which every M1 measurement depends on.

| headline | was | now |
|---|---|---|
| *Amy Adams TEASES Her Excitement For Star Wars: Starfighter…* | 1003596 Avengers: Doomsday | 1417668 Star Wars: Starfighter |
| *Star Wars: Matt Smith Opens Up On His … Starfighter Role* | 1003596 Avengers: Doomsday | 1417668 Star Wars: Starfighter |
| *Lewis Pullman On … The Spaceballs Sequel* | 969681 Spider-Man: Brand New Day | 1306322 Spaceballs: The New One |

All three name a **tracked** film, so each was re-pointed rather than demoted to `none`. The
`relation` / `is_production_news` / `exclusion_category` verdicts are unchanged — the repair
corrects which film the row is about, not whether it should link.

**Method.** Every `about` row was checked against its labeled film's `title`,
`original_title` and every `film_alternative_title`, flagging any row where no title's
significant tokens were *fully* present in the headline + dek (a strictly stronger check than
"some token appears"). That surfaced 8 rows: the 3 defects above and 5 correct partial-title
matches, each eyeballed and kept —

- *Fall 2 Trailer Teases A Vertigo Inducing Thriller* → **Fall 2: Deadpoint** (subtitle absent)
- *The Live-Action Zelda Movie Has A New Worldwide Release Date* → **The Legend of Zelda**
- *Tom Holland and Zendaya's characters reunite in new Spider-Man movie trailer* → **Spider-Man: Brand New Day**
- *Ian McKellen "Can't Wait" To Return As Gandalf In The Hunt For Gollum* → **The Lord of the Rings: The Hunt for Gollum**
- *Anya Taylor-Joy Reacts To Her Lord Of The Rings Casting* → same film

After the repair the audit reports **no unexplained rows**. Only `about` rows carry a film
id, so they are the whole audit surface; `mention` and `none` rows have nothing to mislabel
in this respect.

**Effect on retrieval.** Scored with the §4.1 lexical scorer (titles + alternative titles,
T=0.34), each repaired row moves from below-threshold against its old label to full credit
against its new one:

| row | old label | new label |
|---|---|---|
| Amy Adams / Starfighter | 0.00 | **1.00** via `Star Wars: Starfighter` |
| Matt Smith / Starfighter | 0.00 | **1.00** via `Star Wars: Starfighter` |
| Lewis Pullman / Spaceballs | 0.20 | **1.00** via `Spaceballs 2`, 0.67 on the primary title |

The Spaceballs row scores 1.00 on the alternative title `Spaceballs 2`, but it does **not**
depend on it: the primary title *"Spaceballs: The New One"* scores 0.67 on its own, since the
dek ("no **new** beat") supplies a second token. So the design's ablation finding — that
alternative titles add candidate-set size for zero recall (§3.3) — **survives the repair**.

**The ticket's 93/93 target was not reproduced; the measurement below is 92/93.** That number
was taken with a stand-in scorer, because the real retriever does not exist yet (NEU-990), and
at an as-of date of **2026-07-01** — chosen so the catalog still contains the films the corpus
was labeled against (see *Corpus date* above). On the same scope the pre-repair fixture scores
89/93, so the repair is worth the expected **+3**.

The single residual is *not* a labeling defect and not a scoring failure: **The Legend of
Zelda** clears T at 0.5 (its headline carries `zelda` but not `legend`) and is a legitimate
candidate, but lands at rank 11 on a 287-film catalog and is cut by the K=10 cap. That is a
**ranking** artifact of the cap, and it is exactly the kind of constant §3.4 defers to the
tuning ticket (M3) rather than fixing here.

NEU-993, which owns the permanent oracle test, should therefore pin the recall number
**together with** the as-of date and the K it assumes — an absolute figure alone is not
reproducible against this fixture.

## Retrieval recall oracle (NEU-993)

`retrieval_catalog.json` is the catalog the recall gate scores against —
`tests/integration/link/retrieval/test_recall_oracle.py`. It holds the `title`,
`original_title` and `alternative_titles` of exactly the 33 films the `about` rows label,
exported from the dev database once by `scripts/export_retrieval_catalog.py` and
committed. Re-export it when the validation set gains a labeled film; a companion test
fails with that instruction when the two drift.

Committing the catalog is what makes the floor reproducible. The measurement is otherwise
pinned to whatever is ingested locally on the day, and — per *Corpus date* above — to how
many of the fixture's films have since been released. **The test sidesteps the as-of date
entirely** by seeding every fixture film with a release date a year out, so all 33 are
active whenever the suite runs.

**Measured 2026-08-07 with the real retriever (NEU-990–992): 93/93, recall 1.000**, at
T=0.34, K=10, over the 33-film fixture catalog. That is the floor, with no slack. It
clears the 92/93 recorded above because the residual there was the K=10 cap cutting *The
Legend of Zelda* to rank 11 on a 287-film catalog; against a catalog of only the labeled
films the cap never binds.

Which is also the gate's limit. With no distractors it cannot see a precision regression
or cap saturation — those belong to the offline F1 cutover gate and to shadow telemetry
respectively. This one answers recall, which is the question that costs nothing to ask.
