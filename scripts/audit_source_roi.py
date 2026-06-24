"""Per-source story-ingestion ROI audit. Read-only diagnostic — records nothing to the DB.

Measures each ingestion source's contribution vs. its estimated link-stage token cost.
Run in the container:

    task shell
    python scripts/audit_source_roi.py                    # all history
    python scripts/audit_source_roi.py --window-days 30   # last 30 days of runs
    python scripts/audit_source_roi.py --run-id <uuid>    # one specific run

Paste the printed markdown block into the relevant spec or Linear ticket.
"""

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.db import SessionLocal
from upmovies.ingest.models import IngestRun, RunLLMUsage
from upmovies.news.feeds import is_google_source
from upmovies.news.models import Event, EventStory, Story

log = logging.getLogger("audit_source_roi")

# ---------------------------------------------------------------------------
# Source taxonomy
# ---------------------------------------------------------------------------

TRUSTED_TRADES: frozenset[str] = frozenset(
    {"Variety", "The Hollywood Reporter", "Deadline"}
)

OTHER_TRADE_RSS: frozenset[str] = frozenset(
    {"Collider", "/Film", "Empire", "ScreenRant"}
)

BROAD_GOOGLE: frozenset[str] = frozenset(
    {
        "Google News: casting",
        "Google News: release date",
        "Google News: trailer",
        "Google News: greenlight",
    }
)

PER_FILM_GOOGLE: frozenset[str] = frozenset({"Google News: per-film"})

ALL_SOURCES: list[str] = sorted(
    TRUSTED_TRADES | OTHER_TRADE_RSS | BROAD_GOOGLE | PER_FILM_GOOGLE
)

BUCKET_ORDER: list[tuple[str, frozenset[str]]] = [
    ("Trusted trades", TRUSTED_TRADES),
    ("Other trade RSS", OTHER_TRADE_RSS),
    ("Broad Google News", BROAD_GOOGLE),
    ("Per-film Google News", PER_FILM_GOOGLE),
]


def _bucket_of(source: str) -> str:
    for label, members in BUCKET_ORDER:
        if source in members:
            return label
    return "Unknown"


# ---------------------------------------------------------------------------
# Outlet overlap: trade domains we pull directly
# ---------------------------------------------------------------------------

# Resolved outlet domain strings that correspond to sources we pull via trade RSS.
# story.outlet contains the hostname (e.g. "deadline.com", "variety.com").
DIRECT_PULL_DOMAINS: frozenset[str] = frozenset(
    {
        "deadline.com",
        "variety.com",
        "hollywoodreporter.com",
        "www.hollywoodreporter.com",
        "collider.com",
        "www.collider.com",
        "slashfilm.com",
        "www.slashfilm.com",
        "empireonline.com",
        "www.empireonline.com",
        "screenrant.com",
        "www.screenrant.com",
    }
)


def _is_direct_pull_outlet(outlet: str | None) -> bool:
    if outlet is None:
        return False
    # outlet may be a full domain or domain with path; match on hostname portion
    hostname = outlet.split("/")[0].lower().removeprefix("www.")
    return outlet.lower() in DIRECT_PULL_DOMAINS or f"www.{hostname}" in DIRECT_PULL_DOMAINS or hostname in DIRECT_PULL_DOMAINS  # noqa: E501


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class SourceStats:
    source: str
    volume: int = 0
    linked: int = 0
    events: int = 0
    unique_events: int = 0
    outlet_overlap_count: int = 0  # Google only
    outlet_total: int = 0  # Google only — stories with non-NULL outlet

    @property
    def link_rate(self) -> float:
        return self.linked / self.volume if self.volume else 0.0

    @property
    def event_rate(self) -> float:
        return self.events / self.volume if self.volume else 0.0

    @property
    def outlet_overlap_pct(self) -> float | None:
        """None for trade RSS sources (outlet is always NULL there)."""
        if not is_google_source(self.source):
            return None
        if self.outlet_total == 0:
            return 0.0
        return self.outlet_overlap_count / self.outlet_total


@dataclass
class AuditResult:
    stats: dict[str, SourceStats] = field(default_factory=dict)
    total_volume: int = 0
    total_link_cost_usd: Decimal = Decimal("0")
    date_window_start: datetime | None = None
    date_window_end: datetime | None = None
    run_id_filter: UUID | None = None

    def source_link_cost(self, source: str) -> Decimal:
        if self.total_volume == 0 or self.total_link_cost_usd == 0:
            return Decimal("0")
        s = self.stats.get(source)
        if s is None or s.volume == 0:
            return Decimal("0")
        return Decimal(str(s.volume)) / Decimal(str(self.total_volume)) * self.total_link_cost_usd

    def cost_per_unique_event(self, source: str) -> str:
        cost = self.source_link_cost(source)
        s = self.stats.get(source)
        if s is None or s.unique_events == 0:
            return "∞"
        return f"${cost / Decimal(str(s.unique_events)):.4f}"


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


async def _load_story_stats(
    session: AsyncSession,
    *,
    fetched_after: datetime | None,
    fetched_before: datetime | None,
) -> dict[str, SourceStats]:
    """Per-source: volume, linked count, outlet overlap."""
    stmt = select(
        Story.source,
        func.count().label("volume"),
        func.count(Story.id).filter(Story.link_status == "linked").label("linked"),
    )
    if fetched_after is not None:
        stmt = stmt.where(Story.fetched_at >= fetched_after)
    if fetched_before is not None:
        stmt = stmt.where(Story.fetched_at < fetched_before)
    stmt = stmt.group_by(Story.source)

    rows = (await session.execute(stmt)).all()

    out: dict[str, SourceStats] = {}
    for row in rows:
        src = row.source
        out[src] = SourceStats(source=src, volume=row.volume, linked=row.linked)

    # Outlet overlap: per Google source, count stories whose outlet is a direct-pull domain.
    # We pull outlet values per source, then filter in Python (set is small).
    for src, st in out.items():
        if not is_google_source(src):
            continue
        outlet_stmt = select(Story.outlet).where(
            Story.source == src, Story.outlet.is_not(None)
        )
        if fetched_after is not None:
            outlet_stmt = outlet_stmt.where(Story.fetched_at >= fetched_after)
        if fetched_before is not None:
            outlet_stmt = outlet_stmt.where(Story.fetched_at < fetched_before)
        outlets = (await session.execute(outlet_stmt)).scalars().all()
        st.outlet_total = len(outlets)
        st.outlet_overlap_count = sum(1 for o in outlets if _is_direct_pull_outlet(o))

    return out


async def _load_event_stats(
    session: AsyncSession,
    stats: dict[str, SourceStats],
    *,
    fetched_after: datetime | None,
    fetched_before: datetime | None,
) -> None:
    """Populate event_rate and unique_events on existing SourceStats (mutates in place)."""
    # events per source = distinct events contributed by stories of that source
    event_stmt = (
        select(Story.source, func.count(EventStory.event_id.distinct()).label("events"))
        .join(EventStory, EventStory.story_id == Story.id)
        .join(Event, Event.id == EventStory.event_id)
        .group_by(Story.source)
    )
    if fetched_after is not None:
        event_stmt = event_stmt.where(Story.fetched_at >= fetched_after)
    if fetched_before is not None:
        event_stmt = event_stmt.where(Story.fetched_at < fetched_before)

    for row in (await session.execute(event_stmt)).all():
        if row.source in stats:
            stats[row.source].events = row.events

    # Unique events: events whose story-set is disjoint from the trusted-trade set.
    # Strategy: for each event, collect the distinct sources of its member stories.
    # An event is "unique to non-trusted" if NONE of its stories come from TRUSTED_TRADES.
    # Attribute it to every non-trusted source that contributed.
    #
    # We do this by pulling (event_id, source) pairs and grouping in Python.
    pair_stmt = (
        select(EventStory.event_id, Story.source)
        .join(Story, Story.id == EventStory.story_id)
        .distinct()
    )
    if fetched_after is not None:
        pair_stmt = pair_stmt.where(Story.fetched_at >= fetched_after)
    if fetched_before is not None:
        pair_stmt = pair_stmt.where(Story.fetched_at < fetched_before)

    event_sources: dict[UUID, set[str]] = {}
    for eid, src in (await session.execute(pair_stmt)).all():
        event_sources.setdefault(eid, set()).add(src)

    source_unique_count: dict[str, int] = {}
    for eid, srcs in event_sources.items():
        if srcs.isdisjoint(TRUSTED_TRADES):
            for src in srcs:
                source_unique_count[src] = source_unique_count.get(src, 0) + 1

    for src, cnt in source_unique_count.items():
        if src in stats:
            stats[src].unique_events = cnt


async def _load_link_cost(
    session: AsyncSession,
    *,
    run_id: UUID | None,
    started_after: datetime | None,
    started_before: datetime | None,
) -> Decimal:
    """Sum cost_usd for stage='link' rows within the requested scope."""
    stmt = select(func.coalesce(func.sum(RunLLMUsage.cost_usd), text("0"))).where(
        RunLLMUsage.stage == "link"
    )
    if run_id is not None:
        stmt = stmt.where(RunLLMUsage.run_id == run_id)
    elif started_after is not None or started_before is not None:
        stmt = stmt.join(IngestRun, IngestRun.id == RunLLMUsage.run_id)
        if started_after is not None:
            stmt = stmt.where(IngestRun.started_at >= started_after)
        if started_before is not None:
            stmt = stmt.where(IngestRun.started_at < started_before)

    result = (await session.execute(stmt)).scalar()
    return Decimal(str(result)) if result is not None else Decimal("0")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

COL_SOURCE = 32
COL_VOL = 8
COL_LR = 9
COL_ER = 9
COL_UE = 13
COL_OO = 15
COL_COST = 13
COL_CPU = 19


def _fmt_pct(v: float | None) -> str:
    if v is None:
        return "N/A"
    return f"{v:.1%}"


def _fmt_cost(v: Decimal) -> str:
    if v == 0:
        return "$0.000000"
    return f"${v:.6f}"


def _row_human(src: str, st: SourceStats, result: AuditResult) -> str:
    cost = result.source_link_cost(src)
    cpu = result.cost_per_unique_event(src)
    return (
        f"{src:<{COL_SOURCE}}"
        f"{st.volume:>{COL_VOL}}"
        f"{_fmt_pct(st.link_rate):>{COL_LR}}"
        f"{_fmt_pct(st.event_rate):>{COL_ER}}"
        f"{st.unique_events:>{COL_UE}}"
        f"{_fmt_pct(st.outlet_overlap_pct):>{COL_OO}}"
        f"{_fmt_cost(cost):>{COL_COST}}"
        f"{cpu:>{COL_CPU}}"
    )


def _row_md(src: str, st: SourceStats, result: AuditResult) -> str:
    cost = result.source_link_cost(src)
    cpu = result.cost_per_unique_event(src)
    return (
        f"| {src} | {st.volume} | {_fmt_pct(st.link_rate)} | {_fmt_pct(st.event_rate)} |"
        f" {st.unique_events} | {_fmt_pct(st.outlet_overlap_pct)} |"
        f" {_fmt_cost(cost)} | {cpu} |"
    )


def _bucket_subtotal_human(label: str, members: frozenset[str], result: AuditResult) -> str:
    vol = sum(result.stats.get(s, SourceStats(s)).volume for s in members)
    lnk = sum(result.stats.get(s, SourceStats(s)).linked for s in members)
    evts = sum(result.stats.get(s, SourceStats(s)).events for s in members)
    ue = sum(result.stats.get(s, SourceStats(s)).unique_events for s in members)
    cost = sum((result.source_link_cost(s) for s in members), Decimal("0"))
    lr = lnk / vol if vol else 0.0
    er = evts / vol if vol else 0.0
    cpu = f"${cost / Decimal(str(ue)):.4f}" if ue else "∞"
    src_label = f"  SUBTOTAL: {label}"
    return (
        f"{src_label:<{COL_SOURCE}}"
        f"{vol:>{COL_VOL}}"
        f"{_fmt_pct(lr):>{COL_LR}}"
        f"{_fmt_pct(er):>{COL_ER}}"
        f"{ue:>{COL_UE}}"
        f"{'—':>{COL_OO}}"
        f"{_fmt_cost(cost):>{COL_COST}}"
        f"{cpu:>{COL_CPU}}"
    )


def _bucket_subtotal_md(label: str, members: frozenset[str], result: AuditResult) -> str:
    vol = sum(result.stats.get(s, SourceStats(s)).volume for s in members)
    lnk = sum(result.stats.get(s, SourceStats(s)).linked for s in members)
    evts = sum(result.stats.get(s, SourceStats(s)).events for s in members)
    ue = sum(result.stats.get(s, SourceStats(s)).unique_events for s in members)
    cost = sum((result.source_link_cost(s) for s in members), Decimal("0"))
    lr = lnk / vol if vol else 0.0
    er = evts / vol if vol else 0.0
    cpu = f"${cost / Decimal(str(ue)):.4f}" if ue else "∞"
    return (
        f"| **{label} subtotal** | {vol} | {_fmt_pct(lr)} | {_fmt_pct(er)} |"
        f" {ue} | — | {_fmt_cost(cost)} | {cpu} |"
    )


def _header_human() -> str:
    return (
        f"{'source':<{COL_SOURCE}}"
        f"{'volume':>{COL_VOL}}"
        f"{'link_rate':>{COL_LR}}"
        f"{'event_rate':>{COL_ER}}"
        f"{'unique_events':>{COL_UE}}"
        f"{'outlet_overlap':>{COL_OO}}"
        f"{'est_link_cost':>{COL_COST}}"
        f"{'cost_per_uniq_evt':>{COL_CPU}}"
    )


def _header_md() -> str:
    return (
        "| source | volume | link_rate | event_rate | unique_events |"
        " outlet_overlap | est_link_cost | cost_per_unique_event |\n"
        "|---|---|---|---|---|---|---|---|"
    )


def _provenance(result: AuditResult) -> str:
    parts = [f"total stories in scope: {result.total_volume}"]
    if result.run_id_filter:
        parts.append(f"run_id={result.run_id_filter}")
    elif result.date_window_start or result.date_window_end:
        ws = result.date_window_start.strftime("%Y-%m-%d") if result.date_window_start else "all"
        we = result.date_window_end.strftime("%Y-%m-%d") if result.date_window_end else "now"
        parts.append(f"window={ws}..{we}")
    else:
        parts.append("window=all history")
    parts.append(f"total_link_stage_cost=${result.total_link_cost_usd:.6f}")
    return " | ".join(parts)


def format_report(result: AuditResult) -> str:
    sep = "-" * (COL_SOURCE + COL_VOL + COL_LR + COL_ER + COL_UE + COL_OO + COL_COST + COL_CPU)

    human_lines: list[str] = [
        "## Source ROI audit",
        "",
        _provenance(result),
        "",
        _header_human(),
        sep,
    ]

    md_lines: list[str] = [
        "## Source ROI audit",
        "",
        _provenance(result),
        "",
        _header_md(),
    ]

    for bucket_label, members in BUCKET_ORDER:
        # collect sources in this bucket that appear in stats (plus known missing ones)
        bucket_sources = sorted(
            members, key=lambda s: result.stats.get(s, SourceStats(s)).volume, reverse=True
        )
        for src in bucket_sources:
            st = result.stats.get(src, SourceStats(src))
            human_lines.append(_row_human(src, st, result))
            md_lines.append(_row_md(src, st, result))
        human_lines.append(_bucket_subtotal_human(bucket_label, members, result))
        md_lines.append(_bucket_subtotal_md(bucket_label, members, result))
        human_lines.append(sep)

    # Any unknown sources not in our taxonomy
    unknown = sorted(
        k for k in result.stats if _bucket_of(k) == "Unknown"
    )
    if unknown:
        human_lines.append("--- unlisted sources ---")
        md_lines.append("| *unlisted sources* | | | | | | | |")
        for src in unknown:
            st = result.stats[src]
            human_lines.append(_row_human(src, st, result))
            md_lines.append(_row_md(src, st, result))
        human_lines.append(sep)

    human_block = "\n".join(human_lines)
    md_block = "\n".join(md_lines)
    return f"{human_block}\n\n```markdown\n{md_block}\n```"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    run_id: UUID | None = None
    fetched_after: datetime | None = None
    fetched_before: datetime | None = None
    started_after: datetime | None = None
    started_before: datetime | None = None
    date_window_start: datetime | None = None
    date_window_end: datetime | None = None

    if args.run_id:
        run_id = UUID(args.run_id)
        log.info("scoping to run_id=%s", run_id)

    if args.window_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=args.window_days)
        fetched_after = cutoff
        fetched_before = datetime.now(UTC)
        started_after = cutoff
        started_before = datetime.now(UTC)
        date_window_start = cutoff
        date_window_end = fetched_before
        log.info("window: last %d days (since %s)", args.window_days, cutoff.strftime("%Y-%m-%d"))

    async with SessionLocal() as session:
        stats = await _load_story_stats(
            session,
            fetched_after=fetched_after,
            fetched_before=fetched_before,
        )

        await _load_event_stats(
            session,
            stats,
            fetched_after=fetched_after,
            fetched_before=fetched_before,
        )

        total_link_cost = await _load_link_cost(
            session,
            run_id=run_id,
            started_after=started_after,
            started_before=started_before,
        )

    total_volume = sum(s.volume for s in stats.values())

    result = AuditResult(
        stats=stats,
        total_volume=total_volume,
        total_link_cost_usd=total_link_cost,
        date_window_start=date_window_start,
        date_window_end=date_window_end,
        run_id_filter=run_id,
    )

    print(format_report(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Per-source ingestion ROI audit.")
    parser.add_argument("--window-days", type=int, default=None, help="look-back window in days")
    parser.add_argument("--run-id", type=str, default=None, help="scope to a single ingest run")
    asyncio.run(_amain(parser.parse_args()))
