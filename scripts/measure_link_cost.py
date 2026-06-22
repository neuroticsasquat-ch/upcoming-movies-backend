"""Measure the real cost of the two Stage-1 link paths (sequential+caching vs batched) on
a locally-ingested corpus, using real Anthropic API calls. Records nothing to the DB.

Precondition: run a real tmdb + feeds ingest locally first so the DB holds a representative
`pending` corpus. Then, in the container:

    task shell
    python scripts/measure_link_cost.py --repeats 3            # full corpus
    python scripts/measure_link_cost.py --repeats 3 --limit 300  # spend-capped smoke run

Paste the printed markdown block into
`docs/specs/2026-06-20-ingestion-cadence-recency-architecture-design.md` §Batches.
"""

import argparse
import asyncio
import logging
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import _MAX_TOKENS, build_batch_request, build_link_request
from upmovies.link.pipeline import _chunks
from upmovies.link.roster import Roster, build_roster
from upmovies.llm.client import AnthropicClient, BatchResult, Usage
from upmovies.news.models import Story

log = logging.getLogger("measure_link_cost")

# Cache pricing multipliers (relative to base input): 5-min ephemeral write = 1.25x, read = 0.10x.
_CACHE_WRITE_MULT = 1.25
_CACHE_READ_MULT = 0.10
_BATCH_DISCOUNT = 0.50


@dataclass(frozen=True)
class Rates:
    """Per-million-token USD rates. VERIFY against https://www.anthropic.com/pricing before
    trusting the $ figures — raw token counts are the recorded source of truth."""

    input_per_mtok: float
    output_per_mtok: float


# Claude Haiku 4.5 (the link model). VERIFY current rates before trusting $ output.
HAIKU_4_5 = Rates(input_per_mtok=1.00, output_per_mtok=5.00)


def price(usage: Usage, rates: Rates, *, batch: bool) -> float:
    """Dollar cost of `usage` at `rates`. Cache writes cost 1.25x base input, cache reads
    0.10x; the batch path applies a flat 50% discount on the whole total."""
    base_in = rates.input_per_mtok / 1_000_000
    out = rates.output_per_mtok / 1_000_000
    cost = (
        usage.input_tokens * base_in
        + usage.cache_creation_input_tokens * base_in * _CACHE_WRITE_MULT
        + usage.cache_read_input_tokens * base_in * _CACHE_READ_MULT
        + usage.output_tokens * out
    )
    return cost * (_BATCH_DISCOUNT if batch else 1.0)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def format_report(seq_runs: list[Usage], batch_runs: list[Usage], rates: Rates) -> str:
    """A human table + a markdown block (ready to paste into the spec) comparing the two
    paths. Reports per-run and mean tokens and $, plus the cache_creation:cache_read split."""
    seq_total = sum(seq_runs, Usage())
    bat_total = sum(batch_runs, Usage())
    seq_mean_cost = _mean([price(u, rates, batch=False) for u in seq_runs])
    bat_mean_cost = _mean([price(u, rates, batch=True) for u in batch_runs])
    seq_n = len(seq_runs) or 1
    bat_n = len(batch_runs) or 1

    def _fields(u: Usage) -> str:
        return (
            f"input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
            f"cache_read_input_tokens={u.cache_read_input_tokens} "
            f"cache_creation_input_tokens={u.cache_creation_input_tokens}"
        )

    lines = [
        "## Link cost measurement",
        "",
        f"- runs per path: sequential={len(seq_runs)}, batched={len(batch_runs)}",
        f"- sequential mean per run: {_fields(Usage(input_tokens=seq_total.input_tokens // seq_n, output_tokens=seq_total.output_tokens // seq_n, cache_read_input_tokens=seq_total.cache_read_input_tokens // seq_n, cache_creation_input_tokens=seq_total.cache_creation_input_tokens // seq_n))}",
        f"- batched mean per run:    {_fields(Usage(input_tokens=bat_total.input_tokens // bat_n, output_tokens=bat_total.output_tokens // bat_n, cache_read_input_tokens=bat_total.cache_read_input_tokens // bat_n, cache_creation_input_tokens=bat_total.cache_creation_input_tokens // bat_n))}",
        f"- sequential mean $/run (full rates):  ${seq_mean_cost:.4f}",
        f"- batched mean $/run (50% batch disc.): ${bat_mean_cost:.4f}",
    ]
    if seq_mean_cost > 0:
        delta = (seq_mean_cost - bat_mean_cost) / seq_mean_cost * 100
        lines.append(f"- batched vs sequential: {delta:+.1f}% (positive = batched cheaper)")
    lines += [
        "",
        "| path | mean input | mean output | mean cache_read | mean cache_creation | mean $/run |",
        "|---|---|---|---|---|---|",
        f"| sequential | {seq_total.input_tokens // seq_n} | {seq_total.output_tokens // seq_n} | "
        f"{seq_total.cache_read_input_tokens // seq_n} | {seq_total.cache_creation_input_tokens // seq_n} | "
        f"${seq_mean_cost:.4f} |",
        f"| batched | {bat_total.input_tokens // bat_n} | {bat_total.output_tokens // bat_n} | "
        f"{bat_total.cache_read_input_tokens // bat_n} | {bat_total.cache_creation_input_tokens // bat_n} | "
        f"${bat_mean_cost:.4f} |",
    ]
    return "\n".join(lines)


async def measure_sequential(
    client, roster: Roster, chunks: list[list[Story]], *, model: str
) -> Usage:
    """Drive the sequential Stage-1 shape: one `complete_with_usage` per chunk, reusing the
    production request builder. Cache warms naturally over calls 2..N (same `roster`)."""
    total = Usage()
    for chunk in chunks:
        system, messages = build_link_request(roster, chunk)
        _, usage = await client.complete_with_usage(
            model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
        )
        total += usage
    return total


async def measure_batched(
    client, roster: Roster, chunks: list[list[Story]], *, model: str
) -> Usage:
    """Drive the batched Stage-1 shape: N `build_batch_request`s submitted as one batch.
    Sums usage over succeeded chunks; logs and skips any failed chunk."""
    requests = [
        build_batch_request(custom_id=str(i), model=model, roster=roster, stories=chunk)
        for i, chunk in enumerate(chunks)
    ]
    results = await client.complete_batch(requests)
    total = Usage()
    for i in range(len(chunks)):
        result = results.get(str(i))
        if result is None or not result.ok or result.usage is None:
            detail = result.error_type if result else "missing"
            log.warning("batched chunk %d unavailable (%s) — excluded from totals", i, detail)
            continue
        total += result.usage
    return total


async def select_corpus(session, recency_days: int, limit: int | None) -> list[UUID]:
    """Same WHERE clause as `run_link_ingest`: `pending` stories whose
    coalesce(published_at, fetched_at) is within `recency_days`. Adds `ORDER BY id` (not in
    production) for a stable, repeatable corpus; optional `limit` caps spend."""
    cutoff = datetime.now(UTC) - timedelta(days=recency_days)
    stmt = (
        select(Story.id)
        .where(
            Story.link_status == "pending",
            func.coalesce(Story.published_at, Story.fetched_at) >= cutoff,
        )
        .order_by(Story.id)
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = await session.execute(stmt)
    return [row[0] for row in rows.all()]


async def _load_chunk(session, ids: list[UUID]) -> list[Story]:
    return list(
        (await session.execute(select(Story).where(Story.id.in_(ids)).order_by(Story.id))).scalars().all()
    )


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    model = settings.link_model

    async with SessionLocal() as s:
        roster = await build_roster(s)
        ids = await select_corpus(s, settings.link_recency_days, args.limit)
        chunk_id_lists = _chunks(ids, settings.link_batch_size)
        chunks = [await _load_chunk(s, cids) for cids in chunk_id_lists]

    n_chunks = len(chunks)
    print(
        f"corpus: {len(ids)} stories, {n_chunks} chunks (batch_size={settings.link_batch_size}), "
        f"model={model}, repeats={args.repeats}"
    )
    if n_chunks == 0:
        print("no pending stories in window — run a tmdb+feeds ingest first. Aborting.")
        return
    print(
        f"pre-flight: ~{n_chunks * args.repeats} sequential calls + {args.repeats} batches "
        f"of {n_chunks} requests each"
    )

    seq_runs: list[Usage] = []
    batch_runs: list[Usage] = []
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for r in range(args.repeats):
            # Cold-cache isolation: a unique nonce per path per repeat → each run starts cold
            # (faithful to the daily cron) while caching still works intra-run. Separate
            # nonces for seq vs batch so one path never reads the other's warmed cache.
            seq_roster = replace(roster, text=f"RUN {uuid4()}\n{roster.text}")
            t0 = time.monotonic()
            seq = await measure_sequential(client, seq_roster, chunks, model=model)
            seq_secs = time.monotonic() - t0
            seq_runs.append(seq)
            print(f"[repeat {r + 1}] sequential done in {seq_secs:.1f}s")

            batch_roster = replace(roster, text=f"RUN {uuid4()}\n{roster.text}")
            t0 = time.monotonic()
            bat = await measure_batched(client, batch_roster, chunks, model=model)
            batch_secs = time.monotonic() - t0
            batch_runs.append(bat)
            print(
                f"[repeat {r + 1}] batched done in {batch_secs:.1f}s "
                f"(daily budget ~3600s)"
            )

    print()
    print(format_report(seq_runs, batch_runs, HAIKU_4_5))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure batch vs sequential+caching link cost.")
    parser.add_argument("--repeats", type=int, default=3, help="runs per path (default 3)")
    parser.add_argument("--limit", type=int, default=None, help="cap stories (spend guard)")
    asyncio.run(_amain(parser.parse_args()))
