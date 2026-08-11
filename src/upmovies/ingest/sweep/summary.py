"""The sweep's `ingest_run.detail` line.

All five phases share one run row, so the four sets of counters have to be legible side by
side: the failure this exists to make visible is a run that enumerated fine and refreshed
nothing, which is otherwise indistinguishable from a healthy pass on `/admin/runs` — and costs
the whole catalog-sourced-event feature (spec §6.2). The two event phases are on the same line
for the same reason: they are where that cost finally shows up as a number, and they fail
independently — a reader that stopped carding credits is invisible next to a healthy
field-change count.

Both phases report **missing** (TMDB 404) apart from **failed**, because conflating them is
what hid the 2026-08-11 wedge: fifty dead person ids reported as "50 failed" read as a flaky
TMDB, and eleven dead film ids were what actually aborted the run (NEU-1124). A missing count
that climbs is a catalog-hygiene signal; a failed count that climbs is an outage.

The enumerate clause reports admissions against skips *by reason*, in the `tmdb` stage's
shared `format_skip_detail` form: while the ramp is in progress the operating question is
what the open tranche let in and what stopped the rest, and a bare total leaves "the tranche
is still closed" and "they were all below the corroboration threshold" — one an env change
away from each other — indistinguishable (NEU-1086).
"""

from upmovies.ingest.runs import format_skip_detail
from upmovies.ingest.sweep.credit_events import CreditEventResult
from upmovies.ingest.sweep.enumerate_phase import EnumerateResult
from upmovies.ingest.sweep.field_events import FieldEventResult
from upmovies.ingest.sweep.refresh_phase import RefreshResult
from upmovies.ingest.sweep.release_events import ReleaseEventResult


def sweep_detail(
    enumerated: EnumerateResult,
    refreshed: RefreshResult,
    carded: FieldEventResult,
    attached: CreditEventResult,
    released: ReleaseEventResult,
) -> str:
    """One line reporting all five phases distinctly, for `finalize_run(detail=...)`."""
    parts = [
        f"enumerate: {enumerated.seed_people} seeds "
        f"({enumerated.person_missing} missing), "
        f"{enumerated.candidates_found} candidates, "
        f"{enumerated.admitted} admitted, "
        f"{format_skip_detail(enumerated.skip_counts)}, "
        f"{enumerated.person_failures + enumerated.candidate_failures} failed",
        f"refresh: {refreshed.refreshed}/{refreshed.selected} refreshed "
        f"({refreshed.dormant_selected} dormant), {refreshed.missing} missing, "
        f"{refreshed.failures} failed",
        f"events: {carded.events_created} carded from {carded.changes_read} changes, "
        f"{carded.skipped} already carded, {carded.failures} failed",
        f"credits: {attached.events_created} carded from "
        f"{attached.attachments_read} attachments, "
        f"{attached.skipped} already carded, {attached.failures} failed",
        f"release dates: {released.events_created} carded from "
        f"{released.changes_read} changes, "
        f"{released.skipped} already carded, {released.failures} failed",
    ]
    if enumerated.aborted:
        parts.append(f"enumerate aborted: {enumerated.abort_error}")
    if refreshed.aborted:
        parts.append(f"refresh aborted: {refreshed.abort_error}")
    if carded.aborted:
        parts.append(f"events aborted: {carded.abort_error}")
    if attached.aborted:
        parts.append(f"credits aborted: {attached.abort_error}")
    if released.aborted:
        parts.append(f"release dates aborted: {released.abort_error}")
    return "; ".join(parts)
