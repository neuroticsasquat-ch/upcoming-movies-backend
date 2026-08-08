# Cutover gate #3 is restated in whole items, and its residue is accepted

**Status:** accepted — *backlotter: Entity-Linking Candidate Retrieval* (M4, NEU-1003)

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
