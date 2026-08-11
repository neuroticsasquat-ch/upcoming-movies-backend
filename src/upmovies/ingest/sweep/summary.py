"""The sweep's `ingest_run.detail` line.

All three phases share one run row, so the three sets of counters have to be legible side by
side: the failure this exists to make visible is a run that enumerated fine and refreshed
nothing, which is otherwise indistinguishable from a healthy pass on `/admin/runs` — and costs
the whole catalog-sourced-event feature (spec §6.2). The events phase is on the same line for
the same reason: it is the phase where that cost finally shows up as a number.
"""

from upmovies.ingest.sweep.enumerate_phase import EnumerateResult
from upmovies.ingest.sweep.field_events import FieldEventResult
from upmovies.ingest.sweep.refresh_phase import RefreshResult


def sweep_detail(
    enumerated: EnumerateResult, refreshed: RefreshResult, carded: FieldEventResult
) -> str:
    """One line reporting all three phases distinctly, for `finalize_run(detail=...)`."""
    parts = [
        f"enumerate: {enumerated.seed_people} seeds, "
        f"{enumerated.candidates_found} candidates, "
        f"{enumerated.admitted} admitted, "
        f"{enumerated.withheld} withheld, "
        f"{enumerated.person_failures + enumerated.candidate_failures} failed",
        f"refresh: {refreshed.refreshed}/{refreshed.selected} refreshed "
        f"({refreshed.dormant_selected} dormant), {refreshed.failures} failed",
        f"events: {carded.events_created} carded from {carded.changes_read} changes, "
        f"{carded.skipped} already carded, {carded.failures} failed",
    ]
    if enumerated.aborted:
        parts.append(f"enumerate aborted: {enumerated.abort_error}")
    if refreshed.aborted:
        parts.append(f"refresh aborted: {refreshed.abort_error}")
    if carded.aborted:
        parts.append(f"events aborted: {carded.abort_error}")
    return "; ".join(parts)
