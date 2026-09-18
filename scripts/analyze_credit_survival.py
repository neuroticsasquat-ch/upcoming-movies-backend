"""Survival-time-to-deletion over `catalog.film_credit_change`, and the quarantine window
it recommends (NEU-1372, D-4).

Spec: `docs/specs/bl-consumer-pivot-project-spec.md` D-3/D-4;
report: `docs/specs/bl-consumer-pivot-m05-credit-survival.md`.
Precedent: `docs/specs/NEU-1205-dampen-credit-oscillation.md` (evidence section).

`SWEEP_CREDIT_QUARANTINE_HOURS` holds a seed-grade attachment before it cards, so an edit
that was never true publishes nothing (D-3). Its default of 72h was a guess. This reads the
curve the guess should have come from: pair every `added` with the `removed` that later
retracts it, and ask by what age a revert has almost certainly already happened.

It **writes nothing** to the database — one read out of `catalog` and a CSV — so it is
safely re-runnable and carries no `ingest_run` row. Run it in the container against a
`task db:refresh`ed local DB:

    task shell -- api
    python scripts/analyze_credit_survival.py

**Right-censoring is the whole methodological point**, and it cuts one way only. An
attachment that has already been reverted is informative at every horizon — at 24h a revert
seen at 30h is a known non-event, at 48h it is a known event — so it always counts. Only a
*still-attached* add can be censored, and only when the log ended before it reached the
horizon. Every rate below is scored over the adds whose outcome at that horizon is known,
with that N printed beside it so a shrinking cohort is visible rather than silent. See
`_revert_rate`.

**Two further artefacts of the schedule shape every number here**, both documented at their
call sites: the log's time resolution is the daily `tmdb` pass (`_load_episodes`), and the
gate is only ever evaluated at a sweep, so a window rounds up to the next one
(`_release_at`).
"""

import argparse
import asyncio
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import text

from upmovies.catalog.seed_grade import credit_role
from upmovies.db import SessionLocal

DEFAULT_SINCE = "2026-08-01"
DEFAULT_CSV = "docs/specs/bl-consumer-pivot-m05-credit-survival.csv"

# The horizons D-4 asks for, in hours. 72 is the current default.
HORIZONS = (24, 48, 72, 96, 168)

# Histogram buckets for the survival curve, in hours. Fine below a day because that is where
# the curve was expected to turn over, coarse after it because the tail is what it is.
BUCKETS = (1, 2, 4, 8, 12, 18, 24, 36, 48, 72, 96, 168)

# The hour the sweep runs, UTC. It is a flag rather than a query because the schedule is
# Coolify's, not the database's; `ingest.ingest_run` only evidences it (modal `started_at`
# hour over the last 60 days = 07, against 09 for the `tmdb` pass that writes the changes).
DEFAULT_SWEEP_HOUR = 7

# Billing splits D-4 names. `credit_order` is TMDB's 0-indexed `order`, so "top-1" is order 0.
BILLING_BANDS = (("top-1", 1), ("top-3", 3), ("top-5", 5))

CSV_COLUMNS = (
    "film_title",
    "person_name",
    "role",
    "credit_order",
    "added_at",
    "removed_at",
    "survival_hours",
    "outcome",
)


@dataclass(frozen=True)
class Episode:
    """One attachment and its fate: an `added` row, and the `removed` row that later
    retracted it, or None when the credit is still attached at `observation_end`."""

    film_id: UUID
    film_title: str
    person_id: int
    person_name: str
    role: str
    credit_order: int | None
    """TMDB billing position from `catalog.film_credit` *now* — see `_load_episodes`. None
    for crew (who carry no billing) and for any attachment no longer present."""
    added_at: datetime
    removed_at: datetime | None

    @property
    def reverted(self) -> bool:
        return self.removed_at is not None

    @property
    def survival_hours(self) -> float | None:
        if self.removed_at is None:
            return None
        return (self.removed_at - self.added_at).total_seconds() / 3600.0

    def observed_for(self, hours: int, observation_end: datetime) -> bool:
        """Did the log run for `hours` past this add? Only meaningful for one still
        attached: it is the test for whether its survival past `hours` was actually seen."""
        return (observation_end - self.added_at).total_seconds() / 3600.0 >= hours


# One row per change, with the names the report needs and the billing position the join can
# still supply. The LEFT JOIN is to *current* `film_credit`, which is delete-and-rebuilt on
# every ingest: it answers billing for an attachment that is still there and answers nothing
# for one that was reverted. That asymmetry is measured and reported, not papered over.
_CHANGES_SQL = """
    SELECT c.id,
           c.film_id,
           f.title            AS film_title,
           c.person_id,
           p.name             AS person_name,
           c.credit_type,
           c.job,
           c.change,
           c.changed_at,
           fc.credit_order
      FROM catalog.film_credit_change c
      JOIN catalog.film   f ON f.id = c.film_id
      JOIN catalog.person p ON p.id = c.person_id
      LEFT JOIN catalog.film_credit fc
             ON fc.film_id     = c.film_id
            AND fc.person_id   = c.person_id
            AND fc.credit_type = c.credit_type
            AND fc.job IS NOT DISTINCT FROM c.job
     WHERE c.changed_at >= :since
     ORDER BY c.film_id, c.person_id, c.credit_type, c.job, c.changed_at, c.id
"""


async def _load_episodes(since: datetime) -> tuple[list[Episode], datetime, int]:
    """Pair adds with their reverts. Returns the episodes, the observation end, and the
    count of `removed` rows with no tracked `added` before them.

    Those orphans are baseline credits — recorded before the change log existed, so no
    attachment was ever observed and no survival time exists. NEU-1205 found 227 of 268
    removals in that state; they are counted and excluded, never treated as instant reverts.
    """
    async with SessionLocal() as session:
        rows = (await session.execute(text(_CHANGES_SQL), {"since": since})).mappings().all()

    by_slot: dict[tuple[UUID, int, str, str | None], list[dict]] = defaultdict(list)
    for row in rows:
        by_slot[(row["film_id"], row["person_id"], row["credit_type"], row["job"])].append(
            dict(row)
        )

    episodes: list[Episode] = []
    orphan_removals = 0
    for slot_rows in by_slot.values():
        # Adds and removes alternate within one slot, so the stack only ever holds one entry;
        # it is a stack rather than a single slot so a double-add cannot silently drop a row.
        open_adds: list[dict] = []
        for row in slot_rows:
            if row["change"] == "added":
                open_adds.append(row)
            elif open_adds:
                episodes.append(_episode(open_adds.pop(), removed_at=row["changed_at"]))
            else:
                orphan_removals += 1
        episodes.extend(_episode(add, removed_at=None) for add in open_adds)

    observation_end = max((row["changed_at"] for row in rows), default=since)
    episodes.sort(key=lambda e: e.added_at)
    return episodes, observation_end, orphan_removals


def _episode(add: dict, removed_at: datetime | None) -> Episode:
    role = credit_role(add["credit_type"], add["job"])
    return Episode(
        film_id=add["film_id"],
        film_title=add["film_title"],
        person_id=add["person_id"],
        person_name=add["person_name"],
        # Every recorded change is seed grade by construction (`ingest.tmdb.credit_history`
        # writes nothing else), so a None here means the two definitions have drifted apart.
        role=role if role is not None else f"non-seed:{add['credit_type']}/{add['job']}",
        credit_order=add["credit_order"] if add["credit_type"] == "cast" else None,
        added_at=add["changed_at"],
        removed_at=removed_at,
    )


def _kaplan_meier(
    episodes: list[Episode], observation_end: datetime
) -> list[tuple[float, float, int]]:
    """The Kaplan–Meier survival curve, as (time, S(time), at-risk-before) steps.

    The naive alternatives are both wrong here, in opposite directions. Scoring every
    horizon over *all* adds treats an attachment recorded two hours before the log ends as
    a 168h survivor. Keeping reverts at every horizon while dropping their still-attached
    peers for short follow-up — which is what this function replaced — conditions the
    denominator on the outcome and so oversamples events: inside the last H hours of the
    window, only the reverts survive the filter. Kaplan–Meier does neither, by shrinking the
    risk set as attachments are censored out of it rather than filtering the population.
    """
    observations: list[tuple[float, int]] = []
    for episode in episodes:
        survival = episode.survival_hours
        if survival is not None:
            observations.append((survival, 1))
        else:
            follow_up = (observation_end - episode.added_at).total_seconds() / 3600.0
            observations.append((follow_up, 0))
    # An event and a censoring at the same instant: the event is counted first, so the
    # censored row is still in the risk set that the event is divided by.
    observations.sort(key=lambda o: (o[0], -o[1]))

    steps: list[tuple[float, float, int]] = []
    at_risk = len(observations)
    survival_prob = 1.0
    index = 0
    while index < len(observations):
        time = observations[index][0]
        events = censored = 0
        while index < len(observations) and observations[index][0] == time:
            if observations[index][1] == 1:
                events += 1
            else:
                censored += 1
            index += 1
        if events:
            survival_prob *= 1 - events / at_risk
            steps.append((time, survival_prob, at_risk))
        at_risk -= events + censored
    return steps


def _revert_rate(
    episodes: list[Episode], hours: int, steps: list[tuple[float, float, int]]
) -> tuple[int, float, int]:
    """(reverts observed by `hours`, KM revert probability by `hours`, risk set at `hours`).

    The count is descriptive — how much slop a window of this length would have suppressed
    in this window — and is what the recommendation is argued from. The KM figure is the
    generalisation to "an arbitrary attachment", and is the one that would transfer.
    """
    observed = sum(
        1 for e in episodes if e.survival_hours is not None and e.survival_hours <= hours
    )
    survival_prob, at_risk = 1.0, len(episodes)
    for time, probability, risk_set in steps:
        if time > hours:
            break
        survival_prob, at_risk = probability, risk_set
    return observed, 1.0 - survival_prob, at_risk


def _knee(episodes: list[Episode]) -> float | None:
    """The knee of the cumulative revert curve, by maximum vertical distance to the chord.

    The curve runs from the fastest revert to the slowest; the chord is the straight line
    between those two endpoints. The point furthest *above* that line is where the curve
    stops being steep — the Kneedle construction, reduced to the one case here (a concave,
    monotonically rising CDF), so it needs no library and no smoothing parameter.

    Returns None when fewer than three reverts exist, because a knee through two points is
    the chord itself.
    """
    times = sorted(e.survival_hours for e in episodes if e.survival_hours is not None)
    if len(times) < 3:
        return None
    first, last = times[0], times[-1]
    if last == first:
        return first
    best_hours, best_gap = times[0], -1.0
    for index, hours in enumerate(times):
        # (index / (n - 1)) rather than ((index + 1) / n), so the curve runs (0,0) → (1,1)
        # and both endpoints sit *on* the chord; otherwise the first point starts 1/n above
        # it and a curve with no knee reports its own left edge as one.
        cumulative = index / (len(times) - 1)
        chord = (hours - first) / (last - first)
        gap = cumulative - chord
        if gap > best_gap:
            best_gap, best_hours = gap, hours
    return best_hours


def _release_at(added_at: datetime, hours: int, sweep_hour: int) -> datetime:
    """When an attachment added at `added_at` would actually card under a window of `hours`.

    The gate is `changed_at + hours <= now`, but `now` is only ever *a sweep pass*, and the
    sweep runs once a day two hours **ahead** of the `tmdb` pass that stamps `changed_at`.
    So eligibility that falls at 09:00 waits for the next morning's 07:00 sweep: every
    window silently rounds up to the next pass, and a nominal 72h is a real hold of ~94h.
    Reading the survival curve against the nominal number therefore understates what the
    current default already suppresses, which is the trap this function exists to avoid.
    """
    eligible = added_at + timedelta(hours=hours)
    release = eligible.replace(hour=sweep_hour, minute=0, second=0, microsecond=0)
    return release if release >= eligible else release + timedelta(days=1)


def _print_effective(episodes: list[Episode], observation_end: datetime, sweep_hour: int) -> None:
    """What each nominal window is actually worth once the daily sweep cadence is applied.

    Per-episode rather than averaged: an attachment is suppressed exactly when its revert
    lands at or before its own release instant. `hold` is the median real delay the window
    imposes on the attachments that survive it — the price paid for the `caught` column.
    """
    total_reverts = sum(1 for e in episodes if e.reverted)
    print(f"\n=== Effective window, at a {sweep_hour:02d}:00 UTC daily sweep ===")
    print(f"  {'nominal':>8}  {'hold':>7}  {'held off':>9}  {'known':>6}  {'caught':>7}")
    for hours in HORIZONS:
        suppressed = known = 0
        holds: list[float] = []
        for episode in episodes:
            release = _release_at(episode.added_at, hours, sweep_hour)
            holds.append((release - episode.added_at).total_seconds() / 3600.0)
            if episode.removed_at is not None:
                known += 1
                suppressed += episode.removed_at <= release
            elif observation_end >= release:
                known += 1
        holds.sort()
        median = holds[len(holds) // 2] if holds else 0.0
        caught = suppressed / total_reverts if total_reverts else 0.0
        print(
            f"  {str(hours) + 'h':>8}  {median:>6.0f}h  {suppressed:>9}  {known:>6}  {caught:>6.0%}"
        )


def _histogram(episodes: list[Episode]) -> list[tuple[str, int]]:
    reverts = [e.survival_hours for e in episodes if e.survival_hours is not None]
    counts: list[tuple[str, int]] = []
    lower = 0.0
    for upper in BUCKETS:
        counts.append((f"{lower:g}–{upper}h", sum(1 for h in reverts if lower < h <= upper)))
        lower = float(upper)
    counts.append((f">{BUCKETS[-1]}h", sum(1 for h in reverts if h > BUCKETS[-1])))
    return counts


def _print_histogram(episodes: list[Episode]) -> None:
    counts = _histogram(episodes)
    widest = max((n for _, n in counts), default=0)
    print("\nSurvival time to deletion (reverted attachments only)")
    for label, n in counts:
        bar = "█" * round(40 * n / widest) if widest else ""
        print(f"  {label:>10}  {n:>4}  {bar}")


def _print_horizons(
    label: str, episodes: list[Episode], observation_end: datetime, floor: int = 1
) -> None:
    """One horizon table. `floor` suppresses rows whose risk set is too small to read.

    `reverts` counts what a window of that length would have suppressed here; `KM` is the
    censoring-corrected probability an arbitrary attachment is reverted by then; `caught`
    is the share of *this subset's* reverts suppressed, and `+` what the step up from the
    previous horizon bought — the marginal column a window choice turns on.
    """
    steps = _kaplan_meier(episodes, observation_end)
    total_reverts = sum(1 for e in episodes if e.reverted)
    print(f"\n{label}")
    print(f"  {'horizon':>8}  {'reverts':>7}  {'at risk':>7}  {'KM':>6}  {'caught':>7}  {'+':>6}")
    previous = 0
    for hours in HORIZONS:
        observed, km, at_risk = _revert_rate(episodes, hours, steps)
        if at_risk < floor:
            gap = f"{'—':>6}  {'—':>7}  {'—':>6}"
            print(f"  {str(hours) + 'h':>8}  {observed:>7}  {at_risk:>7}  {gap}")
            continue
        caught = observed / total_reverts if total_reverts else 0.0
        gained = (observed - previous) / total_reverts if total_reverts else 0.0
        previous = observed
        print(
            f"  {str(hours) + 'h':>8}  {observed:>7}  {at_risk:>7}  "
            f"{km:>5.1%}  {caught:>6.0%}  {gained:>5.0%}"
        )


def _print_by_role(episodes: list[Episode], observation_end: datetime) -> None:
    print("\n=== By department (D-4: cast vs director/writer) ===")
    for role in ("cast", "director", "writer"):
        subset = [e for e in episodes if e.role == role]
        if subset:
            _print_horizons(f"{role} (n={len(subset)} adds)", subset, observation_end)


def _print_by_billing(episodes: list[Episode], observation_end: datetime) -> None:
    """The billing split, with its own coverage printed first.

    `film_credit_change` records no `credit_order`, so billing has to come from current
    `film_credit` — which holds a row only for an attachment that is still attached. The
    reverted population is therefore mostly unjoinable, and the coverage line below is the
    number that decides whether anything under it can be read at all.
    """
    cast = [e for e in episodes if e.role == "cast"]
    known = [e for e in cast if e.credit_order is not None]
    reverted_cast = [e for e in cast if e.reverted]
    reverted_known = [e for e in reverted_cast if e.credit_order is not None]
    print("\n=== By billing order (cast only) ===")
    print(f"  billing known for {len(known)}/{len(cast)} cast adds", end="")
    if cast:
        print(f" ({len(known) / len(cast):.1%})", end="")
    print(f"; of the {len(reverted_cast)} reverted, {len(reverted_known)} are joinable", end="")
    if reverted_cast:
        print(f" ({len(reverted_known) / len(reverted_cast):.1%})", end="")
    print()
    print("  NOTE: billing comes from current `film_credit`, which a reverted credit has left.")
    print("  The reverted column below is a survivorship-biased sample — see the report.")
    for label, bound in BILLING_BANDS:
        subset = [e for e in known if e.credit_order is not None and e.credit_order < bound]
        if subset:
            _print_horizons(f"{label} billed (n={len(subset)} adds)", subset, observation_end)


def _print_flappiest(episodes: list[Episode], limit: int) -> None:
    """D-4's "defacement-prone titles" question, and the signal underneath it.

    The top-N table alone understates the case, because it says nothing about whether a
    film's first revert *predicts* its next. The concentration block does: it replays the
    adds in time order and asks, of each revert, whether that film — and separately that
    exact `(film, person)` slot — had already produced one. A film-level hit rate well
    above the slot-level one means the churn is a property of the title, which is a signal
    available at carding time from this table alone, with no schema change (D-8).
    """
    reverts = Counter(e.film_title for e in episodes if e.reverted)
    adds = Counter(e.film_title for e in episodes)
    total = sum(reverts.values())
    print(f"\n=== Most-flapped titles (top {limit} by reverted attachments) ===")
    print(f"  {'reverts':>7}  {'adds':>5}  {'rate':>6}  title")
    for title, n in reverts.most_common(limit):
        print(f"  {n:>7}  {adds[title]:>5}  {n / adds[title]:>5.0%}  {title}")

    top_five = sum(n for _, n in reverts.most_common(5))
    print(
        f"\n  {len(reverts)} distinct films carry all {total} reverts; "
        f"the top five carry {top_five}",
        end="",
    )
    print(f" ({top_five / total:.0%})" if total else "")

    seen_film: Counter[str] = Counter()
    seen_slot: Counter[tuple[str, int]] = Counter()
    repeat_film = repeat_slot = 0
    for episode in sorted(episodes, key=lambda e: e.added_at):
        if not episode.reverted:
            continue
        repeat_film += bool(seen_film[episode.film_title])
        repeat_slot += bool(seen_slot[(episode.film_title, episode.person_id)])
        seen_film[episode.film_title] += 1
        seen_slot[(episode.film_title, episode.person_id)] += 1
    if total:
        print(
            f"  on a film that had already reverted: {repeat_film}/{total} "
            f"({repeat_film / total:.0%})"
        )
        print(
            f"  on a (film, person) slot that had:    {repeat_slot}/{total} "
            f"({repeat_slot / total:.0%})"
        )


def _print_flap_timeline(episodes: list[Episode], title: str) -> None:
    """The add/remove day-by-day trace for one title, so the report can show that a
    top flapper oscillates across the window rather than being one bad edit — the
    distinction that decides whether a longer window would have helped it."""
    subset = [e for e in episodes if e.film_title == title]
    if not subset:
        return
    added = Counter(e.added_at.date() for e in subset)
    removed = Counter(e.removed_at.date() for e in subset if e.removed_at is not None)
    print(f"\n=== Flap timeline — {title} ===")
    print(f"  adds    {', '.join(f'{d} ×{n}' for d, n in sorted(added.items()))}")
    print(f"  removes {', '.join(f'{d} ×{n}' for d, n in sorted(removed.items()))}")


def _write_csv(episodes: list[Episode], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for episode in episodes:
            hours = episode.survival_hours
            writer.writerow(
                {
                    "film_title": episode.film_title,
                    "person_name": episode.person_name,
                    "role": episode.role,
                    "credit_order": "" if episode.credit_order is None else episode.credit_order,
                    "added_at": episode.added_at.isoformat(),
                    "removed_at": ""
                    if episode.removed_at is None
                    else episode.removed_at.isoformat(),
                    "survival_hours": "" if hours is None else f"{hours:.6f}",
                    "outcome": "reverted" if episode.reverted else "attached",
                }
            )


async def _amain(args: argparse.Namespace) -> None:
    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    episodes, observation_end, orphan_removals = await _load_episodes(since)
    if not episodes:
        print(f"No credit changes recorded since {since.date()} — nothing to analyse.")
        return

    reverted = [e for e in episodes if e.reverted]
    span_days = (observation_end - episodes[0].added_at).total_seconds() / 86400.0
    print("=== catalog.film_credit_change — attachment survival ===")
    print(
        f"  window           {episodes[0].added_at:%Y-%m-%d} → {observation_end:%Y-%m-%d} "
        f"({span_days:.1f} days)"
    )
    print(f"  adds tracked     {len(episodes)}")
    print(f"  reverted         {len(reverted)} ({len(reverted) / len(episodes):.1%} of adds)")
    print(f"  still attached   {len(episodes) - len(reverted)}")
    print(f"  orphan removals  {orphan_removals}  (baseline credits, no tracked add — excluded)")

    _print_histogram(episodes)

    knee = _knee(episodes)
    if knee is not None:
        within = sum(
            1 for e in reverted if e.survival_hours is not None and e.survival_hours <= knee
        )
        longest = max(e.survival_hours or 0.0 for e in reverted)
        print(
            f"\n  knee (max distance to chord): {knee:.1f}h "
            f"— {within}/{len(reverted)} ({within / len(reverted):.1%}) of reverts by then; "
            f"longest observed revert {longest:.0f}h"
        )
    else:
        print("\n  knee: not computable (fewer than three reverts)")

    _print_horizons("=== All seed-grade adds (nominal window) ===", episodes, observation_end)
    _print_effective(episodes, observation_end, args.sweep_hour)
    _print_by_role(episodes, observation_end)
    _print_by_billing(episodes, observation_end)
    _print_flappiest(episodes, args.top)
    worst = Counter(e.film_title for e in episodes if e.reverted).most_common(1)
    if worst:
        _print_flap_timeline(episodes, worst[0][0])

    csv_path = Path(args.csv)
    _write_csv(episodes, csv_path)
    print(f"\nWrote {len(episodes)} rows to {csv_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Survival-time-to-deletion for seed-grade credit attachments (NEU-1372)."
    )
    parser.add_argument(
        "--since", default=DEFAULT_SINCE, help=f"ISO date to analyse from (default {DEFAULT_SINCE})"
    )
    parser.add_argument(
        "--csv", default=DEFAULT_CSV, help=f"CSV output path (default {DEFAULT_CSV})"
    )
    parser.add_argument("--top", type=int, default=15, help="how many flapped titles to list")
    parser.add_argument(
        "--sweep-hour",
        type=int,
        default=DEFAULT_SWEEP_HOUR,
        help=f"UTC hour of the daily sweep pass (default {DEFAULT_SWEEP_HOUR})",
    )
    asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    main()
