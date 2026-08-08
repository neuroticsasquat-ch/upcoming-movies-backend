"""Pre-fill the hand-labeling draft with *candidate* labels so building the validation set
is a review-and-correct pass instead of labeling thousands of rows from scratch.

Run in the container with a real key in .env (writes JSON to stdout, logs to stderr):
    task shell
    python scripts/propose_validation_labels.py --as-of 2026-08-08 \
        < tests/fixtures/link/validation_draft.json \
        > tests/fixtures/link/validation_candidates.json

Then review every proposal (see the anchoring note below), keep the rows you want, and save
the curated result as tests/fixtures/link/validation_set.json.

Anti-anchoring: candidates are proposed by a STRONGER model (Sonnet) than the production
Stage-1 linker (Haiku, `link_model`). The validation set then measures the Haiku linker
against a Sonnet-proposed, human-corrected ground truth — not against its own output. Still
read every proposal: a model-assisted set inherits the proposer's blind spots if you
rubber-stamp it. Pay special attention to the about/mention boundary and the film id.

**`--as-of` is the fixture's pin, and it is not optional for a pinned set.** The film list
shown to the proposer defines what may be labeled `about`; the harness later scores the
fixture against the catalog as of the fixture's `as_of_date` and **aborts** if a labeled film
is not reachable there (spec §5.1a). Building that list at wall clock while the fixture is
pinned to some other date puts the two sets out of step — a film that released between the
pin and today is offered to the proposer and filtered out for the harness, and the row it
produces fails the coverage check. Passing the pin here makes the labelable set and the
scoreable set the same set by construction, which is what turns the pin's one-day coverage
margin into no margin needed at all.

**The list comes from the retrieval index, not the deleted roster (NEU-1004).** Both read
the same `active_film_clause` at the same `as_of`, so the labelable set is unchanged — what
moved is only which module owns the read. Note this script still sends its whole film list as
a **cached** prefix: it is the one remaining `cached_system_block` caller now that production
has stopped caching, and here the prefix is large and reused across ~160 batches in one
sitting, which is exactly the shape caching pays for."""

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable, Mapping
from datetime import date
from uuid import UUID

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import _extract_json_array
from upmovies.link.retrieval.index import IndexedFilm, build_candidate_index, indexed_tmdb_ids
from upmovies.llm.client import AnthropicClient, cached_system_block

# A strong model, deliberately distinct from settings.link_model (the Haiku linker under
# test) so the human reviews independent proposals. Override with --model.
PROPOSAL_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 25
MAX_TOKENS = 4096
SUMMARY_MAX = 500
# The overview trim the deleted roster applied to every line it rendered. Kept here because
# the retrieval index carries overviews untrimmed — trimming is a prompt-size decision.
_OVERVIEW_MAX = 120
# Batches are independent, so the draft's size is a latency problem rather than a design
# one: a 4,000-row draft is 160 calls, and sequentially that is hours of wall clock for work
# the API will happily do in parallel.
CONCURRENCY = 8

EVENT_TYPES = (
    "announced",
    "casting",
    "production_start",
    "production_wrap",
    "release_date",
    "trailer",
    "other",
)

EXCLUSION_CATEGORIES = (
    "reaction",
    "roundup",
    "streaming-move",
    "interview-quote",
    "downstream",
    "other",
)


def _proposal_to_row(row: dict, proposal: dict | None, labelable: set[int]) -> dict:
    """Build one fixture row from a draft row + the model's proposal. Sets the production-news
    axis only for 'about' proposals; exclusion_category only when is_production_news is False."""
    relation, tmdb_id, event_type = "TODO", None, None
    is_production_news, exclusion_category = None, None
    if proposal is not None:
        relation = (
            proposal.get("relation")
            if proposal.get("relation") in ("about", "mention", "none")
            else "TODO"
        )
        if relation == "about":
            tmdb_id = proposal.get("tmdb_id")
            if tmdb_id not in labelable:  # hallucinated / not in the labelable set
                tmdb_id = None
            event_type = (
                proposal.get("event_type") if proposal.get("event_type") in EVENT_TYPES else None
            )
            is_production_news = proposal.get("is_production_news")
            if not isinstance(is_production_news, bool):
                is_production_news = None
            if is_production_news is False:
                cat = proposal.get("exclusion_category")
                exclusion_category = cat if cat in EXCLUSION_CATEGORIES else None
    return {
        "url": row["url"],
        "source": row["source"],
        "title": row["title"],
        "summary": row.get("summary", ""),
        "relation": relation,
        "expected_film_tmdb_id": tmdb_id,
        "event_type": event_type,
        "event_group": None,
        "is_production_news": is_production_news,
        "exclusion_category": exclusion_category,
    }


_INSTRUCTIONS = f"""You label news stories for an upcoming-movies tracker's validation set.

You are given a ROSTER of tracked films (each line starts with its TMDB id) and a batch of \
news stories (each an id, headline, and short dek). For every story decide its relation to \
the roster:

- "about": the story is PRIMARILY about exactly one tracked film. Set "tmdb_id" to that \
film's TMDB id and "event_type" to one of: {", ".join(EVENT_TYPES)}. Also judge whether it \
is production news: set "is_production_news" true if it announces or confirms something NEW \
(casting, filming start/wrap, trailer, release date, a major creative/production change, a \
release-affecting distribution deal); false if it is merely about the film without new \
production info (a reaction, praise, an "everything we know" roundup, interview color, a \
streaming/catalogue move, or a downstream piece). When false, set "exclusion_category" to \
one of: reaction, roundup, streaming-move, interview-quote, downstream, other.
- "mention": a tracked film is only referenced in passing — a list, a comparison, an \
aside, or an actor's other project. Set "tmdb_id" and "event_type" to null.
- "none": the story is not about any tracked film (unrelated TV, games, sports, \
obituaries, already-released films). Most stories are "none"; that is expected. Set \
"tmdb_id" and "event_type" to null.

Be strict about same-titled / substring traps: the film "Runner" is not "showrunner" or \
"Blade Runner". Use year, original title, genres, and overview to disambiguate. Only use a \
TMDB id that appears in the ROSTER.

Return ONLY a JSON array — no prose, no markdown — one object per input story, using the \
story's id:
[{{"id": "<id>", "relation": "about"|"mention"|"none", "tmdb_id": <roster TMDB id or \
null>, "event_type": <one of the types above, or null>, "is_production_news": <true|false|\
null>, "exclusion_category": <category or null>}}]"""


def _film_list_text(films: Iterable[IndexedFilm], tmdb_by_film_id: Mapping[UUID, int]) -> str:
    """Render the labelable film list keyed by TMDB id (the fixture's film key).

    The prompt still calls this block `ROSTER:` — that is the *prompt's* word, and moving it
    would edit a prompt whose output is hand-reviewed ground truth. The module that used to
    build a roster is gone (NEU-1004); these rows come from the retrieval index instead. The
    line format is byte-for-byte what the roster rendered, including the 120-character
    overview trim, so a re-proposal against the same pin sees the same prefix it always did
    — `test_propose_validation_labels` pins that."""
    lines = []
    for f in films:
        tmdb_id = tmdb_by_film_id.get(f.film_id)
        if tmdb_id is None:
            continue
        parts = [f'tmdb={tmdb_id} "{f.title}"']
        if f.year is not None:
            parts.append(f"({f.year})")
        if f.original_title and f.original_title != f.title:
            parts.append(f"[orig: {f.original_title}]")
        if f.genres:
            parts.append(f"genres: {', '.join(f.genres)}")
        line = " ".join(parts)
        if f.overview:
            line += f" — {f.overview[:_OVERVIEW_MAX]}"
        lines.append(line)
    return "\n".join(lines)


async def _propose_batch(
    client: AnthropicClient,
    *,
    model: str,
    system: list[dict],
    draft: list[dict],
    start: int,
    semaphore: asyncio.Semaphore,
    progress: list[int],
) -> dict[str, dict]:
    """One batch of proposals, keyed by draft index.

    A batch that fails or returns unparseable JSON yields nothing rather than killing the
    run: at 160 batches a single bad reply should cost 25 undecided rows the human can see
    in the output, not four thousand rows of re-proposal."""
    chunk = draft[start : start + BATCH_SIZE]
    payload = [
        {
            "id": str(start + i),
            "title": row["title"],
            "summary": (row.get("summary") or "")[:SUMMARY_MAX],
        }
        for i, row in enumerate(chunk)
    ]
    async with semaphore:
        try:
            raw = await client.complete(
                model=model,
                system=system,
                messages=[{"role": "user", "content": json.dumps(payload)}],
                max_tokens=MAX_TOKENS,
            )
            batch = {str(d.get("id")): d for d in json.loads(_extract_json_array(raw))}
        except Exception as exc:  # reported, and the rows stay undecided
            print(f"  batch at {start} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            batch = {}
    # A one-element list is the counter: batches run concurrently but asyncio only
    # switches at awaits, and there is none between the read and the write.
    progress[0] += len(chunk)
    print(f"  proposed {progress[0]}/{len(draft)}", file=sys.stderr)
    return batch


async def main(args: argparse.Namespace) -> None:
    draft = json.load(sys.stdin)
    print(f"loaded {len(draft)} draft rows", file=sys.stderr)

    settings = get_settings()
    async with SessionLocal() as s:
        index = await build_candidate_index(s, as_of=args.as_of)
        tmdb_by_film_id = {
            row.id: row.tmdb_id for row in (await s.execute(select(Film))).scalars().all()
        }
    labelable = indexed_tmdb_ids(index.films, tmdb_by_film_id)
    system = [
        cached_system_block(
            f"{_INSTRUCTIONS}\n\nROSTER:\n{_film_list_text(index.films, tmdb_by_film_id)}"
        )
    ]
    print(
        f"labelable: {len(labelable)} films as of {args.as_of or 'today'} "
        f"({len(tmdb_by_film_id)} in catalog) | proposer model: {args.model}",
        file=sys.stderr,
    )

    # id == draft index, so proposals map back unambiguously regardless of model ordering.
    semaphore = asyncio.Semaphore(args.concurrency)
    progress = [0]
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        batches = await asyncio.gather(
            *(
                _propose_batch(
                    client,
                    model=args.model,
                    system=system,
                    draft=draft,
                    start=start,
                    semaphore=semaphore,
                    progress=progress,
                )
                for start in range(0, len(draft), BATCH_SIZE)
            )
        )
    proposals: dict[str, dict] = {k: v for batch in batches for k, v in batch.items()}

    out = [_proposal_to_row(row, proposals.get(str(i)), labelable) for i, row in enumerate(draft)]
    missing = sum(1 for i in range(len(draft)) if proposals.get(str(i)) is None)

    n_about = sum(1 for r in out if r["relation"] == "about")
    n_mention = sum(1 for r in out if r["relation"] == "mention")
    n_none = sum(1 for r in out if r["relation"] == "none")
    n_excluded = sum(1 for r in out if r["is_production_news"] is False)
    print(
        f"proposals: {n_about} about ({n_excluded} not-news), {n_mention} mention, "
        f"{n_none} none, {missing} undecided",
        file=sys.stderr,
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=PROPOSAL_MODEL, help="the proposing model")
    parser.add_argument(
        "--as-of",
        type=date.fromisoformat,
        default=None,
        help="read the catalog as of this date — the fixture's pin (default: today)",
    )
    parser.add_argument("--concurrency", type=int, default=CONCURRENCY, help="batches in flight")
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(_parse_args()))
