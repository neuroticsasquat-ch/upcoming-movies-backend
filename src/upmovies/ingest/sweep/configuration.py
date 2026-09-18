"""The sweep's boot-time settings check, beside the LLM, mail and rate-limit ones.

The sweep's tuned constants are not independent of each other: its carding phases hold a
change back for a while and then read it out of a *rolling window*, so a hold longer than the
window is a hold that never ends. The failure is silent — nothing raises, nothing fails, the
phase simply cards less and less until it cards nothing — and it is invisible on the detail
line, where a held item and an item that has aged out of the window both read as "not carded
this pass". A setting that can quietly switch a feature off is one to refuse at boot.

The ceiling is a sweep period *plus slack* below the lookback, not one hour below it
(NEU-1401). A hold is never observed continuously: `quarantine_attachments` gates on
`changed_at + quarantine_hours <= now`, and `now` is only ever a sweep pass. The sweep runs
once a day, two hours *ahead* of the `tmdb` pass that stamps `changed_at`, so eligibility
falling just after a pass waits for the next one and the **effective hold** is up to a sweep
period longer than the number configured — NEU-1372 measured a nominal 72h holding for ~94h.
Comparing the *nominal* window against the window therefore admits settings that produce
exactly the silent failure this check exists to refuse: at a 7-day lookback a nominal 150h
passes `150 < 168`, but a pass at `T` sees only `changed_at` in `[T-168h, T-150h]` — an 18h
band against a ~24h period, so rows landing in the remaining 6h are eligible at no pass at all
and age out unread.

Only the *attachment* quarantine is enforced here, not NEU-1205's `SWEEP_CREDIT_DWELL_DAYS`,
which carries the same documented constraint and rounds up to the next pass in exactly the same
way. The asymmetry is deliberate, and it is about what recovers from the mistake rather than
about the arithmetic: a mis-tuned dwell loses removals that the removal backfill
(`scripts/backfill_credit_removals.py`) can re-read from full history, while nothing re-reads
an attachment that ages out unheld —
`_already_carded` never fires for a card that was never written, so the beat is simply gone.
"""

import math

from upmovies.config import Settings

HOURS_PER_DAY = 24

# The most the effective hold can exceed the nominal window by. One daily sweep pass, plus one
# skipped or shifted pass: NEU-1372 counted 24 sweep runs over 21 days, two of them at 04:00
# rather than the usual 07:00, so the interval is neither reliably 24h nor reliably aligned, and
# a guard whose whole job is to refuse a silently-lossy setting should not itself rest on an
# interval the data contradicts. Named for the rounding it corrects rather than for the schedule
# — 48 is not the sweep period, and a reader looking up the cron interval here would otherwise
# find a false answer. Not a `Settings` field: it tracks Coolify's schedule, but a wrong value in
# the Coolify UI would re-open this bug rather than close it (NEU-1401, D-1401.1/2).
SWEEP_HOLD_ROUNDING_HOURS = 48


class SweepConfigurationError(RuntimeError):
    """The sweep's tuned constants contradict each other, in a way that would cost events
    rather than raise. Raised at startup so a mis-tuned window fails the container rather
    than the nightly pass — the same class of guard as `StageConfigurationError` for LLM
    routing and `MailConfigurationError` for sends."""


def _ceiling_hours(lookback_hours: int) -> int:
    """The largest nominal hold whose effective hold still fits inside `lookback_hours`.

    Can be non-positive: `SWEEP_EVENT_LOOKBACK_DAYS` is `ge=1`, so a 1-day lookback yields
    -24h, meaning no non-zero hold is safe there at all. Callers handle that case rather than
    clamping it, because a negative ceiling is not a number to show an operator.
    """
    return lookback_hours - SWEEP_HOLD_ROUNDING_HOURS


def validate_sweep_configuration(settings: Settings) -> None:
    """Assert the sweep's holds fit inside the window the held rows are re-read from.

    Collects every fault before raising, for the reason `validate_mail_configuration` does: a
    deploy that mis-set two of them should learn about both from one failed boot.
    """
    problems: list[str] = []
    lookback_days = settings.sweep_event_lookback_days
    lookback_hours = lookback_days * HOURS_PER_DAY
    quarantine_hours = settings.sweep_credit_quarantine_hours
    ceiling_hours = _ceiling_hours(lookback_hours)
    # `0` is the disable switch, not a window, so it is exempt rather than trivially valid:
    # saying so here is what stops a future `>= 1` floor being read into this comparison.
    if quarantine_hours > 0 and quarantine_hours > ceiling_hours:
        effective_hold = quarantine_hours + SWEEP_HOLD_ROUNDING_HOURS
        # The smallest lookback that would admit the configured value. Named because the
        # remedy is to raise the lookback *first*, in the same Coolify edit (AGENTS.md).
        minimum_lookback_days = math.ceil(effective_hold / HOURS_PER_DAY)
        preamble = (
            f"SWEEP_CREDIT_QUARANTINE_HOURS is {quarantine_hours}, but a hold is only ever "
            f"observed at a sweep pass, so its effective hold can reach {effective_hold}h"
        )
        if ceiling_hours > 0:
            problems.append(
                f"{preamble} — past the {lookback_hours}h rolling window "
                f"(SWEEP_EVENT_LOOKBACK_DAYS={lookback_days}). Held attachments would age out "
                f"unread and never card. The ceiling at this lookback is {ceiling_hours}h; for "
                f"{quarantine_hours}h, raise SWEEP_EVENT_LOOKBACK_DAYS to "
                f"{minimum_lookback_days}."
            )
        else:
            problems.append(
                f"{preamble}, and a {lookback_hours}h rolling window "
                f"(SWEEP_EVENT_LOOKBACK_DAYS={lookback_days}) has no room for any non-zero "
                f"hold. Held attachments would age out unread and never card. Set "
                f"SWEEP_CREDIT_QUARANTINE_HOURS to 0 to disable the hold, or raise "
                f"SWEEP_EVENT_LOOKBACK_DAYS to {minimum_lookback_days}."
            )
    if problems:
        raise SweepConfigurationError(
            "sweep configuration is unusable:\n  " + "\n  ".join(problems)
        )
