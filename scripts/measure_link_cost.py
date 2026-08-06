"""Measure the real cost of the Stage-1 link path (sequential + prompt caching) on a
locally-ingested corpus, using real Anthropic API calls. Records nothing to the DB.

Originally (NEU-297) this compared sequential against the Anthropic Message Batches path;
that path was removed in ADR-0005, so only the sequential shape remains to measure.

Precondition: run a real tmdb + feeds ingest locally first so the DB holds a representative
`pending` corpus. Then, in the container:

    task shell
    python scripts/measure_link_cost.py --repeats 3            # full corpus
    python scripts/measure_link_cost.py --repeats 3 --limit 300  # spend-capped smoke run
"""

import argparse
import asyncio
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import _MAX_TOKENS, build_link_request
from upmovies.link.pipeline import _chunks
from upmovies.link.roster import Roster, build_roster
from upmovies.llm.client import AnthropicClient, Usage
from upmovies.llm.pricing import HAIKU_4_5, Rates, price
from upmovies.news.models import Story


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def format_report(seq_runs: list[Usage], rates: Rates) -> str:
    """A markdown block reporting mean tokens and $ per run, plus the
    cache_creation:cache_read split — the ratio ADR-0005's cache-thrash finding rests on."""
    seq_total = sum(seq_runs, Usage())
    seq_mean_cost = _mean([price(u, rates, batch=False) for u in seq_runs])
    seq_n = len(seq_runs) or 1

    def _fields(u: Usage) -> str:
        return (
            f"input_tokens={u.input_tokens} output_tokens={u.output_tokens} "
            f"cache_read_input_tokens={u.cache_read_input_tokens} "
            f"cache_creation_input_tokens={u.cache_creation_input_tokens}"
        )

    mean = Usage(
        input_tokens=seq_total.input_tokens // seq_n,
        output_tokens=seq_total.output_tokens // seq_n,
        cache_read_input_tokens=seq_total.cache_read_input_tokens // seq_n,
        cache_creation_input_tokens=seq_total.cache_creation_input_tokens // seq_n,
    )
    return "\n".join(
        [
            "## Link cost measurement",
            "",
            f"- runs: sequential={len(seq_runs)}",
            f"- sequential mean per run: {_fields(mean)}",
            f"- sequential mean $/run (full rates): ${seq_mean_cost:.4f}",
            "",
            "| path | mean input | mean output | mean cache_read | mean cache_creation "
            "| mean $/run |",
            "|---|---|---|---|---|---|",
            f"| sequential | {mean.input_tokens} | {mean.output_tokens} | "
            f"{mean.cache_read_input_tokens} | {mean.cache_creation_input_tokens} | "
            f"${seq_mean_cost:.4f} |",
        ]
    )


async def measure_sequential(
    client, roster: Roster, chunks: list[list[Story]], *, model: str
) -> Usage:
    """Drive the sequential Stage-1 shape: one `complete_with_usage` per chunk, reusing the
    production request builder. Cache warms naturally over calls 2..N (same `roster`)."""
    run_date = datetime.now(UTC).date()
    total = Usage()
    for chunk in chunks:
        system, messages = build_link_request(roster, chunk, run_date)
        _, usage = await client.complete_with_usage(
            model=model, system=system, messages=messages, max_tokens=_MAX_TOKENS
        )
        total += usage
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
    print(f"pre-flight: ~{n_chunks * args.repeats} sequential calls")

    seq_runs: list[Usage] = []
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for r in range(args.repeats):
            # Cold-cache isolation: a unique nonce per repeat → each run starts cold
            # (faithful to the daily cron) while caching still works intra-run.
            seq_roster = replace(roster, text=f"RUN {uuid4()}\n{roster.text}")
            t0 = time.monotonic()
            seq = await measure_sequential(client, seq_roster, chunks, model=model)
            seq_secs = time.monotonic() - t0
            seq_runs.append(seq)
            print(f"[repeat {r + 1}] sequential done in {seq_secs:.1f}s")

    print()
    print(format_report(seq_runs, HAIKU_4_5))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Measure sequential+caching link cost.")
    parser.add_argument("--repeats", type=int, default=3, help="runs (default 3)")
    parser.add_argument("--limit", type=int, default=None, help="cap stories (spend guard)")
    asyncio.run(_amain(parser.parse_args()))
