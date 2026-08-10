"""The sweep's `ingest_run.detail` line.

Both phases share one run row, so the two sets of counters have to be legible side by side:
the failure this exists to make visible is a run that enumerated fine and refreshed nothing,
which is otherwise indistinguishable from a healthy pass on `/admin/runs` — and costs the
whole catalog-sourced-event feature (spec §6.2).
"""

from upmovies.ingest.sweep.enumerate_phase import EnumerateResult
from upmovies.ingest.sweep.refresh_phase import RefreshResult


def sweep_detail(enumerated: EnumerateResult, refreshed: RefreshResult) -> str:
    """One line reporting both phases distinctly, for `finalize_run(detail=...)`."""
    parts = [
        f"enumerate: {enumerated.seed_people} seeds, "
        f"{enumerated.candidates_found} candidates, "
        f"{enumerated.admitted} admitted, "
        f"{enumerated.withheld} withheld, "
        f"{enumerated.person_failures + enumerated.candidate_failures} failed",
        f"refresh: {refreshed.refreshed}/{refreshed.selected} refreshed "
        f"({refreshed.dormant_selected} dormant), {refreshed.failures} failed",
    ]
    if enumerated.aborted:
        parts.append(f"enumerate aborted: {enumerated.abort_error}")
    if refreshed.aborted:
        parts.append(f"refresh aborted: {refreshed.abort_error}")
    return "; ".join(parts)
