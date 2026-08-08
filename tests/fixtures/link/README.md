# Link / Cluster Validation Fixture

`validation_set.json` is the **ground truth** for the NEU-279 accuracy baseline. It is
produced by hand-labeling a sample of real stories drawn from the current corpus.

## The file is dated: `as_of_date` (NEU-1010, re-pinned NEU-1012)

The set is an envelope, not a bare list:

```json
{ "as_of_date": "2026-08-08", "items": [ … ] }
```

`as_of_date` is the date the catalog must be **read as of** when scoring. It is not
decoration: the fixture's stories are news from the weeks around labeling, and the films they
name are in the active catalog only until they release. Roughly **22% of the active catalog
turns over per month** (88 films release within 7 days of any given day, 266 within 30), so a
set scored against *today's* catalog steadily loses its own subjects. Between the first pin
and the re-label six weeks later, that was 42 of 94 `about` items naming films that had left
the active set — unlinkable by **either** link path, and scored as recall failures of the path
under test rather than as drift.

`scripts/validate_linking.py` reads the date from the file, applies it to the retrieval index
(and, until NEU-1004 deleted it, to the roster), and uses it as the prompt's own `as_of_date`
so recency reasoning is reproducible too. It **exits with an error** if a pinned set still
names an unreachable film — that means the pin has drifted from the labels, and the numbers
would look plausible
while measuring decay.

`--as-of` overrides the file's date; it is an escape hatch for re-pinning, not routine use.
A bare-list fixture (the pre-NEU-1010 shape) still loads and falls back to wall clock.

### Why the pin is the *labeling* date, and why that is better than an early one

The first pin (2026-07-01) was chosen to keep the then-labeled films reachable, and it bought
three problems that the labeling date does not have. NEU-1012 moved it to **2026-08-08**, the
day the enlarged set was labeled:

- **Coverage stops being a coincidence.** At 2026-07-01, coverage held by *one day* — the
  earliest film the set named (*Enola Holmes 3*) released exactly on the pin. Now the roster
  shown to the label proposer **is** the pinned active set (`propose_validation_labels.py
  --as-of`), so a film that cannot be scored cannot be labeled in the first place. 121 of 121
  labeled films are reachable, structurally rather than luckily.
- **No story is in the fixture's future.** The draft is bounded at the pin
  (`export_link_validation_draft.py --as-of`), so the prompt's `as_of_date` is never earlier
  than the story it is reasoning about. The old set had one row published five weeks after its
  pin, recorded as unfixable because the two constraints conflicted; at the labeling date they
  do not conflict at all.
- **False positives are counted honestly.** See below.

**What the pin is and isn't.** It rewinds the *active-film filter*, not ingestion history. At
2026-08-08 that filter is doing real work for the first time: it excludes the 212 films that
released between the two pins, where at 2026-07-01 it excluded *nothing* (the catalog's
earliest release date was the pin itself). The eval therefore now exercises scope filtering,
and 129 rows are marked `untracked_film` — real movie news about films the roster does not
hold, which is exactly the negative the active-film clause exists to produce.

**Post-labeling films, and why there are none left.** A `none` label means "not about any film
tracked **at labeling time**", so a pick naming a film the catalog gained *later* is outside
the label space — the link may be perfectly correct and the fixture still has no way to credit
it. Six of retrieval's thirteen false positives at the first pin were exactly that, so
`films_ingested_after` neutralizes such a pick to "no link" when scoring, for both paths.

That neutralization was a necessary correction and also a large one: at the 2026-07-01 pin it
covered **1,068 of 1,435 films**, three quarters of the catalog, so any false positive naming
one of them simply did not count. Pinning to the labeling date empties the set — **0 films
postdate 2026-08-08** — and the machinery stays in place for the next time the two dates
diverge. The practical consequence is that the false-positive counts gate #3 is stated in are,
for the first time, counts of every false positive rather than of the quarter that happened to
be scoreable. `validate_linking.py` still reports the count and **aborts** if a film the
fixture actually labels turns out to postdate the pin, since that would silently convert a
correct link into a false negative.

**Except for 9 synthetic rows.** NEU-358 and NEU-367 added hand-written `about` examples to
exercise the production-news axis where the real corpus was thin. They are identifiable by an
`https://example.test/{ticket}/...` url, and they are labeled by exactly the same rules as the
real rows — but they were never anchored to a real story, which is how the three defects in
*Label audit (NEU-989)* below got in.

## Building the set (candidate-assisted)

Labeling a thousand rows from scratch is slow, so the workflow is review-and-correct:

1. `scripts/export_link_validation_draft.py --target N --as-of <pin> --exclude <current set>`
   → `validation_draft.json` (rows with `relation: "TODO"`). It **samples** rather than dumps:
   the retained corpus is ~98k stories, and the draft is a source-stratified, seed-reproducible
   slice of it bounded at the pin.
2. `scripts/propose_validation_labels.py --as-of <pin>` reads the draft and writes
   `validation_candidates.json` with proposed `relation` / `expected_film_tmdb_id` /
   `event_type`. **Pass the pin** — the roster it shows the proposer is what defines the
   labelable film set, and it has to be the same set the harness will score against.
3. `scripts/assemble_validation_set.py` reconciles the proposals with the existing labeled set:
   it carries the old rows forward, demotes any whose film the new pin excludes, and subsamples
   the proposed `none` rows to the target class mix.
4. **Review every row the model proposed** and correct it. `scripts/build_review_html.py` turns
   candidates into `validation_review.html` with full text and a searchable film picker.
5. `scripts/validate_linking.py` runs the live Stage-1 gate; `scripts/diagnose_linking.py`
   explains the misses and sweeps the confidence floor.

`validation_draft.json`, `validation_candidates.json` and `validation_review.html` are
regenerable intermediates (gitignored); only `validation_set.json` is committed.

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
| `untracked_film` | bool | no | `true` marks a `none` row that is real movie news about a film **not in the roster**, for either reason: undated / not-yet-ingested (the original sense, coverage-gap evidence for NEU-285 / NEU-284), or **already released** and therefore out of scope at the pin (NEU-1012). Ignored by the harness (`load_validation_set` drops unknown fields). Omitted when false. |

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

**The set is enriched, not representative, and that is deliberate.** In the production corpus
only ~8% of stories are `about` a tracked film. A representative sample large enough to hold
300 `about` rows would be ~3,700 rows to hand-review. Instead the draft is sampled
representatively and the *fixture* keeps all of its `about` and `mention` proposals plus a
subsample of its `none` proposals. Gate #3 is a comparison of two paths on one fixture, so a
class mix that is the same for both paths does not favour either; what the mix does affect is
the absolute precision level, which is why absolute numbers here are not production estimates.

Current shape (2026-08-08, NEU-1012):

| | rows |
|---|---|
| `about` | 538 — of which **283 linkable**, 255 not-production-news |
| `mention` | 66 |
| `none` | 571 — of which 129 marked `untracked_film` |
| **total** | **1,175** across 121 films and 27 sources |

**The `about` total is what sets the gate's resolution**, and the two axes read it
differently. `compute_link_metrics` counts a true positive for any `about` row linked to its
labeled film — including a not-production-news row that leaks, which is simultaneously a
true positive on the link axis and a false positive on the news-value axis. So the
true-positive ceiling is 538, not 283. `linkable_about` is the population
`compute_news_value_metrics` scores recall against, and it is reported separately because it
moves independently of the total.

**Why enlarging it was the whole point.** At 94 `about` rows and single-digit false-positive
counts, one story was worth ~1.3 precision points, so cutover gate #3's ±1-point tolerance
asked the fixture to resolve less than a single row (design spec §5.7). Growing the population
is the only thing that fixes that — averaging more runs of a small fixture produces more
numbers and no more resolution.

**Sampling is reproducible.** The draft is drawn by `md5(seed ‖ url)` within each source, so
the same seed against the same corpus draws the same rows; the recipe, not just the output, is
the record. NEU-1012's draft was `--target 4000 --as-of 2026-08-08 --seed neu-1012`, excluding
the 302 urls already labeled.

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
fixture is scored against shrinks over time as its films reach release. That is what the
`as_of_date` pin exists to hold still. Any absolute recall number is only meaningful alongside
the as-of date it was measured at; the fixture is a label oracle, not a catalog snapshot.

**Expect to re-pin roughly every six weeks.** At ~22% catalog turnover per month, a set left
at a stale pin is measuring decay within about that long — which is exactly what happened
between NEU-1010 and NEU-1012. Re-pinning is not re-labeling: carry the set forward with
`assemble_validation_set.py`, which demotes the rows the new pin strands and leaves the rest
alone.

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
`original_title` and `alternative_titles` of exactly the 34 films the `about` rows label,
exported from the dev database once by `scripts/export_retrieval_catalog.py` and
committed. Re-export it when the validation set gains a labeled film; a companion test
fails with that instruction when the two drift.

Committing the catalog is what makes the floor reproducible. The measurement is otherwise
pinned to whatever is ingested locally on the day, and — per *Corpus date* above — to how
many of the fixture's films have since been released. **The test sidesteps the as-of date
entirely** by seeding every fixture film with a release date a year out, so all 34 are
active whenever the suite runs.

**Measured 2026-08-07 with the real retriever (NEU-990–992): 93/93, recall 1.000**, at
T=0.34, K=10, over the 33-film fixture catalog. That is the floor, with no slack. It
clears the 92/93 recorded above because the residual there was the K=10 cap cutting *The
Legend of Zelda* to rank 11 on a 287-film catalog; against a catalog of only the labeled
films the cap never binds.

**Re-measured the same day at the tuned constants (NEU-1001): still 93/93** at T=0.5,
K=25. The test reads the module defaults rather than a frozen pair precisely so a retune
re-runs it, and this one costs the fixture nothing — every labeled film scores at or above
0.5, which is the same fact the production sweep found on its own corpus.

**Grown to 94/94 on 2026-08-08 (NEU-1009).** The added row is the real Deadline story
*Warner Bros' 'F.A.S.T.' Dashes Into Summer 2027* against **F.A.S.T.** (tmdb 556682) — a
film retrieval could not reach at all, because the headline spells the title the same
dotted way the title does and the `len > 1` rule dropped every letter on both sides of the
index. Its dek re-states the initialism, so the row exercises the collapse on headline and
dek alike. This row is the oracle's coverage of that fix: revert the collapse in
`link/retrieval/normalize.py` and the gate reads 93/94, red.

**The re-export that added it also picked up unrelated alt-title drift**, and that is worth
knowing when reading its diff: TMDB's alternative titles move, so re-running
`export_retrieval_catalog.py` after five weeks rewrote ~25 rows across eight other films —
*The Batman Part II* lost four spellings and gained one, *Avengers: Doomsday* traded a
Hebrew and an Armenian title for a Japanese and a Lithuanian one. The drift guard compares
`tmdb_id` sets only, so nothing flags this; the recall floor is what checks it, and it held
at 94/94. Expect the same on the next re-export — it is the fixture catching up with the
catalog, not the ticket changing unrelated films.

Which is also the gate's limit. With no distractors it cannot see a precision regression
or cap saturation — those belong to the offline F1 cutover gate and to shadow telemetry
respectively. This one answers recall, which is the question that costs nothing to ask.

## Label audit (NEU-1012)

The 4,000-row draft was proposed by Sonnet and **every proposed row that reached the fixture
was read and corrected by hand** — 873 rows. 60 corrections, a 6.9% proposal error rate. What
the errors were is more useful than the rate:

**The film id is where the proposer fails, and it fails by transcription rather than by
comprehension.** 21 of the 60 corrections were an `about` row naming the wrong film, and
**16 of those named a film whose TMDB id shares a five-digit prefix with the correct one**:

| rows | proposed | correct | shared prefix |
|---|---|---|---|
| 10 | 1170600 *Return of the Living Dead* | 1170608 *Dune: Part Three* | 6 of 7 |
| 4 | 1389373 *Rowdy Janardhana* | 1389379 *Ranabaali* | 6 of 7 |
| 1 | 1300926 *The Angry Birds Movie 3* | 1300968 *Hunger Games: Sunrise on the Reaping* | 5 of 7 |
| 1 | 1375187 *Dumas: Black Devil* | 1375161 *Don't Move* | 5 of 7 |

The model read the story correctly and then copied the id wrong from a 1,223-line roster. This
is invisible to any end-to-end score — the rows are still `about`, still production news — and
it is fatal to the retrieval oracle, which is the same defect class NEU-989 repaired by hand.
**It is caught cheaply**: the audit is "does any significant token of the labeled film's title
appear in the headline or dek?", run over every `about` row. Re-run it after every proposal
pass. Use a minimum token length of 4 — short tokens like `in` match as substrings under the
squash-fold and hide the row.

**The structural fix is to stop asking the model to copy an integer.** Have the proposer return
the roster line number or the title and resolve the TMDB id in code. Worth doing before the next
labeling pass; not done here, because this pass was audited by hand instead.

**The second failure mode is the same-title trap, in the direction the prompt warns about.**
The rest of the wrong-film corrections were stories about video games, businesses and unrelated
events that share a title with a tracked film — *Resident Evil* (four rows, all about the
games), *Madden*, *Masterplan* (Italian regional planning), *Verity* (a mining stock),
*Call of Duty*, *Don't Look Back in Anger* (Liam Gallagher on X), *Zelda* (the game remake).
These are now `none` rows and they are among the most valuable negatives in the set.

**False negatives were rare.** Reading all 546 proposed `none` rows turned up exactly one row
that should have been `about` (an OTT/satellite rights deal for *Lenin Pandiyan*). The
proposer under-calls `about` far less often than it mis-keys the film.

**What the review did not do.** Rows the proposer labeled `none` and the subsample dropped were
never read, so an `about` story Sonnet missed *and* the subsample excluded is simply absent from
the fixture. Absent rows bias no score — they are not counted for either path — but they do mean
the set is easier than the corpus: it under-represents the stories a strong model finds hard.
Film-existence checks during review were made against a title index only to *confirm* a film the
row had already been read to be about, never to discover what a row might be about; deriving
labels from title matching would make the retrieval path's recall true by construction.
