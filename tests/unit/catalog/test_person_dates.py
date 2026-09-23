"""The one encoding of "these dates are inconsistent with a credit on this day" (D-8, D-21).

Read by the sweep's sanity holds and by person resolution's scoring, which is why it is tested
here rather than in either of theirs.
"""

from datetime import date

from upmovies.catalog.person_dates import (
    DECEASED,
    IMPLAUSIBLE_AGE,
    date_contradiction,
    years_before,
)
from upmovies.ingest.models import HOLD_DECEASED, HOLD_IMPLAUSIBLE_AGE

DAY = date(2026, 9, 18)


def contradiction(**overrides) -> str | None:
    fields: dict = {
        "birthday": None,
        "deathday": None,
        "on": DAY,
        "posthumous_years": 2,
        "min_age_years": 3,
    }
    fields.update(overrides)
    return date_contradiction(**fields)


def test_years_before_moves_whole_years():
    """Whole years, not `365 * n` days: both checks are stated in years and are read against
    birthdays, so "under 3 at the time" has to mean what it means on a passport."""
    assert years_before(date(2026, 9, 18), 3) == date(2023, 9, 18)
    assert years_before(date(2026, 9, 18), 2) == date(2024, 9, 18)


def test_years_before_lands_a_leap_day_on_the_28th():
    """29 February has no counterpart in a common year, and `replace` raises rather than
    guessing — so the guess is made here, in the direction that never *shortens* the bar."""
    assert years_before(date(2024, 2, 29), 3) == date(2021, 2, 28)


def test_a_long_dead_person_contradicts_the_day():
    assert contradiction(deathday=date(1998, 3, 4)) == DECEASED


def test_a_recent_death_is_inside_the_posthumous_window():
    """A film completed before the death, archive footage, a voice recorded years earlier —
    all ordinary, and all landing inside a couple of years."""
    assert contradiction(deathday=date(2025, 9, 18)) is None
    # The window's own edge: two years to the day is still inside it, a day earlier is not.
    assert contradiction(deathday=date(2024, 9, 18)) is None
    assert contradiction(deathday=date(2024, 9, 17)) == DECEASED


def test_someone_too_young_on_the_day_contradicts_it():
    assert contradiction(birthday=date(2025, 1, 1)) == IMPLAUSIBLE_AGE
    assert contradiction(birthday=date(2010, 1, 1)) is None


def test_an_absent_date_never_contradicts_anything():
    """`catalog.person` cannot tell "no death recorded" from "alive", and most people have no
    birthday there at all — reading an absence as evidence would disqualify everyone TMDB is
    merely thin on."""
    assert contradiction() is None
    assert contradiction(birthday=None, deathday=None) is None


def test_a_zero_window_turns_its_own_check_off():
    """Both callers pass positive windows, but the guard is what keeps a zero from meaning
    "hold everybody TMDB holds a date for"."""
    assert contradiction(deathday=date(1998, 3, 4), posthumous_years=0) is None
    assert contradiction(birthday=date(2025, 1, 1), min_age_years=0) is None


def test_death_is_tested_before_age():
    """A date of death is recorded because somebody checked; a birthday is one of TMDB's
    thinnest fields, so the sharper fact names the contradiction."""
    assert contradiction(birthday=date(2025, 1, 1), deathday=date(1998, 3, 4)) == DECEASED


def test_the_reason_strings_are_the_ones_the_hold_table_is_constrained_to():
    """`ingest.models` aliases these two rather than respelling them, so the constants agree by
    construction — but `ingest.credit_hold.reason`'s CHECK constraint holds the *literals* in
    migration SQL, which no alias reaches. Renaming either value without a migration would
    make every date hold fail to insert, so the literals are pinned here."""
    assert (DECEASED, IMPLAUSIBLE_AGE) == ("deceased", "implausible_age")
    assert (HOLD_DECEASED, HOLD_IMPLAUSIBLE_AGE) == (DECEASED, IMPLAUSIBLE_AGE)
