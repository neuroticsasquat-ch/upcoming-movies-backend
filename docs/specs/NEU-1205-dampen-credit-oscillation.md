# NEU-1205 — Dampen TMDB credit oscillation in removal carding

## Problem

NEU-1200 cards every seed-grade credit detachment as a `credit_removed` event, with the invariant
*the latest card for a person reflects their current attachment state* — so a re-attachment after a
removal *is* carded (not suppressed). That invariant is correct, but NEU-1200 has no dwell filter:
TMDB is community-edited and credits oscillate (added → removed → added → removed) over a few days,
often from anonymous edits that get reverted. Each cycle produces a join card and a depart card,
cluttering the timeline with noise — the exact concern ADR-0014 originally declined to card for.

**Reported case.** Black Panther 3 (tmdb 1386618): Maya Boyd was added Aug 24, removed Aug 25,
added Aug 27, removed Aug 28 — four changes in four days, all carded 1:1 by NEU-1200. She is
correctly *not* in the cast now, so the final state is right; the problem is the four-card chain to
get there.

## Evidence (dev DB, populated, 268 removals — matches the prod report)

- **268 removals** total; **41 are dwell-eligible** (have a tracked prior `added` in
  `catalog.film_credit_change`, i.e. a prior attachment card could exist). The other 227 are
  baseline credits whose `added` predates the change log → no attachment card → already not carded
  by NEU-1200. **The dwell gate's entire scope is those 41.**
- Of the 41: **7 are flaps** (a later `added` for the same film+person follows the removal) and
  **34 are final** (no re-attachment). **256 of all 268 removals are final.**
- **Max forward gap among the 7 dwell-eligible flaps is 2.001 days.** All 7 re-attach within
  ≤2.001 days. (A 5.003-day outlier exists but is *non-dwell* — no prior tracked attachment — so it
  never enters the gate.)
- Of the 7 suppressed flaps: 5 self-correct to a final `added` state; 2 (Naruto, Black Panther 3)
  still card a real final `removed` later. **No suppressed flap leaves a stale attachment.**
- Only **3 oscillator films** in the dev DB (Naruto, Ghost Market, Black Panther 3), each ≤2 cycles.
  The flap signature (≤2-day re-attach) is clear; re-verify against prod for more oscillator mass.

## Direction — forward-dwell + N-day hold (NOT the ticket's backward dwell)

The ticket proposed a **backward** gate: card the removal only if the prior attachment card is ≥ N
days old. The data rejects this. Backward dwell cannot distinguish a flap (removed then re-attached)
from a *real brief departure* (removed, never re-attached) — both have short dwell. At any N it
suppresses mostly final brief departures, **re-creating NEU-1200's stale-attachment bug** (an
uncorrected brief attachment living forever) rather than dampening flaps. At N=7 the trade is ~12
flaps fixed against ~27 stale tails created — a bad trade.

The alternative the data supports is a **forward** gate: card a removal only if the person does
**not** re-attach within N days *after* it. A flap (removal immediately followed by re-attachment)
is suppressed; a real departure (no re-attachment) always cards. This is surgical: it gates the 7
flaps and cards the 34 finals, with **no stale tail**.

**The hold.** A removal processed the day it happens cannot yet know whether a re-attachment will
follow. Forward-dwell therefore needs an N-day hold: a removal is only eligible to card once it is
≥ N days old, by which time the forward re-attachment window is fully observed. Cost: the removal
card publishes N days late on the feed (`created_at`, per ADR-0016) — but the **film page groups by
`occurred_at`** (NEU-1204), so it still appears under the correct day, and removal cards are
collapsed-section `rumored` cards, so the feed delay is low-harm. The hold requires
`N < SWEEP_EVENT_LOOKBACK_DAYS` so a held removal is still in the rolling window when it becomes
eligible; the backfill (no lookback, all history fully observed) is the backstop if an operator
mis-tunes N above the lookback.

### Worked example — Maya Boyd (Black Panther 3), N=3

| Step | TMDB change | Carding (under forward-dwell) |
|------|-------------|-------------------------------|
| 1 | added 8/24 | `casting` card, `occurred_at` 8/24 |
| 2 | removed 8/25 | hold: 8/25 + 3 = 8/28 > now (now≈8/26). **Held, not carded.** |
| 3 | added 8/27 | removal-aware suppression: latest *carded* event for Maya is the 8/24 `casting` (the 8/25 removal is held, invisible to `_latest_credit_event_types`). latest = attachment → **re-attachment suppressed.** No card. |
| 4 | removed 8/28 | hold: 8/28 + 3 = 8/31 > now (now≈8/29). **Held.** |
| — | now = 8/29 | 8/25 removal eligible (8/25+3 = 8/28 ≤ 8/29). Forward check (raw `film_credit_change`): `added` for Maya in `[8/25, 8/28)`? Yes — 8/27. → **flap → suppressed.** No card. |
| — | now = 9/1 | 8/28 removal eligible (8/28+3 = 8/31 ≤ 9/1). Forward check: `added` in `[8/28, 9/1)`? No. → **final → cards.** `occurred_at` 8/28. |

**Final timeline: attach 8/24 + remove 8/28 — two cards (bookends).** Correct, and the 4-card chain
is dampened to 2.

## Scope

- **Backend only.** No frontend change — a dampened removal is simply a `credit_removed` card that is
  never published; the frontend renders the cards that *do* fire exactly as under NEU-1200.
- **Removal-only.** The attachment carding path is untouched. Flap dampening comes entirely from
  (a) the removal being held/suppressed by the forward gate and (b) the existing removal-aware
  suppression suppressing the flap's re-attachment. Touching attachment carding is unnecessary and
  would re-introduce the stale-removal problem NEU-1200 fixed.
- **Credits only.** Release-date withdrawals remain out of scope (no withdrawal rows exist).
- **Forward-only backfill.** Already-carded removals (from the NEU-1200 ship, including the reported
  Maya Boyd 4-card chain) are grandfathered — left as-is, not destructively cleaned. Only oscillation
  observed *after* this gate ships is dampened. See "Backfill posture" below.
- **Notifications still deferred.** A dampened flap produces no removal card and so no future
  notification; a real final departure still cards and is notification-ready. No notification
  machinery is built here.

## Acceptance criteria

- A `credit_removed` event cards for a detachment iff **all three** hold:
  1. **Prior-attachment gate** (unchanged from NEU-1200): the person has a prior visible attachment
     card (`crew_attached` or `casting`, any provenance) with `occurred_at < detachment.changed_at`.
  2. **Hold** (when `SWEEP_CREDIT_DWELL_DAYS > 0`): `detachment.changed_at + N <= now` — the forward
     window is fully observed.
  3. **Forward-reattachment gate** (when `SWEEP_CREDIT_DWELL_DAYS > 0`): no `added` row for the same
     `(film_id, person_id, seed-grade role)` exists in the half-open forward window
     `[changed_at, changed_at + N)`.
- When `SWEEP_CREDIT_DWELL_DAYS == 0`, gates 2 and 3 are skipped — behavior reverts to plain
  NEU-1200 (card on prior-attachment gate alone). This is the kill switch.
- The forward-reattachment gate reads **`catalog.film_credit_change`** (raw history), **not**
  `news.event` (cards). A flap's re-attachment is itself suppressed by the removal-aware suppression
  and so is never carded; checking `Event` would miss it and wrongly card the flap's removal.
- The forward-reattachment gate is scoped to the **same seed-grade role** (cast/director/writer), not
  person-only. A person removed from the cast and later re-added as director is two real events (a
  cast departure + a director arrival), not a flap; person-only scoping would suppress the cast
  removal and leave it uncorrected.
- The gate is **per-person** within a group, composed with the existing per-person prior-attachment
  gate: a group of three removed where one is a flap and two are finals cards naming only the two
  finals. If none remain, no card (unchanged from NEU-1200).
- The hold is **monotonic held → carded, never carded → skipped**: a held removal is not carded, so
  `uq_event_catalog_change` does not fire, and `_already_carded` returns False on re-read; the hold
  check runs again next sweep. Once the hold passes and no forward re-attach exists, it cards and is
  stable on all later re-reads (deterministic across the rolling window).
- The removal event's metadata is unchanged from NEU-1200: `event_type="credit_removed"`,
  `provenance="catalog"`, `confidence="rumored"`, `occurred_at = detachment.changed_at`,
  `region=None`, `subject_key = [normalize_name(name) for each gated person]`, deterministic summary
  sentinel `model="deterministic"`.
- A re-attachment during the hold is suppressed by the removal-aware suppression (the held removal is
  invisible to `_latest_credit_event_types`, so the person's latest *carded* event is still the
  original attachment). The invariant *latest card = current state* holds for the carded timeline;
  the hold introduces only a **transient** ≤N-day window where TMDB says "removed" but the latest
  card is still "attached" (see "Transient invariant violation" below).
- The 7 dwell-eligible flaps in the dev DB are all suppressed; the 34 finals all card. No suppressed
  flap leaves a stale attachment (5 self-correct to `added`; 2 still card a real final `removed`).
- `task test && task lint && task typecheck` pass.

### Non-goals / out of scope

- **Backward dwell.** The ticket's preferred direction. Rejected on the data (suppresses mostly
  real brief departures, re-creating NEU-1200's stale-attachment bug). See ADR-0014 amendment.
- **Suppressing the attachment card.** The ticket's "more aggressive" alternative. Rejected —
  re-introduces the stale-removal problem and is unnecessary under forward-dwell.
- **Destructive cleanup of already-carded removals.** The reported Maya Boyd 4-card chain is
  grandfathered. A destructive `DELETE` of published events is out of scope (and would be dicey once
  notifications exist).
- **Release-date withdrawals, notifications, story-side dedup for removals, frontend changes.** All
  unchanged from NEU-1200's non-goals.

## Technical decisions

### 1. Forward-dwell + N-day hold, not backward dwell

See "Direction" above. The choice is data-driven: backward dwell re-introduces NEU-1200's bug at
scale (27 stale tails vs 12 flaps at N=7); forward-dwell preserves the correction guarantee and only
delays publication of a low-signal collapsed card. Recorded as a dated amendment to ADR-0014.

### 2. The forward-reattachment gate reads raw `film_credit_change`, not `Event`

The flap's re-attachment is suppressed by the removal-aware suppression (`_uncarded_credits`) and so
is **never carded**. A forward gate that read `news.event` would see no re-attachment and wrongly
card the flap's removal. The gate must read `catalog.film_credit_change` (raw history), where the
re-attachment row exists regardless of whether it was carded. The prior-attachment gate, by contrast,
continues to read `Event` (cards) — unchanged from NEU-1200 — because it asks "was a beat published?"

### 3. Role-scoped forward gate

The forward check matches the same **seed-grade role** (cast/director/writer), derived via
`credit_role(credit_type, job)`, not person-only. A person removed from the cast and re-added as
director is two real events; person-only scoping would suppress the (real) cast departure and leave
it uncorrected — the stale-attachment bug, reintroduced through the back door. `FilmCreditChange`
stores `credit_type` + `job` (not `role`); the gate loads candidate `added` rows for `(film_id,
person_id)` in the forward window and filters in Python by `credit_role == detached.role`.

### 4. `SWEEP_CREDIT_DWELL_DAYS` config, 0 disables

A new tuned constant in `config.py`, mirroring `SWEEP_EVENT_LOOKBACK_DAYS`:

```python
sweep_credit_dwell_days: int = Field(default=3, ge=0, alias="SWEEP_CREDIT_DWELL_DAYS")
```

- `ge=0` (0 is the kill switch, not a gap — reverts to plain NEU-1200).
- **Constraint `N < SWEEP_EVENT_LOOKBACK_DAYS`** (default 3 < 7): documented but **not enforced**.
  If an operator sets `N >= lookback`, a removal ages out of the rolling window before eligibility
  and is never carded in steady state; the backfill (no lookback, all history fully observed) is the
  backstop and still cards eligible finals. Documented in the config comment + deploy checklist.
- Follows the repo's tuned-constants deploy checklist: code default → `docker-compose.prod.yml` →
  Coolify UI → `printenv` on the running container.
- The value is empirical (dev DB has only 3 oscillator films); re-verify against prod after deploy.

### 5. Window semantics — half-open forward window, hold predicate

- **Forward window:** `[changed_at, changed_at + N)` — half-open, fixed by N. A re-attachment
  exactly at `changed_at + N` is *outside* the window and does not suppress (it is a real departure
  followed by a much-later re-attach). The fixed window (not `[changed_at, now)`) is what makes
  re-reads deterministic: the answer does not depend on exactly when `now` falls.
- **Hold:** `changed_at + N <= now`. Uses the same `now` the phase already receives. A removal
  failing the hold is read but not carded (counted as skipped, indistinguishable from
  already-carded in the result counter); it is re-evaluated next sweep. Once the hold passes, the
  forward window is fully observed and the forward check is deterministic.

### 6. Removal-only scope

Attachment carding (`run_credit_attachment_events`, `_uncarded_credits`,
`_latest_credit_event_types`) is unchanged. Flap dampening is the composition of the held/suppressed
removal and the existing removal-aware suppression of the re-attachment. No new logic on the
attachment path.

### 7. Backfill posture — forward-only, grandfathered

The existing `scripts/backfill_credit_removals.py` is updated to pass `dwell_days` (from settings)
and `now` (current time) into `_card_detachment_group`. Because the backfill reads all history
(no lookback), every backlog removal is old enough that the hold trivially passes; only the
forward-reattachment check applies. Idempotent via `_already_carded` + `uq_event_catalog_change`.

**Forward-only** means: removals already carded by the NEU-1200 ship backfill — including the
reported Maya Boyd 4-card chain (NEU-1200 shipped 2026-08-28; its backfill carded the 8/25 and 8/28
removals) — are skipped by `_already_carded` and left in place. No destructive `DELETE`. Only
oscillation observed after this gate ships is dampened. The grandfathered chain is collapsed-section
`rumored` and low-harm; the trade (no destructive cleanup of published events) is the Q4 decision.

### 8. Transient invariant violation (the one real cost)

During the N-day hold, the removal is not yet carded. So for ≤N days the latest *carded* event for
the person is still the attachment, while TMDB says removed — a **transient** violation of NEU-1200's
*latest card = current state* invariant. Accepted as the documented cost of flap dampening:

- **Bounded** (≤N days, N=3 by default).
- **Self-correcting**: once the hold passes, a final departure cards (correcting the timeline); a
  flap's removal is suppressed and the re-attachment was already suppressed, so no stale tail.
- **Confined** to the collapsed `rumored` "via TMDB" section — the transient mismatch is invisible
  on the feed's expanded section and on the film page's news section.
- **Strictly better** than the alternatives (permanent stale under backward dwell; 4-card chain under
  status quo; retracting published events, which the ticket explicitly rejected).

Recorded as a scoped, dated relaxation in the ADR-0014 amendment.

### 9. ADR-0014 amendment + CONTEXT.md update

A dated amendment to ADR-0014 (alongside the NEU-1200 one) records: the forward-dwell choice, the
empirical basis, the rejection of backward dwell, and the transient-invariant acceptance. CONTEXT.md's
"Credit detachment event" entry is amended to note the dwell hold, and a "Credit oscillation"
glossary term is added.

## Implementation plan

### Config (`src/upmovies/config.py`)

Add after `sweep_event_lookback_days` (line 59):

```python
# How long a credit detachment must age before it is eligible to card, so a rapid TMDB
# re-attachment (a flap) is observed and suppressed rather than carded as a real departure
# (NEU-1205). Forward-dwell: a removal cards only if the person does NOT re-attach within N
# days after it. 0 disables the gate (reverts to plain NEU-1200). Must be <
# SWEEP_EVENT_LOOKBACK_DAYS so a held removal is still in the rolling window when it becomes
# eligible; the backfill backstops a mis-tuned N above the lookback. Re-verify against prod.
sweep_credit_dwell_days: int = Field(default=3, ge=0, alias="SWEEP_CREDIT_DWELL_DAYS")
```

### Forward-reattachment gate (`src/upmovies/ingest/sweep/credit_events.py`)

1. Add `_has_forward_reattachment(session, *, film_id, person_id, role, after, until) -> bool` —
   EXISTS a `FilmCreditChange` row with `change == CREDIT_ADDED`, `film_id`, `person_id`,
   `changed_at` in `[after, until)`, whose `credit_role(credit_type, job)` equals `role`. Reads raw
   history (not `Event`) and is role-scoped (Technical decisions 2 & 3). The role match is done by
   loading candidate `added` rows `(credit_type, job)` in the window and filtering in Python with
   `credit_role`, since the table stores no `role` column.

2. Preserve `person_id` and `role` through grouping: `DetachmentGroup.credits` becomes
   `tuple[DetachedCredit, ...]` (the local dataclass, which carries `person_id`, `name`, `role`),
   not `tuple[CreditDetached, ...]` (the deterministic dataclass, which has only `role`, `name`).
   `group_detachments` appends the full `DetachedCredit` instead of stripping to `CreditDetached`.
   `_card_detachment_group` builds `CreditDetached(role=c.role, name=c.name)` when constructing the
   `CreditsDetached` summary, so the deterministic renderer is unaffected.

### Hold + gate composition (`_card_detachment_group`)

New signature: `_card_detachment_group(session, *, group, now, dwell_days) -> bool`. Logic:

1. `_already_carded` → return False (idempotent, unchanged).
2. **Hold** (if `dwell_days > 0`): if `group.changed_at + timedelta(days=dwell_days) > now`,
   return False (held, not carded this pass; re-evaluated next sweep).
3. **Prior-attachment gate** (unchanged): per person, `_has_prior_attachment_card` (reads `Event`);
   keep only persons with a prior visible attachment card.
4. **Forward-reattachment gate** (if `dwell_days > 0`): per remaining person,
   `_has_forward_reattachment(session, film_id=group.film_id, person_id=c.person_id, role=c.role,
   after=group.changed_at, until=group.changed_at + timedelta(days=dwell_days))`; drop persons with a
   forward re-attach (flaps).
5. If none remain → return False.
6. Create the `Event` + `write_deterministic_summary` with `CreditsDetached(credits=tuple(
   CreditDetached(role=c.role, name=c.name) for c in gated))` — unchanged from NEU-1200.

### Phase wiring (`run_credit_detachment_events` + `pipeline_run.py`)

1. `run_credit_detachment_events` gains a `dwell_days: int` parameter (threaded from
   `settings.sweep_credit_dwell_days`), passed to each `_card_detachment_group` call alongside
   `now`.
2. `pipeline_run.py` (line 246): add `dwell_days=settings.sweep_credit_dwell_days` to the
   `run_credit_detachment_events` call.
3. `now` is already passed and shared with the field/attachment phases — the hold uses the same
   `now`.

### Backfill (`scripts/backfill_credit_removals.py`)

Pass `dwell_days` (from `settings`) and `now` (current UTC time) into `_card_detachment_group`.
All backlog removals are old → hold trivially passes → only the forward check applies. Idempotent.

### Deploy checklist

1. Code default (`SWEEP_CREDIT_DWELL_DAYS=3`).
2. `docker-compose.prod.yml`: add `SWEEP_CREDIT_DWELL_DAYS=3`.
3. Coolify UI: set the env var.
4. `printenv SWEEP_CREDIT_DWELL_DAYS` on the running container to confirm.
5. Re-verify the flap forward-gap distribution against prod; adjust N if prod shows flaps >3 days.

### ADR-0014 + CONTEXT.md

1. Amend ADR-0014 with a dated NEU-1205 note (forward-dwell, empirical basis, backward-dwell
   rejection, transient-invariant acceptance).
2. Amend CONTEXT.md "Credit detachment event" entry: note the dwell hold.
3. Add CONTEXT.md "Credit oscillation" glossary term.

## Tests

### Forward-dwell gate (`tests/integration/ingest/sweep/test_credit_events.py`)

- `test_flap_suppressed_when_reattach_within_window` — P attaches (carded), removed, re-attached
  within N days, removal now ≥ N days old → no `credit_removed` card (flap suppressed).
- `test_final_departure_carded_when_no_reattach` — P attaches (carded), removed, no re-attach,
  removal ≥ N days old → `credit_removed` card created.
- `test_held_removal_not_carded_within_window` — P attaches (carded), removed, now < N days after
  removal → no card yet (held); verify it is not counted as a failure.
- `test_held_removal_cards_after_window_passes` — same setup, first pass holds; advance `now` past
  `changed_at + N` with no re-attach → second pass cards.
- `test_flap_then_final_departure_cards_only_final` — Maya Boyd sequence (add/remove/add/remove):
  assert exactly two cards — the original `casting` attachment and the final `credit_removed`; the
  flap's removal and re-attachment are both suppressed.
- `test_per_person_gate_in_group` — one flap + one final removed in one observation (same
  `changed_at`) → one `credit_removed` card naming only the final person.
- `test_forward_gate_reads_raw_history_not_events` — flap whose re-attachment was suppressed (never
  carded) is still detected → removal suppressed (proves the gate reads `film_credit_change`, not
  `Event`).
- `test_forward_gate_role_scoped` — P removed as cast, re-added as director within N → cast
  `credit_removed` still cards (cross-role re-attach is not a flap); director `crew_attached` cards
  (removal-aware suppression does not suppress cross-type).
- `test_dwell_zero_disables_gate` — `dwell_days=0`, flap with prior attachment → `credit_removed`
  carded (reverts to plain NEU-1200; no hold, no forward check).
- `test_determinism_reread_stable` — card a final removal; re-read the window → skipped
  (`_already_carded`), not re-carded. Hold a removal; re-read within the hold → still held, not
  carded. (Monotonic held→carded, never carded→skipped.)
- `test_prior_attachment_gate_still_applies` — baseline credit (never carded) removed, ≥ N days old
  → no `credit_removed` card (unchanged from NEU-1200).
- `test_forward_window_half_open` — re-attachment at exactly `changed_at + N` is outside the
  window → removal cards (boundary re-attach is not a flap).

### Backfill (`tests/...` or script smoke)

- `test_backfill_applies_forward_gate` — backlog with a flap and a final; backfill cards only the
  final (hold trivially passes for old rows).
- `test_backfill_skips_already_carded` — removal carded by a prior NEU-1200 backfill → skipped
  (forward-only; no destructive cleanup).

### Pipeline (`tests/integration/test_pipeline_run.py`)

- `test_sweep_threads_dwell_days` — `run_credit_detachment_events` called with
  `dwell_days=settings.sweep_credit_dwell_days`.

## Out-of-scope reminders

- Backward dwell, attachment-card suppression, destructive cleanup of grandfathered cards,
  release-date withdrawals, notifications, story-side dedup for removals, frontend changes — all
  unchanged from NEU-1200 or explicitly rejected above.
