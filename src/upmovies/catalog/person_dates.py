"""Whether a person's birth and death dates are consistent with something happening to their
credits on a given day (D-8, D-21).

This lives in `catalog` rather than next to either consumer because there are two of them and
they must not drift apart, the same reason `catalog.seed_grade` does. The sweep asks it
*whether to hold an attachment before carding it* (`ingest.sweep.credit_events`, NEU-1370);
person resolution asks it *whether this candidate can be the person a story just named*
(`link.resolve.scoring`, NEU-1400). A resolver that scored "implausibly young" on a different
rule from the one the sweep holds on would be two answers to one question — and the two reach
it from opposite directions, one gating a write and one weighing evidence, which is exactly
when a rule drifts.

**Both tests are one-sided, and that is the whole design.** A NULL date never contradicts
anything: `catalog.person.deathday` cannot tell "no death recorded" from "alive", and most
people have no `birthday` there at all, so reading an absent date as evidence would disqualify
everyone TMDB is merely thin on. Absence of evidence is not evidence — see
`link.resolve.scoring` on why its feature weights are built the same way.

The two windows are parameters rather than constants here because the callers hold them
differently: the sweep's are operator-settable (`SWEEP_SANITY_*`), since they gate what gets
written, while scoring's are module constants, since they only move a weight.
"""

from datetime import date

DECEASED = "deceased"
"""A credit or mention arriving long enough after a recorded death to be implausible."""

IMPLAUSIBLE_AGE = "implausible_age"
"""A person too young, on the day in question, for the job being claimed."""


def years_before(day: date, years: int) -> date:
    """`day` moved back a whole number of years, 29 February landing on the 28th.

    Whole years rather than `365 * years` days because both checks are stated in years and are
    read by humans against birthdays: "under 3 at the time" has to mean the same thing as it
    does on a passport, leap days included.
    """
    try:
        return day.replace(year=day.year - years)
    except ValueError:
        return day.replace(year=day.year - years, day=28)


def date_contradiction(
    *,
    birthday: date | None,
    deathday: date | None,
    on: date,
    posthumous_years: int,
    min_age_years: int,
) -> str | None:
    """`DECEASED`, `IMPLAUSIBLE_AGE`, or None when these dates say nothing against `on`.

    A posthumous credit inside `posthumous_years` is ordinary — a film completed before the
    death, archive footage, a voice recorded years earlier — so the check is for credits and
    mentions that arrive long after, which is the shape vandalism, misfiles and same-name
    confusion take.

    Death is tested before age because it is the sharper fact: a date of death is recorded
    because somebody checked, while a `birthday` is one of TMDB's thinnest fields.
    """
    if (
        posthumous_years > 0
        and deathday is not None
        and deathday < years_before(on, posthumous_years)
    ):
        return DECEASED
    if min_age_years > 0 and birthday is not None and birthday > years_before(on, min_age_years):
        return IMPLAUSIBLE_AGE
    return None
