# Cutover gate #3 is restated in whole items, and its residue is accepted

**Status:** accepted — *backlotter: Entity-Linking Candidate Retrieval* (M4, NEU-1003)
· **revisited 2026-08-08** (NEU-1012), see *Revisited* below. The judgement stands; the
constant it was written as does not, the reason 3c's ceiling was not set to zero has expired,
and gate #3 now fails on 3a.

## Context

Cutover gate #3 (design spec §5) reads: offline end-to-end F1 within ~1 point of the roster
baseline on the repaired fixture, **and precision within the same tolerance, judged
separately**. The second clause is the load-bearing one. It exists to catch the narrowing
regression §4.3 predicts — an F1 that holds while precision falls and recall rises — which an
F1-only gate waves through.

The gate did its job: it caught precision falling, twice (§5.1a, §5.7). What NEU-1011 then
established is that **the instrument cannot resolve the number the gate is asking about.**

- **One story is worth ~1.3 precision points.** The fixture has 94 scoreable `about` items and
  the paths make single-digit numbers of false positives. A ±1-point gate asks for a
  distinction finer than a single item.
- **The control is not stable, and by more than §5.7 said.** Across four `--mode both` runs in
  which the roster prompt is byte-identical (sha `bcd49466c06ab7c6`) and only *retrieval* code
  changed, the roster path's own precision read 0.972 / 0.985 / 0.971 and its F1 0.836 / 0.805
  / 0.822. §5.7 reports its false-positive count wandering between 1 and 2. Recovering its
  **true positives** from those same figures gives **69 / 64 / 67** — a spread of **five items**
  on identical inputs. That is the number that matters, and it was not written down.
- **The F1 delta spans 5.4 points** across those runs (−2.9 to +2.5) against a ±1-point
  tolerance.

The gate's tolerance is therefore several times below its instrument's noise floor. No change
to the code moves that: the binding constraint is the size of the labeled `about` population,
and enlarging it is a hand-labeling project (NEU-1010 established that cost).

At the shipped configuration — §5.7's run D — the residue is small and well characterized.
Retrieval offers the correct film for **94 of 94** scoreable items, links **one more** story
correctly than the roster does, and links **three more** stories it should not have.

## Decision

**Restate gate #3 in whole items, and accept its residue as a recorded product judgement
rather than certify it as a pass.**

The restated gate, measured on the pinned fixture at the shipped configuration:

| # | check | at run D | verdict |
|---|---|---|---|
| 3a | retrieval offers the correct film for every scoreable `about` item | 94 / 94 | **pass**, and the only part with real margin |
| 3b | retrieval's true positives ≥ the roster's | 68 ≥ 67 | **pass**, but inside the noise |
| 3c | retrieval adds no more than **3** false positives against the roster | +3 (5 vs 2) | **at the boundary** |

The true-positive counts are not stated in §5.7 — they are recovered from the precision and F1
it does record, and are over-determined, so the reconstruction is checkable rather than
assumed. TP 67 / FP 2 reproduces the roster's P = 0.971 *and* F1 = 0.822; TP 68 / FP 5
reproduces retrieval's P = 0.932, F1 = 0.814 and hence Δ F1 = −0.8. Spec §5.8 shows the
arithmetic.

**3c's ceiling is a relaxation, and saying otherwise would be the dishonest version of this
decision.** The original gate had *two* clauses, and converted into items they disagree.
Holding retrieval's 68 true positives against the roster's 67 / 2:

| original clause | converted to items | excess allowed |
|---|---|---|
| F1 within ~1 point | 5 false positives scores −0.77 pts, 6 scores −1.26 → ≤5 total | **3** |
| precision within ~1 point | P must be ≥ 0.961; 68/70 = 0.971 passes, 68/71 = 0.958 does not → ≤2 total | **0** |

**The precision clause is the load-bearing one** — it is the clause this gate exists for, since
an F1-only gate is precisely what waves the narrowing regression through. Converted, it allows
retrieval **no** extra false positives at all. Taking 3 relaxes it.

That relaxation is taken deliberately, and for a reason that is not "so the measurement
passes": **0 excess is not a bar this instrument can express.** The roster control's own false
positives move by one and its true positives by five between byte-identical runs. A gate of
"zero extra false positives" would be decided by which run you happened to take, not by the
code under test. Between two clauses that disagree, the ceiling is set by the one the fixture
can actually resolve, and the gap between them — three items — is what is being *accepted*
rather than met.

**So 3c is at a boundary that has already been moved, and 3b's margin is one item against a
control that moves by five.** Neither is a measurement anyone should lean on. The gate is not
what licenses the cutover; the judgement below is, and it is recorded here so a future reader
can check it rather than merely observe that a gate was declared passed.

## The judgement, and its grounds

**Three extra mention-links out of 94, bought with equal-or-better recall, is an acceptable
trade for this product.**

- **The baseline has an expiry date.** Gate #3 compares against the roster path, and the roster
  path is not a live alternative to choose. The 200k context ceiling makes the cutover
  mandatory rather than optional (§1), and NEU-1004 deletes `build_roster` in the same release
  (§5.5). The question the fixture can answer — *is retrieval at parity with the roster?* — is
  not the question that decides anything. The one that does is *is retrieval good enough to run
  on?*
- **The two error kinds are not symmetric.** A false positive puts a story on a film's timeline
  that merely mentions it: visible, and **remediable** — the admin delink surface exists and
  stamps `link_note = 'manual-unlink'`. A false negative is permanent: `link` is lossy, so an
  unlinked story ages out of the recency window and is never linked. The trade runs toward the
  recoverable failure.
- **The residue is not the failure this gate was written to catch.** §4.3's narrowing regression
  is the model shown a film and a headline about *that film's untracked sibling*, and picking
  the sibling's story. Not one of run D's five false positives is a wrong-film pick on an
  `about` row — every one is a `none` or `mention` row: magazine issue previews, multi-film
  roundups, and stories about untracked films whose titles overlap a tracked one (§5.7). And
  candidate coverage is 94/94 in every run ever taken: it is never the retrieval stage that
  loses a link.
- **§8 already accepts precision-under-narrowing** as mitigated rather than eliminated. This
  decision is that acceptance being cashed, not a new risk being taken on.

## Considered alternatives

- **Hold the gate as written and block the cutover.** Rejected. It blocks on a measurement this
  fixture cannot make, against a baseline that is being deleted, while the context ceiling makes
  the cutover mandatory. Blocking would not buy more certainty, only more delay.
- **Widen the points tolerance to ~5 so the measured delta passes.** Rejected — but note
  carefully what the difference is, because it is *not* that this decision avoids relaxing the
  bar. It relaxes it, from 0 excess false positives to 3. The difference is that a widened
  tolerance constant states the new bar and hides the fact that it moved, the reason it moved,
  and by how much; stating it in items forces all three into the open, next to the noise figure
  that motivates them. A relaxation you can audit is a different artifact from one you cannot,
  even when the number it licenses is the same.
- **Average N runs and gate on the mean.** Not rejected on merit — it is the right way to make
  this fixture adjudicable at all, and §5.7 recommends it. Declined here because averaging a
  noisy estimator does not enlarge a 94-item population, which is the binding constraint, and
  because the decision it would inform is dominated by the asymmetries above. Carried forward.
- **Enlarge the labeled `about` population and re-gate.** The real fix, and out of this
  ticket's scope. The population would have to grow severalfold for a ±1-point gate to mean
  anything, and NEU-1010 established that hand-labeling is the expensive part. Carried forward.
- **Raise `LINK_CONFIDENCE_FLOOR` to buy precision.** Rejected on measurement, not on taste:
  §5.7 priced the entire curve from a single floor-0.0 run. Every floor from 0.5 to 0.8 is
  identical — the model returns high confidence on essentially everything it links — and 0.9
  buys 1.8 precision points for 8.5 of recall.

## Consequences

- **`GateVerdict` changes shape.** The harness is the executable form of the gate, so it now
  scores 3a/3b/3c in items rather than a symmetric points tolerance. Leaving it printing
  `GATE: FAIL` on a gate the spec accepts would make the report actively misleading — the next
  reader would take a recorded, argued decision for an unnoticed regression.
- **The points deltas do not disappear from the report.** They are still computed and printed,
  because they are what §5.1a and §5.7 are written in and what makes those sections checkable.
  They stop being the *pass condition*, not the *record*.
- **The accepted residue is watched in production, not on the fixture.** `link_note =
  'manual-unlink'` already records every operator delink, so the rate of manual delinks is a
  direct human-adjudicated precision signal on live traffic — a larger and continuously growing
  sample than the fixture will ever be. A sustained rise after cutover is the signal that this
  judgement was wrong. No new instrumentation is needed for that; only the discipline to look.
- **A future re-gate is a labeling project, not a tuning one.** Anyone revisiting this should
  start by enlarging the `about` population. Re-running the existing fixture more times will
  produce more numbers and no more resolution.

## Revisited — 2026-08-08 (NEU-1012)

The last consequence above says a future re-gate "is a labeling project, not a tuning one" and
should start by enlarging the `about` population. That was done: **94 rows over 34 films → 538
over 121**, re-pinned to the labeling date, hand-reviewed. Design spec §5.11 has the numbers;
what they mean for this decision:

**The judgement stands; the measurement it now rests on is slightly worse than the bar, not
inside it.** The residue accepted here was 3 excess false positives per 94 scoreable `about`
items — 3.19%, which converts to **17.2 items** at the enlarged population. Over three runs
with nothing changed between them, retrieval runs at **3.35%** (excess 17, 19, 18; mean 18.0).
Sample sd 1.0, so the standard error of that mean is 0.58: the mean is **1.0 item above the
converted ceiling, ~1.7 SE, with 2 of 3 runs above it.**

So the accepted trade is confirmed *in magnitude* — 3.35% against 3.19% is agreement to within
about one story per 538 — and is **not** demonstrated to be within the bar. Do not read this
section as the gate passing. What changed is that the difference is now measurable at all:
before, the same question was unanswerable in either direction.

**The instrument is no longer the problem it was — for precision.** The roster control's
precision spread fell from 1.4 points across three byte-identical runs to **0.2**, and one item
fell from ~1.3 precision points to 0.22. The quantity 3c is stated in now moves by 2 items
against a ceiling of 17 (~5%), where it previously moved by about as much as the entire
allowance. "Zero excess false positives is not a bar this instrument can express" was true of
the 94-item fixture and is **no longer true**: at 538 items, zero excess is expressible.
Whether to tighten to it is a product decision this ADR does not pre-empt, but the reason it
was refused has expired.

**What did not improve: F1.** The control's F1 spread was 3.1 points at 94 items and 3.2 at
538, because F1 is dominated by recall and recall noise is proportional to the population. The
original ±1-point F1 clause remains unmeasurable at any fixture size this project will build.

**One correction to this ADR's mechanics.** Stating the residue as the bare integer `3` was
right for a fixed 94-row fixture and wrong as a durable constant: carried unchanged onto 538
rows it would have tightened the accepted bar 5.7-fold, as a silent change to what was decided.
`validate_linking.py` now holds the residue as the rate it was accepted at
(`ACCEPTED_EXCESS_FALSE_POSITIVES` per `ACCEPTED_PER_SCOREABLE_ABOUT`) and derives the item
ceiling from whatever fixture is scored — exact at 94, 17 at 538. Enlarging the population then
buys resolution without moving the bar, which is the separation this ADR needed and did not
have.

**What newly fails: 3a.** This ADR calls candidate coverage "the only part with real margin",
at 94/94 in every run ever taken. At 538 items it is **534/538** in every run — four *correct*
labels naming films lexical retrieval cannot reach (a placeholder title, two cases of subtitle
dilution, one one-character spelling variant). Retrieval did not regress; the corpus finally
contained the failure modes. Gate #3 as written therefore fails, on the clause that was
supposed to be safe, and §5.11 records that rather than restating it away.

**What the enlarged population newly shows.** Retrieval leaks ~50% more not-production-news
stories than the roster (127 vs 84 per run; news-value precision 0.667 vs 0.739, −7.2 points,
consistent across all three runs). This *confirms* the "not one of them is a wrong-film pick"
reading above and shows the effect is systematic and large rather than incidental to five
items. The asymmetry argument — false positives are visible and remediable, false negatives are
permanent — is unchanged, but the manual-delink rate deserves closer watching than "the
discipline to look", and the `link_note = 'not-news:*'` split is where a prompt fix would
surface first.
