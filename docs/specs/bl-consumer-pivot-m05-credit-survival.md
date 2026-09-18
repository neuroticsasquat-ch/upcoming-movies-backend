# M5 spike — credit-attachment survival and the quarantine window (NEU-1372)

**Decision:** D-4 (`docs/specs/bl-consumer-pivot-project-spec.md`). Report only; no production
code changed.
**Script:** `scripts/analyze_credit_survival.py`. **Data:** `bl-consumer-pivot-m05-credit-survival.csv`
(675 rows, one per tracked attachment). Every number below is printed by the script.
**Precedent:** `docs/specs/NEU-1205-dampen-credit-oscillation.md` (evidence section).

## Recommendation

| Question D-4 asks | Answer |
|---|---|
| `SWEEP_CREDIT_QUARANTINE_HOURS` | **96** (from 72) — but see *How strong is this?* below. It is a weak preference, not a clear read. |
| Variable bar by **billing order**? | **No** — and it is not currently measurable at all. |
| Variable bar by **department**? | **No** for v1, though it is the split with the most evidence behind it. |
| Variable bar by **defacement-prone title**? | **No** — but a per-film flap signal is the strongest lever in this data, and it belongs in D-8, not in the window. |

Three findings drive all of it:

1. **The curve does not turn over inside a day.** `config.py` says the window is "set from a
   survival curve that turns over inside a day". It does not — and it *cannot be observed to*,
   because the log's time resolution is one day (see *Method*). That comment should be corrected.
2. **Every window silently rounds up to the next sweep pass.** The gate is
   `changed_at + hours <= now`, but `now` is only ever a sweep, and the sweep runs once a day
   **two hours ahead** of the `tmdb` pass that stamps `changed_at`. A nominal 72h is a real hold
   of ~94h. The current default is already doing more than its number suggests, and the curve
   must be read against the effective hold, not the nominal one.
3. **More than half the reverts come from films that have already flapped.** 33 of 58 reverts
   (57%) land on a film that produced an earlier revert inside the same 20-day window, against
   only 6 of 58 on a repeat of the same `(film, person)` slot. No window length addresses that;
   a flap history does.

## Method

`catalog.film_credit_change` is append-only and seed-grade only (director, writer, top-5 billed
cast). For each `(film, person, credit_type, job)` slot, changes are walked in time order and each
`added` is paired with the `removed` that later retracts it. Survival time is the gap between them.

- **Orphan removals are excluded.** 279 `removed` rows have no tracked `added` before them — they
  are baseline credits, recorded before the change log covered them, so no attachment was ever
  observed and no survival time exists. NEU-1205 found the same shape (227 of 268 removals). They
  are counted and dropped, never treated as instant reverts.
- **Censoring is handled by Kaplan–Meier**, because both naive alternatives are wrong in opposite
  directions. Scoring every horizon over all adds treats an attachment recorded two hours before
  the log ends as a 168h survivor. Keeping reverts at every horizon while dropping their
  still-attached peers for short follow-up conditions the denominator on the outcome and
  oversamples events — inside the last H hours of the window, only reverts survive that filter.
  KM shrinks the risk set instead of filtering the population; the risk set is printed beside
  each estimate.
- **The log's resolution is one day.** Changes are stamped almost entirely in the 09:00 UTC hour
  (912 of 1,012 rows) — the single daily `tmdb` pass. A credit added and reverted between two
  passes leaves no trace at all. So the true revert rate is **understated**, the empty buckets
  below 18h are an artefact of the cadence rather than a finding, and no window below 24h is
  meaningful. It also means survival times land on a ~24h grid, which matters for the knee below.

## The data

Local `app` database, `catalog`/`news`/`ingest` mirrored from production.

```
window           2026-08-12 → 2026-09-01 (20.1 days)
adds tracked     675
reverted         58 (8.6% of adds)
still attached   617
orphan removals  279  (baseline credits, no tracked add — excluded)
```

Survival time to deletion, reverted attachments only:

```
      18–24h     3  ██████
      24–36h    19  ████████████████████████████████████████
      36–48h     0
      48–72h     6  █████████████
      72–96h    10  █████████████████████
     96–168h    12  █████████████████████████
       >168h     8  █████████████████
```

Nothing reverts before 18h (see resolution, above). The mass sits at 24–36h, then the curve has a
**genuine heavy tail**: 20 of 58 reverts take longer than 96h, and the longest runs 313h.

### Fraction of adds reverted within each horizon (D-4's requested table)

`reverts` is the raw count suppressed at that length; `KM` is the censoring-corrected probability
for an arbitrary attachment; `caught` is the share of all 58 reverts; `+` is the marginal gain over
the previous row.

| horizon | reverts | at risk | KM | caught | + |
|---:|---:|---:|---:|---:|---:|
| 24h | 3 | 618 | 0.5% | 5% | 5% |
| 48h | 22 | 566 | 3.6% | 38% | 33% |
| 72h | 28 | 523 | 4.7% | 48% | 10% |
| 96h | 38 | 480 | 6.5% | 66% | 17% |
| 168h | 50 | 406 | 9.0% | 86% | 21% |

## Nominal window vs. what it actually holds

`hold` is the median real delay imposed on an attachment; `caught` is the share of all 58 reverts
suppressed before they could card.

| nominal | real hold | held off | known | caught |
|---:|---:|---:|---:|---:|
| 24h | 46h | 22 | 585 | 38% |
| 48h | 70h | 27 | 550 | 47% |
| **72h** *(today)* | **94h** | **37** | 517 | **64%** |
| **96h** *(recommended)* | **118h** | **46** | 488 | **79%** |
| 168h | 190h | 51 | 392 | 88% |

Reading it: 24h → 48h buys +9pp of coverage, 48h → 72h +17pp, **72h → 96h +15pp**, and
96h → 168h only +9pp for three further days of delay. On coverage per hour of delay, 96h is the
last step that pays.

**Cost.** The extra day falls on the 617 attachments that survive, ~31/day. That is bounded by D-5:
an attachment a trade story names short-circuits quarantine and publishes immediately (NEU-1371),
so the hold only ever delays TMDB-only attachments — the tranche with no external corroboration,
which is exactly the tranche this feature exists to distrust.

**Safety.** 96 < 168 (`SWEEP_EVENT_LOOKBACK_DAYS` = 7 days), so `validate_sweep_configuration`
passes, with 50h of slack between the 118h effective hold and the 168h rolling window. See
follow-up 1 before raising it further.

## How strong is this?

Weaker than the table makes it look, and the reader should know exactly how.

- **The knee is one burst.** The maximum-distance-to-chord knee is 97.4h, by which 46 of 58 (79.3%)
  of reverts have happened. But 4 of the 9 reverts that the 72h → 96h step buys are a *single
  simultaneous burst* on **Untitled Michael Sequel**, all four stamped 97.38h. The knee lands on
  that burst. The whole 9 come from 6 films.
- **The apparent corroboration is a grid artefact.** Nominal 96h suppresses exactly 46 reverts,
  the same 46 the 97.4h knee identifies — but that is forced, not independent: there are **zero**
  reverts anywhere in (97.4h, 118h]. Any nominal setting from ~76h to ~96h lands in the same gap
  and catches the same 46. This is not two methods agreeing.
- **So the real choice is 72 vs 96, and the data does not sharply separate them.** 96h buys 9
  fewer slop cards per 20 days (~0.45/day) at the price of one extra day of hold on ~31
  attachments/day.

**The decision criterion, stated plainly:** take 96 if a published-then-retracted credit card costs
more than a day of latency on an uncorroborated TMDB attachment. Given D-3's own rationale — "an
edit that was never true publishes nothing at all rather than publishing and being corrected" — and
D-5 bounding the latency to attachments no trade covered, it does. **But staying at 72 is defensible
on this evidence**, and if the value is revisited after a quarter of accumulation, it should be
revisited on the flap signal below rather than on this curve.

## Splits

### By department — raw proportions, and stark

| role | adds | reverts | rate |
|---|---:|---:|---:|
| cast | 520 | 55 | 10.6% |
| writer | 116 | 2 | 1.7% |
| director | 39 | 1 | 2.6% |

Cast carries 55 of 58 reverts. The three crew reverts took 72.0h, 121.4h and 168.1h — all at or past
the current window, so no plausible quarantine setting catches any of them, and quarantining crew
buys nothing measurable in this sample.

**Still: no variable bar for v1.** The tempting move is to shorten or drop the crew window and
publish director/writer attachments a day sooner. It is a second constant and a branch, over 23% of
volume, on the strength of three events — and a fabricated director credit is precisely the
high-visibility slop the feature exists to suppress. Revisit with a full quarter of data if
freshness on directors and writers ever becomes the complaint. This is the one split where the
evidence would support acting later.

### By billing order — not measurable, and that is the finding

`film_credit_change` records `credit_type` and `job` but **no `credit_order`**. The only source of
billing position is `catalog.film_credit`, which is delete-and-rebuilt on every ingest and so holds
a row only for a credit that is *still attached*. Billing applies to cast only (crew carry no
`credit_order` by construction), and among cast the coverage splits:

- Cast adds overall: 475 of 520 joinable (91.3%).
- **Reverted** cast adds: 10 of 55 joinable (18.2%) — and those ten are the flaps that re-attached.

So billing is observable for almost exactly the population whose billing does not matter, and
missing for the population the question is about. Any top-1/top-3/top-5 split computed today is
survivorship-biased to the point of being an artefact; the tables the script prints under
`=== By billing order ===` carry that warning inline and should not be quoted without it.

**No variable bar by billing order.** If it is ever wanted, the prerequisite is a schema change:
stamp `credit_order` onto `film_credit_change` at write time in `ingest.tmdb.credit_history`, then
re-run this script after a quarter of accumulation. Nothing can be concluded before that.

### By title — the real signal

| reverts | adds | rate | title |
|---:|---:|---:|---|
| 13 | 15 | 87% | The Seven Husbands of Evelyn Hugo |
| 6 | 6 | 100% | Untitled Saw Film |
| 4 | 5 | 80% | Untitled Michael Sequel |
| 4 | 5 | 80% | Ghost Market |
| 4 | 7 | 57% | Untitled National Treasure 3 |

25 distinct films carry all 58 reverts, and the top five carry 31 of them (53%). Evelyn Hugo is not
one bad edit — it oscillates across the whole window:

```
adds    2026-08-12 ×2, 2026-08-15 ×3, 2026-08-16 ×2, 2026-08-17 ×4, 2026-08-26 ×2, 2026-08-29 ×2
removes 2026-08-15 ×1, 2026-08-16 ×4, 2026-08-17 ×2, 2026-08-19 ×2, 2026-08-27 ×2, 2026-08-30 ×2
```

Nine of its thirteen reverts complete in 24–25h. Each cycle is shorter than any window and there
are six of them — **a longer window does not help; it holds each cycle a little longer before the
next one starts.**

The predictive version: **33 of 58 reverts (57%) occur on a film that had already produced a revert
earlier in the window, against only 6 of 58 on a repeat of the same `(film, person)` slot.** It is
the *film* that flaps, not the individual credit. That signal needs no schema change — it is a count
over `film_credit_change` — and it is available at the moment of carding.

**No variable bar by title.** Vary the *hold decision*, not the window: this belongs with D-8's
sanity holds, which already contemplate "an add→remove→re-add flap inside the window". A per-film
flap-rate check ("this film has produced N reverts in the last M days — hold and log to
`ingest.credit_hold`") addresses a majority of the slop a 96h window still lets through, and is a
better use of the next ticket than tuning the window a second time.

## Follow-ups

1. **`validate_sweep_configuration` is off by up to one sweep period.** It compares the *nominal*
   `SWEEP_CREDIT_QUARANTINE_HOURS` against `SWEEP_EVENT_LOOKBACK_DAYS`, but the effective hold is up
   to 24h longer. A nominal 150h passes the guard; at a sweep the eligible band is then
   `changed_at ∈ [T-168h, T-150h]`, 18h wide against a ~24h sweep period, so rows slip past unread
   — the exact silent failure the guard was written to refuse. The safe ceiling is
   `lookback_hours - 24`, not `lookback_hours - 1`, and even that assumes no pass is skipped (24
   sweep runs over 21 days, two of them at 04:00, so the interval is not reliably 24h). Unticketed;
   not fixed here because this spike changes no production code.
2. **Correct the `config.py` comment** on `sweep_credit_quarantine_hours`: the curve does not "turn
   over inside a day", and it cannot be seen to at a daily ingest cadence.
3. **Stamp `credit_order` onto `film_credit_change`** if a billing-order bar is ever wanted.
4. **Per-film flap check for D-8**, per the section above. This is the highest-value follow-up here.

## Caveats

- **One 20-day window, 58 events.** Every rate has a wide interval; the department and billing
  splits are the thinnest. The shape (no reverts under 18h, mass at 24–36h, heavy tail past 96h) is
  robust; the exact percentages are not, and the 72-vs-96 margin rests on 9 reverts from 6 films.
- **The snapshot ends 2026-09-01.** `task db:refresh` needs `PROD_SSH`, which is not configured in
  this workspace, so this is the content mirror as it stood. Re-run the script after a refresh
  before the 96h value is deployed — it is a one-command check and needs no arguments.
- **The revert rate is a floor**, not an estimate: same-day add-and-revert is invisible at a daily
  cadence.
- **D-5 was merged after this data was recorded** (NEU-1371, merged 2026-09-18), so no attachment in
  this window carries `carded_by_event_id`. The argument that Tier-A bounds the freshness cost is
  structural, not measured.
