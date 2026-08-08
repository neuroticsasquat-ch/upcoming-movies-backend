"""Sweep T, K and the link batch size against real traffic at real catalog scale (NEU-1001).

The design spec set T=0.34 and K=10 as **placeholders**, measured over a 249-film dev
catalog and a 301-item fixture, and said so explicitly: a probe that small cannot predict
collision behaviour at ~1,200 films. This script is what replaces them with measurements.

**It costs no LLM tokens.** Retrieval is pure, and every story in `news.story` already
carries the roster's verdict, so the shadow measurement M2 wires into the live pipeline can
be replayed offline over the same population, at the same catalog scale, for free. What the
live shadow adds is fresh traffic against a moving catalog; what this adds is every
threshold and cap at once over six weeks of it, which is what tuning needs and a single
run's probe rows cannot give.

Run it against a **prod-refreshed** database (`task db:refresh`) — the dev catalog is a
fraction of prod's, and catalog size is the one variable the whole exercise turns on:

    task shell
    python scripts/tune_retrieval.py --days 30 > /tmp/tuning.md
    python scripts/tune_retrieval.py --days 30 --count-tokens   # exact prompt sizes

**One pass, every combination.** Each story is scored once with the threshold floored at
`MIN_RECORDED_SCORE` and no cap; every (T, K) in the sweep is then derived from the recorded
score vector. Scoring 40k stories against 1,200 films is the expensive half, and doing it
once rather than once per grid cell is what makes a grid affordable.

**Reading the recall column.** It is measured against *roster picks*, the same denominator
shadow uses and with the same caveat (spec §5): the roster makes false positives, so
retrieval declining to surface one is a win that a bare percentage scores as a loss. Picks
whose film has since left the active catalog are excluded outright rather than counted as
misses — no threshold can reach a film the index does not contain.
"""

import argparse
import asyncio
import json
import logging
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import (
    _MAX_TOKENS,
    _RETRIEVAL_INSTRUCTIONS,
    StoryCandidates,
    build_retrieval_link_request,
    story_dek,
)
from upmovies.link.retrieval.index import CandidateIndex, build_candidate_index, build_index
from upmovies.link.retrieval.render import render_candidates
from upmovies.link.retrieval.select import select_candidates
from upmovies.news.models import Story

# Scores below this are recorded as "did not clear", not kept. Every threshold in the sweep
# sits above it, so nothing the grid asks about is lost — and it keeps the score vector of a
# story that brushes a hundred films short.
MIN_RECORDED_SCORE = 0.2

# Stand-in for "no cap" while collecting: `CandidateSet.limit` only narrows the view, so a
# limit past any real candidate count records the full scored list.
_UNCAPPED = 10**9

THRESHOLDS = (0.25, 0.34, 0.4, 0.5, 0.6, 0.67, 0.75, 1.0)
LIMITS = (3, 5, 10, 15, 20, 30, 50)
BATCH_SIZES = (10, 15, 20, 25, 30, 40)
# Catalog sizes for the growth curve. What degrades as the catalog grows is candidate-set
# size, not recall, and the shape of that curve is the only predictor available for the 5–20x
# the undated-film expansion brings — so it is measured rather than assumed (NEU-1001).
CATALOG_SIZES = (250, 500, 750, 1000)
# Stories scored per catalog size. The curve needs a shape, not another 98k-story figure.
SCALING_SAMPLE = 10_000
# Stories whose rendered candidate list is token-counted, for the per-candidate cost.
_CANDIDATE_COST_SAMPLE = 20


@dataclass(frozen=True)
class StoryScores:
    """One story's retrieval result, recorded once and re-read at every (T, K).

    `pick_index` is a position in the *uncapped* descending score list, which is what makes
    the derivation valid: every entry above the pick scores at least as much as the pick, so
    raising T to anything the pick still clears cannot move it. `pick_score` and `pick_index`
    then separate the two failure modes the tuning has to tell apart — a pick lost to the cap
    (raise K) from one that never cleared the threshold (lower T, or accept it as a lexical
    miss K cannot reach).

    **`has_pick` is its own field rather than `pick_index is not None`.** A pick scoring below
    `MIN_RECORDED_SCORE` has no index and no score, and it is *still a pick* — it belongs in
    the recall denominator as a miss. Inferring the denominator from the index would silently
    drop exactly the stories the measurement exists to count, and a zero sentinel in
    `pick_score` would be worse still: at a threshold of zero it would read as retrieved."""

    scores: tuple[float, ...]
    has_pick: bool = False
    pick_score: float | None = None
    pick_index: int | None = None


@dataclass(frozen=True)
class SweepRow:
    """What one (T, K) pair does to the corpus."""

    threshold: float
    limit: int
    stories: int
    zero_candidate: int
    saturated: int
    offered_total: int
    over_threshold: tuple[int, ...]
    picks: int
    retrieved: int
    lost_to_cap: int

    @property
    def zero_pct(self) -> float:
        return 100.0 * self.zero_candidate / self.stories if self.stories else 0.0

    @property
    def saturated_pct(self) -> float:
        return 100.0 * self.saturated / self.stories if self.stories else 0.0

    @property
    def mean_offered(self) -> float:
        return self.offered_total / self.stories if self.stories else 0.0

    @property
    def recall(self) -> float | None:
        """Share of roster picks retrieval would have offered, or None with no picks."""
        return self.retrieved / self.picks if self.picks else None

    @property
    def lost_to_threshold(self) -> int:
        """Picks that never cleared T — the misses raising K cannot recover."""
        return self.picks - self.retrieved - self.lost_to_cap


def percentile(values: Sequence[int], q: float) -> int:
    """The `q`-th percentile of `values` by nearest-rank. Empty input gives 0.

    Nearest-rank rather than an interpolating definition: these are candidate-set sizes, and
    a p90 of 4.5 films is not a thing a prompt can contain."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), math.ceil(q * len(ordered))))
    return ordered[rank - 1]


def sweep(rows: Sequence[StoryScores], *, threshold: float, limit: int) -> SweepRow:
    """Derive one grid cell from the recorded scores. Pure."""
    over_threshold: list[int] = []
    zero_candidate = saturated = offered_total = 0
    picks = retrieved = lost_to_cap = 0

    for row in rows:
        over = sum(1 for score in row.scores if score >= threshold)
        over_threshold.append(over)
        offered_total += min(over, limit)
        if over == 0:
            zero_candidate += 1
        if over > limit:
            saturated += 1
        if not row.has_pick:
            continue
        picks += 1
        # No score means the pick did not clear `MIN_RECORDED_SCORE`, which every threshold
        # in the grid sits above — a threshold miss at every T, including T=0.
        if row.pick_score is None or row.pick_score < threshold:
            continue
        if row.pick_index is not None and row.pick_index < limit:
            retrieved += 1
        else:
            lost_to_cap += 1

    return SweepRow(
        threshold=threshold,
        limit=limit,
        stories=len(rows),
        zero_candidate=zero_candidate,
        saturated=saturated,
        offered_total=offered_total,
        over_threshold=tuple(over_threshold),
        picks=picks,
        retrieved=retrieved,
        lost_to_cap=lost_to_cap,
    )


def score_story(index: CandidateIndex, story: Story) -> StoryScores:
    """Retrieve for one story and record what tuning needs to know about the result.

    Uses the production selector on the production fields, so what is swept is the path that
    ships rather than a re-implementation of it."""
    candidates = select_candidates(
        index,
        headline=story.title,
        dek=story_dek(story),
        threshold=MIN_RECORDED_SCORE,
        limit=_UNCAPPED,
    )
    scores = tuple(c.score for c in candidates.scored)

    # A pick whose film has left the active catalog is not a miss — it is unreachable for any
    # (T, K), so it is dropped from the denominator rather than scored against.
    has_pick = (
        story.link_status == "linked"
        and story.film_id is not None
        and index.film(story.film_id) is not None
    )
    if not has_pick:
        return StoryScores(scores=scores)
    for position, candidate in enumerate(candidates.scored):
        if candidate.film.film_id == story.film_id:
            return StoryScores(
                scores=scores, has_pick=True, pick_score=candidate.score, pick_index=position
            )
    # Below `MIN_RECORDED_SCORE`: a pick with no score and no rank, which every threshold in
    # the grid counts as a miss. It stays in the denominator — that is what `has_pick` is for.
    return StoryScores(scores=scores, has_pick=True)


async def load_scores(
    session: AsyncSession, index: CandidateIndex, *, days: int, sample: int | None
) -> list[StoryScores]:
    """Score every story in the window, streaming so 98k ORM rows never land at once.

    The score vectors are kept — they are the whole point — but they are a few floats per
    story against a full `Story` row carrying its raw feed entry."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    stmt = (
        select(Story)
        .where(func.coalesce(Story.published_at, Story.fetched_at) >= cutoff)
        .order_by(Story.id)
    )
    if sample is not None:
        stmt = stmt.limit(sample)

    rows: list[StoryScores] = []
    result = await session.stream_scalars(stmt.execution_options(yield_per=500))
    async for story in result:
        rows.append(score_story(index, story))
        if len(rows) % 5000 == 0:
            logging.info("scored %d stories", len(rows))
    return rows


def format_threshold_table(rows: Sequence[StoryScores], thresholds: Iterable[float]) -> str:
    """T against everything the cap does not touch — recall, zero-candidate rate, set size."""
    lines = [
        "| T | recall | picks | lost to T | zero-candidate | median | p90 | p99 | max | mean |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for threshold in thresholds:
        row = sweep(rows, threshold=threshold, limit=_UNCAPPED)
        recall = f"{row.recall:.3f}" if row.recall is not None else "—"
        sizes = row.over_threshold
        lines.append(
            f"| {threshold} | **{recall}** | {row.picks} | {row.lost_to_threshold} | "
            f"{row.zero_candidate} ({row.zero_pct:.1f}%) | {percentile(sizes, 0.5)} | "
            f"{percentile(sizes, 0.9)} | {percentile(sizes, 0.99)} | {max(sizes, default=0)} | "
            f"{row.mean_offered:.2f} |"
        )
    return "\n".join(lines)


def format_cap_table(rows: Sequence[StoryScores], threshold: float, limits: Iterable[int]) -> str:
    """K at a fixed T — what the cap costs in recall and what it saves in prompt size."""
    lines = [
        f"| K (at T={threshold}) | recall | lost to cap | saturated | mean offered |",
        "|---|---|---|---|---|",
    ]
    for limit in limits:
        row = sweep(rows, threshold=threshold, limit=limit)
        recall = f"{row.recall:.3f}" if row.recall is not None else "—"
        lines.append(
            f"| {limit} | **{recall}** | {row.lost_to_cap} | "
            f"{row.saturated} ({row.saturated_pct:.1f}%) | {row.mean_offered:.2f} |"
        )
    return "\n".join(lines)


def format_scaling_table(
    index: CandidateIndex,
    stories: Sequence[Story],
    *,
    threshold: float,
    limit: int,
    catalog_sizes: Iterable[int],
) -> str:
    """How the candidate-set distribution moves with catalog size.

    The ticket's forward-looking question, and the only one the tuning can answer about a
    catalog that does not exist yet: recall at a given T does not move as films are added,
    but **collisions do** — more films share a token with any given headline. Sub-catalogs
    are drawn deterministically by film id, so the sample is unbiased with respect to title
    length and the curve is reproducible."""
    ordered = sorted(index.films, key=lambda f: str(f.film_id))
    lines = [
        "| films | median | p90 | p99 | max | mean offered | zero-candidate | saturated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for size in (*catalog_sizes, len(ordered)):
        if size > len(ordered):
            continue
        sub = build_index(ordered[:size])
        rows = [score_story(sub, story) for story in stories]
        row = sweep(rows, threshold=threshold, limit=limit)
        sizes = row.over_threshold
        lines.append(
            f"| {size} | {percentile(sizes, 0.5)} | {percentile(sizes, 0.9)} | "
            f"{percentile(sizes, 0.99)} | {max(sizes, default=0)} | {row.mean_offered:.2f} | "
            f"{row.zero_pct:.1f}% | {row.saturated_pct:.1f}% |"
        )
    return "\n".join(lines)


def _estimate_tokens(text: str) -> int:
    """A characters/4 stand-in for a token count, for runs without `--count-tokens`."""
    return round(len(text) / 4)


def _request_text(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> str:
    return "".join(block["text"] for block in system) + "".join(str(m["content"]) for m in messages)


def _reply_for(batch: Sequence[StoryCandidates]) -> str:
    """A worst-case reply for `batch`: every story answered with a film and a category.

    The output side of the batch-size question. `_MAX_TOKENS` caps the reply at 2048, and a
    batch whose reply does not fit is not a cost problem but a truncation one — the parse
    fails and the whole batch is re-tried or lost."""
    return json.dumps(
        [
            {
                "id": str(entry.story.id),
                "film": len(entry.candidates.candidates),
                "confidence": 0.95,
                "reason": "not-news",
                "category": "interview-quote",
            }
            for entry in batch
        ]
    )


async def format_batch_table(
    index: CandidateIndex,
    stories: Sequence[Story],
    *,
    threshold: float,
    limit: int,
    batch_sizes: Iterable[int],
    counter: "AsyncAnthropic | None",
) -> str:
    """Prompt size per call at each batch size, with the instruction block's share.

    The old 15 was chosen when a ~46k-token roster prefix was cached and amortized across the
    batch; with the prefix gone the arithmetic is a different one entirely — a ~2k instruction
    block, re-sent uncached on every call, against per-story candidate lists that now dominate
    the request (spec §4.2). This is that re-derivation.

    **Every batch size is measured over the same pool of stories**, chunked, rather than over
    that pool's first `size` entries. Stories differ wildly in how many candidates they
    retrieve, so slicing would report story composition as if it were amortization — and it
    did, non-monotonically, until this was fixed.

    `counter` is the token-counting client, or None to fall back to the character estimate.
    Its lifetime belongs to the caller — a measurement helper should not be deciding when an
    HTTP client closes."""

    async def tokens(system: list[dict[str, Any]], messages: list[dict[str, Any]]) -> int:
        if counter is None:
            return _estimate_tokens(_request_text(system, messages))
        # Same `type: ignore` the production client carries: the request builders hand back
        # plain dicts, which the SDK's TypedDict params do not accept structurally.
        counted = await counter.messages.count_tokens(
            model=get_settings().link_model,
            system=system,  # type: ignore[arg-type]
            messages=messages,  # type: ignore[arg-type]
        )
        return counted.input_tokens

    async def reply_tokens(text: str) -> int:
        """Token count of a would-be reply — the output side of the batch-size question.

        Counted as a user message because the endpoint prices inputs; the tokenizer is the
        same one the reply is generated against, which is what the `max_tokens` comparison
        needs."""
        if counter is None:
            return _estimate_tokens(text)
        counted = await counter.messages.count_tokens(
            model=get_settings().link_model, messages=[{"role": "user", "content": text}]
        )
        return counted.input_tokens

    entries = [
        StoryCandidates(
            story=story,
            candidates=select_candidates(
                index,
                headline=story.title,
                dek=story_dek(story),
                threshold=threshold,
                limit=limit,
            ),
        )
        for story in stories
    ]
    # Only stories with candidates reach the model — the zero-candidate ones are rejected
    # before the call (ADR-0009), so they cost nothing and must not pad the batch's size.
    entries = [e for e in entries if not e.candidates.is_empty]

    instructions = await tokens(
        [{"type": "text", "text": _RETRIEVAL_INSTRUCTIONS}], [{"role": "user", "content": "{}"}]
    )
    candidate_tokens = [
        await reply_tokens(json.dumps(render_candidates(entry.candidates), ensure_ascii=False))
        for entry in entries[:_CANDIDATE_COST_SAMPLE]
    ]
    offered = sum(len(e.candidates.candidates) for e in entries[:_CANDIDATE_COST_SAMPLE])
    lines = [
        f"Instruction block: **{instructions} tok**, re-sent uncached on every call "
        f"(below Haiku 4.5's 4096-token cache floor). Candidate list: "
        f"**{sum(candidate_tokens) / len(candidate_tokens):.0f} tok/story** over "
        f"{len(candidate_tokens)} stories, **{sum(candidate_tokens) / offered:.0f} tok** per "
        "rendered candidate.",
        "",
        "| batch | calls | input tok/call | tok/story | instructions share | "
        f"worst-case reply tok/call | headroom vs max_tokens={_MAX_TOKENS} |",
        "|---|---|---|---|---|---|---|",
    ]
    for size in batch_sizes:
        # The same stories at every size, so the only thing moving between rows is how the
        # instruction block is spread over them.
        chunks = [entries[i : i + size] for i in range(0, len(entries) - size + 1, size)]
        if not chunks:
            break
        counted = [
            await tokens(*build_retrieval_link_request(chunk, datetime.now(UTC).date()))
            for chunk in chunks
        ]
        stories_counted = sum(len(chunk) for chunk in chunks)
        reply = await reply_tokens(_reply_for(chunks[0]))
        lines.append(
            f"| {size} | {len(chunks)} | {sum(counted) / len(counted):.0f} | "
            f"{sum(counted) / stories_counted:.0f} | "
            f"{100 * instructions * len(chunks) / sum(counted):.1f}% | "
            f"{reply} | {_MAX_TOKENS - reply} |"
        )
    return "\n".join(lines)


async def _window_stories(session: AsyncSession, *, days: int, limit: int) -> list[Story]:
    """A deterministic head of the traffic window, materialized.

    The sub-measurements that need whole `Story` rows rather than score vectors: prompt sizes
    and the catalog-growth curve. Contiguous by id on purpose — batches are chunked before
    retrieval runs, so a real batch is whatever stories happen to sit together, zero-candidate
    ones included."""
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return list(
        (
            await session.execute(
                select(Story)
                .where(func.coalesce(Story.published_at, Story.fetched_at) >= cutoff)
                .order_by(Story.id)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def _amain(args: argparse.Namespace) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    counter = (
        AsyncAnthropic(api_key=get_settings().anthropic_api_key) if args.count_tokens else None
    )
    try:
        async with SessionLocal() as session:
            index = await build_candidate_index(session)
            rows = await load_scores(session, index, days=args.days, sample=args.sample)
            sample = await _window_stories(session, days=args.days, limit=SCALING_SAMPLE)
            batch_table = await format_batch_table(
                index,
                sample[: max(args.batch_sizes) * 4],
                threshold=args.threshold,
                limit=args.limit,
                batch_sizes=args.batch_sizes,
                counter=counter,
            )
            scaling_table = format_scaling_table(
                index,
                sample,
                threshold=args.threshold,
                limit=args.limit,
                catalog_sizes=args.catalog_sizes,
            )
    finally:
        if counter is not None:
            await counter.close()

    picks = sum(1 for r in rows if r.has_pick)
    print(f"# Retrieval tuning — {datetime.now(UTC).date().isoformat()}\n")
    print(
        f"Corpus: **{len(rows)} stories** over the last {args.days} days, "
        f"**{picks} roster picks** still in the active catalog. "
        f"Index: **{index.size} films**, {index.token_count} tokens, "
        f"{len(index.rescue_folds)} rescue folds.\n"
    )
    print("## Threshold (uncapped)\n")
    print(format_threshold_table(rows, args.thresholds))
    print("\n## Cap\n")
    print(format_cap_table(rows, args.threshold, args.limits))
    print(f"\n## Catalog growth (T={args.threshold}, K={args.limit}, {len(sample)} stories)\n")
    print(scaling_table)
    print("\n## Batch size\n")
    print(batch_table)


def _floats(raw: str) -> tuple[float, ...]:
    return tuple(float(part) for part in raw.split(","))


def _ints(raw: str) -> tuple[int, ...]:
    return tuple(int(part) for part in raw.split(","))


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=30, help="traffic window to sweep over")
    parser.add_argument("--sample", type=int, default=None, help="cap the corpus, for a smoke run")
    parser.add_argument("--thresholds", type=_floats, default=THRESHOLDS)
    parser.add_argument("--limits", type=_ints, default=LIMITS)
    parser.add_argument("--batch-sizes", type=_ints, default=BATCH_SIZES)
    parser.add_argument("--catalog-sizes", type=_ints, default=CATALOG_SIZES)
    parser.add_argument(
        "--threshold",
        type=float,
        default=settings.link_retrieval_threshold,
        help="the T the cap and batch tables are measured at",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=settings.link_retrieval_max_candidates,
        help="the K the batch table is measured at",
    )
    parser.add_argument(
        "--count-tokens",
        action="store_true",
        help="count prompt tokens with the Anthropic token-counting endpoint (free) "
        "instead of estimating them at four characters per token",
    )
    asyncio.run(_amain(parser.parse_args()))


if __name__ == "__main__":
    main()
