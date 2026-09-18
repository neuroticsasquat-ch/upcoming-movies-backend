"""The sweep's boot-time settings check, beside the LLM, mail and rate-limit ones.

The sweep's tuned constants are not independent of each other: its carding phases hold a
change back for a while and then read it out of a *rolling window*, so a hold longer than the
window is a hold that never ends. The failure is silent — nothing raises, nothing fails, the
phase simply cards less and less until it cards nothing — and it is invisible on the detail
line, where a held item and an item that has aged out of the window both read as "not carded
this pass". A setting that can quietly switch a feature off is one to refuse at boot.

Only the *attachment* quarantine is enforced here, not NEU-1205's `SWEEP_CREDIT_DWELL_DAYS`,
which carries the same documented constraint. The asymmetry is deliberate and is about what
recovers from the mistake: a mis-tuned dwell loses removals that the removal backfill
(`scripts/backfill_credit_removals.py`) can re-read from full history, while nothing re-reads
an attachment that ages out unheld —
`_already_carded` never fires for a card that was never written, so the beat is simply gone.
"""

from upmovies.config import Settings

HOURS_PER_DAY = 24


class SweepConfigurationError(RuntimeError):
    """The sweep's tuned constants contradict each other, in a way that would cost events
    rather than raise. Raised at startup so a mis-tuned window fails the container rather
    than the nightly pass — the same class of guard as `StageConfigurationError` for LLM
    routing and `MailConfigurationError` for sends."""


def validate_sweep_configuration(settings: Settings) -> None:
    """Assert the sweep's holds fit inside the window the held rows are re-read from.

    Collects every fault before raising, for the reason `validate_mail_configuration` does: a
    deploy that mis-set two of them should learn about both from one failed boot.
    """
    problems: list[str] = []
    lookback_hours = settings.sweep_event_lookback_days * HOURS_PER_DAY
    quarantine_hours = settings.sweep_credit_quarantine_hours
    # `0` is the disable switch, not a window, so it is exempt rather than trivially valid:
    # saying so here is what stops a future `>= 1` floor being read into this comparison.
    if quarantine_hours > 0 and quarantine_hours >= lookback_hours:
        problems.append(
            f"SWEEP_CREDIT_QUARANTINE_HOURS is {quarantine_hours}, which is not less than "
            f"SWEEP_EVENT_LOOKBACK_DAYS ({settings.sweep_event_lookback_days} days = "
            f"{lookback_hours} hours): an attachment held that long ages out of the rolling "
            f"window before it is eligible, and is never carded"
        )
    if problems:
        raise SweepConfigurationError(
            "sweep configuration is unusable:\n  " + "\n  ".join(problems)
        )
