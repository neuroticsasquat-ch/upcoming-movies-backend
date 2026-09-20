"""The sweep's `ingest_run.detail` line.

Every phase shares one run row, so their counters have to be legible side by
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

The credits clause reports **held** (NEU-1368) apart from both carded and already-carded,
because quarantine (D-3) makes "nothing carded" ambiguous: a pass that read an empty window
and a pass that withheld everything it read are otherwise the same line. It counts attachment
*rows*, not groups, unlike the counters either side of it — a held row has no group, because
grouping happens after the gate.

Read it against `attachments_read`, not on its own, and **not** as a health signal. It merges
the two reasons a row is withheld — still inside the window, and already reverted — and in
steady state the second dominates, which is the feature working rather than a fault. What it
answers is "did the gate see this backlog at all"; what it cannot answer is whether the window
is tuned right. The window leaving too little of the rolling lookback for the sweep pass that
observes it, the one tuning fault that would silently cost every attachment, is refused at boot
instead (`validate_sweep_configuration`).

The holds clause (NEU-1370) is its own, beside the credits clause rather than inside it,
because the two numbers are not the same kind. `held` merges quarantine's two reasons and is
not a health signal; every hold counted here is a specific, reviewable claim — a burst, a
death, an age — against a named person, and `new` climbing is worth looking at. The three
counts are also the only place the *release* paths are visible: `cleared` says the condition
lifted and those attachments carded on this same pass, while `expired` says a hold ran to the
end of the rolling window and its beat is simply gone. A steady `0 new, 0 cleared, 0 expired`
is the healthy state; `expired` rising without `new` rising is the shape of a threshold set
too tight.

There is no watchlist clause any more (M8, ADR-0018): the sweep's derivation phase is gone with
the table it wrote to, because the watchlist is computed from the follow graph on every read
and has nothing to maintain between passes.

The attachment clause is here because nowhere else keeps it: the histogram is built on every
sweep and was only ever logged, and Coolify runs the sweep through `docker exec`, whose output
never reaches `docker logs` and dies with the container. `detail` is the only durable outlet
for the distribution the M4 tuning ticket reads the threshold off (§4.3) — and, after the
directors flip, for what opening the writers tranche would admit (NEU-1089, NEU-1116).

The role clause is beside it for the same reason and answers the neighbouring question: not
*how many* people reached a candidate but *how*. It is what makes an undifferentiated
`no_tranche` count actionable — which flag would admit those films — and it is the only signal
that the `followed` enumeration (D-50) is reaching anything while `SWEEP_ADMIT_FOLLOWED` is
still off and every candidate it finds is withheld.
"""

from collections import Counter

from upmovies.catalog.seed_grade import ROLE_ORDER
from upmovies.ingest.runs import format_skip_detail
from upmovies.ingest.sweep.credit_events import CreditDetachmentResult, CreditEventResult
from upmovies.ingest.sweep.enumerate_phase import EnumerateResult
from upmovies.ingest.sweep.field_events import FieldEventResult
from upmovies.ingest.sweep.refresh_phase import RefreshResult
from upmovies.ingest.sweep.release_events import ReleaseEventResult

_HISTOGRAM_TAIL = 3
"""Seed-attachment counts at or above this are reported as one `3+` group, keeping the whole
of §4.3's table readable off one line: it reads cumulatively at ≥1, ≥2 and ≥3, and all three
survive the grouping (≥1 is the total, ≥2 the total less the first bucket, ≥3 the last one).
What it does cost is the region above 3 — §4.3 stopped there because the tranche got small,
not because ≥4 is uninteresting, so a retune that wants to look higher needs this raised
rather than the log it replaces."""


def _format_role_detail(histogram: Counter[str]) -> str:
    """The `roles: director×N, cast×N, followed×N` clause, or `""` when nothing was counted.

    Which roles reached the candidates that cleared status, counted per candidate — so a film
    two roles reached appears under both and the buckets do not sum to the candidate total.
    Here rather than only in the log for the reason the module docstring gives: the sweep's
    output never reaches `docker logs`, and `detail` is the only durable outlet.

    What it answers is which tranche a `no_tranche` skip count is waiting on, and since D-50
    whether the `followed` enumeration reaches anything — the one number that says the
    followed half is working *before* `SWEEP_ADMIT_FOLLOWED` is flipped, when every candidate
    it reaches is still being withheld.

    Rendered in `ROLE_ORDER`, strongest attachment first and `followed` last, so two runs'
    lines compare by eye. Zero-valued roles are dropped, matching `skip_counts`.
    """
    if not histogram:
        return ""
    buckets = ", ".join(f"{role}×{histogram[role]}" for role in ROLE_ORDER if histogram[role])
    return f"roles: {buckets}" if buckets else ""


def _format_attachment_detail(histogram: Counter[int]) -> str:
    """The `seed attachments: 1×N, 2×N, 3+×N` clause, or `""` when nothing was counted.

    Labelled *seed* attachments because the same line already says "carded from N
    attachments" about credit attachment events, which are a different thing: these are the
    distinct seed people that reached a candidate, the quantity the corroboration threshold
    is set against.

    An empty histogram is dropped rather than rendered as an empty group, matching how
    `skip_counts` drops zero-valued reasons: a sweep that reached no candidates has nothing
    to say about their distribution.
    """
    if not histogram:
        return ""
    grouped: Counter[int] = Counter()
    for attachments, films in histogram.items():
        grouped[min(attachments, _HISTOGRAM_TAIL)] += films
    buckets = ", ".join(
        f"{attachments}{'+' if attachments == _HISTOGRAM_TAIL else ''}×{films}"
        for attachments, films in sorted(grouped.items())
    )
    return f"seed attachments: {buckets}"


def sweep_detail(
    enumerated: EnumerateResult,
    refreshed: RefreshResult,
    carded: FieldEventResult,
    attached: CreditEventResult,
    detached: CreditDetachmentResult,
    released: ReleaseEventResult,
) -> str:
    """One line reporting all phases distinctly, for `finalize_run(detail=...)`."""
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
        f"{attached.skipped} already carded, {attached.held} held, "
        f"{attached.failures} failed",
        f"holds: {attached.holds_new} new, {attached.holds_cleared} cleared, "
        f"{attached.holds_expired} expired",
        f"credit removals: {detached.events_created} carded from "
        f"{detached.detachments_read} detachments, "
        f"{detached.skipped} already carded, {detached.failures} failed",
        f"release dates: {released.events_created} carded from "
        f"{released.changes_read} changes, "
        f"{released.skipped} already carded, {released.failures} failed",
    ]
    # Both beside the enumerate clause they belong to, ahead of the phases that follow it,
    # and inserted in reverse so they read `roles`, then `seed attachments`.
    attachments = _format_attachment_detail(enumerated.attachment_histogram)
    if attachments:
        parts.insert(1, attachments)
    roles = _format_role_detail(enumerated.role_histogram)
    if roles:
        parts.insert(1, roles)
    if enumerated.aborted:
        parts.append(f"enumerate aborted: {enumerated.abort_error}")
    if refreshed.aborted:
        parts.append(f"refresh aborted: {refreshed.abort_error}")
    if carded.aborted:
        parts.append(f"events aborted: {carded.abort_error}")
    if attached.aborted:
        parts.append(f"credits aborted: {attached.abort_error}")
    if detached.aborted:
        parts.append(f"credit removals aborted: {detached.abort_error}")
    if released.aborted:
        parts.append(f"release dates aborted: {released.abort_error}")
    return "; ".join(parts)
