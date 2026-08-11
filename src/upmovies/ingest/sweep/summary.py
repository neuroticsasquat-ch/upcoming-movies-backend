"""The sweep's `ingest_run.detail` line.

All four phases share one run row, so the four sets of counters have to be legible side by
side: the failure this exists to make visible is a run that enumerated fine and refreshed
nothing, which is otherwise indistinguishable from a healthy pass on `/admin/runs` — and costs
the whole catalog-sourced-event feature (spec §6.2). The two event phases are on the same line
for the same reason: they are where that cost finally shows up as a number, and they fail
independently — a reader that stopped carding credits is invisible next to a healthy
field-change count.
"""

from upmovies.ingest.sweep.credit_events import CreditEventResult
from upmovies.ingest.sweep.enumerate_phase import EnumerateResult
from upmovies.ingest.sweep.field_events import FieldEventResult
from upmovies.ingest.sweep.refresh_phase import RefreshResult


def sweep_detail(
    enumerated: EnumerateResult,
    refreshed: RefreshResult,
    carded: FieldEventResult,
    attached: CreditEventResult,
) -> str:
    """One line reporting all four phases distinctly, for `finalize_run(detail=...)`."""
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
        f"credits: {attached.events_created} carded from "
        f"{attached.attachments_read} attachments, "
        f"{attached.skipped} already carded, {attached.failures} failed",
    ]
    if enumerated.aborted:
        parts.append(f"enumerate aborted: {enumerated.abort_error}")
    if refreshed.aborted:
        parts.append(f"refresh aborted: {refreshed.abort_error}")
    if carded.aborted:
        parts.append(f"events aborted: {carded.abort_error}")
    if attached.aborted:
        parts.append(f"credits aborted: {attached.abort_error}")
    return "; ".join(parts)
