# NEU-1401 — `validate_sweep_configuration` must guard the effective hold, not the nominal window

**Ticket:** [NEU-1401](https://linear.app/neuroticsasquatch/issue/NEU-1401/validate-sweep-configuration-checks-the-nominal-quarantine-window-not)
**Parent story:** NEU-1333 · **Milestone:** M5 — Quarantine and the publication queue
**Decision:** D-3 (`docs/specs/bl-consumer-pivot-project-spec.md` §5)
**Origin:** `docs/specs/bl-consumer-pivot-m05-credit-survival.md` (NEU-1372), finding 2 and follow-up 1
**Target repo:** upcoming-movies-backend · **Not urgent, latent at today's defaults**

## What to build and why

`ingest/sweep/configuration.py` refuses a boot when
`SWEEP_CREDIT_QUARANTINE_HOURS >= SWEEP_EVENT_LOOKBACK_DAYS * 24`. That compares the **nominal**
window against the rolling window, but the nominal window is not what the sweep holds for.

`quarantine_attachments` (`credit_events.py:395`) gates on `changed_at + quarantine_hours <= now`,
and `now` is only ever a sweep pass. The sweep runs once a day, two hours *ahead* of the `tmdb`
pass that stamps `changed_at`, so eligibility that falls just after a pass waits for the next one:
the **effective hold** is up to one sweep period longer than the number configured. NEU-1372
measured it — a nominal 72h is a real hold of ~94h.

So the guard admits settings that produce exactly the silent failure it was written to refuse.
At today's defaults (`SWEEP_EVENT_LOOKBACK_DAYS` = 7 → 168h), a nominal **150h** passes, since
150 < 168. At a sweep at `T`, `load_attachment_backlog` sees `changed_at ∈ [T-168h, T-150h]` — an
18h band against a ~24h sweep period. Rows whose `changed_at` lands in the remaining 6h are
eligible at no pass at all: they age out of the rolling window unread and never card. Nothing
raises, nothing fails, the credit phase just cards less — and per the module's own docstring,
"nothing re-reads an attachment that ages out unheld".

The fix is to compare against the effective hold: refuse when
`quarantine_hours + SWEEP_HOLD_ROUNDING_HOURS > lookback_hours`.

**Why now, given it is latent.** The bug bites nobody today (72 + 48 = 120 < 168) and would not
bite at NEU-1372's recommended 96 (96 + 48 = 144 < 168). It is fixed now so that the next person
to tune the window — which NEU-1372 explicitly invites — does not walk into it, and because the
guard's *purpose* is to be trustworthy at the moment someone changes the value.

## Key decisions

### D-1401.1 — The slack constant is 48h, not the 24h nominal period

`SWEEP_HOLD_ROUNDING_HOURS = 48`, documented as one daily sweep plus one skipped or shifted pass.

The ticket proposed one sweep period (24h) and asked whether it should carry slack. It should.
The 24h assumption is measurably optimistic: NEU-1372 reports 24 sweep runs over 21 days, two of
them at 04:00 rather than 07:00, so the interval is neither reliably 24h nor reliably aligned. A
guard whose whole job is to refuse a silently-lossy setting should not itself rest on an interval
the data contradicts. 48h survives exactly the failure the evidence shows.

The ceiling becomes `lookback_hours - 48` = **120h** at today's defaults.

**This costs nothing deployed.** 72 (today) and 96 (NEU-1372's recommendation) both remain valid,
with 48h and 24h of slack respectively. What 48 rejects is the 121–167h band, which no analysis
has asked for and which NEU-1372's own curve argues against — 96h → 168h buys only +9pp of
coverage for three further days of delay.

*Rejected:* 24h flat (the ticket's proposal — fixes the arithmetic but leaves a guard known to be
optimistic about the thing it guards). A separate `SWEEP_PERIODS_OF_SLACK = 2` multiplier (same
120h ceiling, one more name to carry for a factor that is not independently tunable). Making it a
`Settings` field (more faithful to "the sweep period is Coolify's schedule", but it adds a tuned
constant to the AGENTS.md deploy checklist for a value that has never changed, and a wrong value
there re-opens the bug rather than closing it).

### D-1401.2 — It is named for the rounding it corrects, not for the schedule

`SWEEP_HOLD_ROUNDING_HOURS`, not `SWEEP_PERIOD_HOURS`.

A constant holding 48 cannot honestly be called the sweep period — a future reader looking up the
cron interval would find a false answer. "Rounding" is also the word NEU-1372 already uses ("Every
window silently rounds up to the next sweep pass"), so the constant, the spike and CONTEXT.md all
say the same thing. It reads correctly at the comparison site: the nominal hold plus the most it
can round up by must fit inside the window.

*Rejected:* `SWEEP_PERIOD_HOURS = 48` (the ticket's name, states something untrue).
`EFFECTIVE_HOLD_SLACK_HOURS` (honest, but says less about where the 48 comes from).

### D-1401.3 — The failure message carries the nominal value, the ceiling and the remedy

The guard fails a container boot in `pipeline_run` only, so its message is the entire diagnostic —
there is no run to inspect afterwards and no held row to find. It must name:

- the configured `SWEEP_CREDIT_QUARANTINE_HOURS`;
- why the effective hold exceeds it (observed only at a sweep pass);
- the resulting effective-hold figure and the `lookback_hours` it passes;
- the consequence, in the existing message's terms (held attachments age out unread, never card);
- the **ceiling** at this lookback, and the smallest `SWEEP_EVENT_LOOKBACK_DAYS` that would admit
  the configured value — `ceil((quarantine_hours + 48) / 24)` days.

Shape (numbers from the boundary case):

```
sweep configuration is unusable:
  SWEEP_CREDIT_QUARANTINE_HOURS is 150, but a hold is only ever observed at a sweep
  pass, so its effective hold can reach 198h — past the 168h rolling window
  (SWEEP_EVENT_LOOKBACK_DAYS=7). Held attachments would age out unread and never card.
  The ceiling at this lookback is 120h; for 150h, raise SWEEP_EVENT_LOOKBACK_DAYS to 9.
```

Naming the remedy is consistent with `AGENTS.md:103`, which already tells the operator to raise
the lookback *first*, in the same Coolify edit.

### D-1401.4 — A private ceiling helper; tests assert literal boundaries

A module-private `_ceiling_hours(lookback_hours)` serves the comparison and the message. The
module's public surface stays at `validate_sweep_configuration` + `SweepConfigurationError`,
which is all `pipeline_run.py:517` needs.

Tests assert behaviour against **literal** hour values (accepts 120, refuses 121) rather than
calling the helper. A boundary test that computes its own expectation from the constant cannot
fail when the constant moves, which is the one thing these tests exist to catch.

*Rejected:* exporting `quarantine_ceiling_hours` (a second public name on a module whose whole job
is one boot check; no caller wants it yet). Inlining the expression twice (small duplication, but
the message needs the derived minimum-lookback too, so a helper pays for itself).

### D-1401.5 — A non-positive ceiling refuses every non-zero hold, and says so

`SWEEP_EVENT_LOOKBACK_DAYS` is `ge=1`, so a 1-day lookback gives a ceiling of `24 - 48` = −24h. No
non-zero quarantine value is safe there, and that is the correct outcome, not an edge case to
paper over: with a one-day window there is no room to hold anything and still re-read it.

The message must not print a negative ceiling. When the ceiling is `<= 0`, say that no non-zero
hold fits inside this lookback and that the options are `0` (disable) or a longer lookback.
`quarantine_hours = 0` stays exempt at any lookback — it is the disable switch, not a window —
which keeps `test_zero_is_the_disable_switch_and_not_a_window` (lookback 1) valid as written.

### D-1401.6 — The dwell gate gets a sentence, not a guard

`SWEEP_CREDIT_DWELL_DAYS` (NEU-1205) rounds up identically, and stays **deliberately unguarded**.
The asymmetry NEU-1368 documented is unchanged and is about recovery, not about arithmetic: a
mis-tuned dwell loses removals that `scripts/backfill_credit_removals.py` can re-read from full
history, while nothing re-reads an attachment that ages out unheld. Record that the rounding
applies there too — in `configuration.py`'s module docstring, which is where the asymmetry is
already argued, and as a clause on the `config.py` dwell comment whose "Must be <
`SWEEP_EVENT_LOOKBACK_DAYS`" is now understated by the same 48h.

At the default 3 days (72h) the dwell has 48h of headroom against the 168h window, so nothing is
at risk today either.

### D-1401.7 — NEU-1372's follow-up 2 is absorbed here

`config.py:88-89` says the window is "set from a survival curve that turns over inside a day".
NEU-1372 disproved that and found it *cannot be observed* at a daily ingest cadence: the log is
stamped almost entirely in one hour a day, nothing reverts before 18h, the mass sits at 24–36h,
and the curve has a genuine heavy tail past 96h. The comment is four lines above the constraint
sentence this ticket rewrites; leaving a known-false claim in a comment block being edited is
worse than the extra line of diff. Corrected here, and recorded as absorbed so the spike's
follow-up 2 is neither lost nor done twice.

Follow-ups **3** (stamp `credit_order` onto `film_credit_change`) and **4** (the per-film flap
check for D-8, which NEU-1372 calls its highest-value follow-up) stay out of scope and remain
unticketed.

### D-1401.8 — Operator docs ship with the code, not ahead of it

`AGENTS.md`, `CONTEXT.md` and `docker-compose.prod.yml` all state the old constraint, and they are
updated in this ticket's commits rather than now, at planning time. NEU-1368 set that precedent:
`AGENTS.md:97-104` documents the guard as shipped. Documenting a ceiling that is not yet enforced
would make the repo's own operator checklist wrong in the window between planning and merge.

## Changes

**`src/upmovies/ingest/sweep/configuration.py`**
- Add `SWEEP_HOLD_ROUNDING_HOURS = 48` with a comment carrying the justification: one daily pass
  plus one skipped or shifted pass, citing NEU-1372's 24-runs-over-21-days cadence measurement.
- Add `_ceiling_hours(lookback_hours)`.
- Change the comparison to `quarantine_hours > 0 and quarantine_hours > _ceiling_hours(...)`.
- Rewrite the message per D-1401.3, including the non-positive-ceiling branch (D-1401.5).
- Extend the module docstring: why the ceiling is a sweep period *and slack* below the lookback
  rather than one hour below it, that the hold is only ever observed at a pass, and (D-1401.6)
  that the dwell rounds the same way and is still unguarded.

**`src/upmovies/config.py`**
- `sweep_credit_quarantine_hours` (~84-100): correct the constraint sentence from "Must be <
  `SWEEP_EVENT_LOOKBACK_DAYS` (in hours)" to the effective-hold ceiling, and correct the
  "turns over inside a day" claim (D-1401.7).
- `sweep_credit_dwell_days` (~77-83): add the rounding clause (D-1401.6).

**`tests/unit/ingest/sweep/test_configuration.py`**
- **Rewrite `test_a_window_inside_the_lookback_is_accepted`.** It currently accepts `7*24 - 1` =
  167h, which asserts the *old* ceiling and must now refuse. This is not a passing test that
  survives the change, and it is the one place the old rule is encoded as an expectation.
- Add the ticket's named boundary: **150h at a 7-day lookback is refused** — passes today and
  should not.
- Add ceiling boundaries: 121h refused, 120h accepted.
- Keep/extend: defaults valid (72); NEU-1372's recommended 96 valid; 168h and 167h refused; 0
  exempt at a 1-day lookback.
- Add: a 1-day lookback refuses any non-zero hold with the no-room message (D-1401.5).
- Assert the message names the ceiling and the suggested lookback, in the style of the existing
  `assert "SWEEP_CREDIT_QUARANTINE_HOURS" in str(err.value)` checks.

**Docs (shipped with the code, D-1401.8)**
- `AGENTS.md:97-104`: the bullet's headline ("must stay under `SWEEP_EVENT_LOOKBACK_DAYS`") and
  its "72 against 7 days = 168" arithmetic both become the ceiling rule — 72 against a 120h
  ceiling — with a sentence on why the hold rounds up. Keep the existing "raise the lookback
  first" advice and the "`0` is exempt" clause.
- `CONTEXT.md` **Quarantine** entry (~601-618): the clause "which is why the hold must stay inside
  it (refused at boot by `validate_sweep_configuration`)" gains the rounding — the hold must fit
  inside the window *with room for the pass that observes it*. Introduce **effective hold** as the
  term for the real delay, distinct from the nominal setting.
- `docker-compose.prod.yml:123` (quarantine) and `:109` (dwell): same correction to the inline
  constraint comments.
- No ADR change: ADR-0017 discusses the quarantine window without asserting the numeric
  constraint.

## Acceptance criteria

1. `validate_sweep_configuration` refuses a nominal window whose effective hold could exceed the
   lookback: **150h at `SWEEP_EVENT_LOOKBACK_DAYS` = 7 raises `SweepConfigurationError`**, where
   today it passes.
2. The ceiling is `lookback_hours - 48`: 120h accepted, 121h refused, at a 7-day lookback.
3. The shipped defaults (72) and NEU-1372's recommendation (96) both validate.
4. `0` remains exempt at any lookback, including `SWEEP_EVENT_LOOKBACK_DAYS` = 1.
5. A lookback whose ceiling is `<= 0` refuses every non-zero hold with a message that names no
   negative number and points at `0` or a longer lookback.
6. The failure message names the configured value, the effective hold, the ceiling and the
   smallest lookback that would admit the value.
7. The module docstring explains why the ceiling is a sweep period *plus slack* below the lookback
   rather than one hour below it, and notes that `SWEEP_CREDIT_DWELL_DAYS` rounds the same way and
   is deliberately unguarded.
8. `config.py`'s "turns over inside a day" claim is corrected against NEU-1372's findings.
9. `AGENTS.md`, `CONTEXT.md` and `docker-compose.prod.yml` state the ceiling rule, not the old
   nominal one.
10. `task format`, `task lint`, `task typecheck`, `task test` green.

## Out of scope / deferred

- **No guard for `SWEEP_CREDIT_DWELL_DAYS`** (D-1401.6) — documented only, per the ticket and
  NEU-1368's recovery-based asymmetry.
- **No change to `SWEEP_CREDIT_QUARANTINE_HOURS` itself.** Whether to take NEU-1372's recommended
  96 is a separate decision and a separate deploy; this ticket only makes the guard honest about
  the value that is set. Both 72 and 96 pass the new guard.
- **No `SWEEP_STORY_CONFIRM_DAYS` guard** — `config.py:115-117` already argues it out: mis-tuning
  it publishes a visible duplicate that is fixable forward, where a hold outliving its window
  publishes nothing at all.
- **No runtime detection of the real sweep interval.** Reading `ingest.ingest_run` to measure the
  actual cadence and deriving the slack from it would be more faithful, but a boot check that
  queries history to decide whether to boot is a much larger surface than this bug justifies. The
  constant is documented as an assumption with its evidence cited.
- **NEU-1372 follow-ups 3 and 4** (stamp `credit_order`; per-film flap check for D-8) — untouched,
  still unticketed.
- **No admin or observability surface** for the ceiling. It is a boot check; there is nothing to
  show in a running system.
