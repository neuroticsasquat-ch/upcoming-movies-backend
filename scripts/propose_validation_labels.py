"""Pre-fill the hand-labeling draft with *candidate* labels so building the validation set
is a review-and-correct pass instead of labeling 490 rows from scratch.

Run in the container with a real key in .env (writes JSON to stdout, logs to stderr):
    task shell
    python scripts/propose_validation_labels.py < tests/fixtures/link/validation_draft.json \
        > tests/fixtures/link/validation_candidates.json

Then open validation_candidates.json, correct the proposals (see the anchoring note below),
keep ~150-200 rows, and save the curated result as tests/fixtures/link/validation_set.json.

Anti-anchoring: candidates are proposed by a STRONGER model (Sonnet) than the production
Stage-1 linker (Haiku, `link_model`). The validation set then measures the Haiku linker
against a Sonnet-proposed, human-corrected ground truth — not against its own output. Still
read every proposal: a model-assisted set inherits the proposer's blind spots if you
rubber-stamp it. Pay special attention to the about/mention boundary and the film id."""

import asyncio
import json
import sys

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import _extract_json_array
from upmovies.link.roster import build_roster
from upmovies.llm.client import AnthropicClient, cached_system_block

# A strong model, deliberately distinct from settings.link_model (the Haiku linker under
# test) so the human reviews independent proposals. Override with the first CLI arg.
PROPOSAL_MODEL = "claude-sonnet-4-6"
BATCH_SIZE = 25
MAX_TOKENS = 4096
SUMMARY_MAX = 500

EVENT_TYPES = (
    "announced",
    "casting",
    "production_start",
    "production_wrap",
    "release_date",
    "trailer",
    "other",
)

_INSTRUCTIONS = f"""You label news stories for an upcoming-movies tracker's validation set.

You are given a ROSTER of tracked films (each line starts with its TMDB id) and a batch of \
news stories (each an id, headline, and short dek). For every story decide its relation to \
the roster:

- "about": the story is PRIMARILY about exactly one tracked film (its casting, release \
date, trailer, production milestone, announcement). Set "tmdb_id" to that film's TMDB id \
and "event_type" to one of: {", ".join(EVENT_TYPES)}.
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
null>, "event_type": <one of the types above, or null>}}]"""


def _roster_text(entries, tmdb_by_film_id) -> str:
    """Render roster lines keyed by TMDB id (the fixture's film key), reusing the same
    title/year/orig/genres/overview content the production roster builds."""
    lines = []
    for e in entries:
        tmdb_id = tmdb_by_film_id.get(e.film_id)
        if tmdb_id is None:
            continue
        parts = [f'tmdb={tmdb_id} "{e.title}"']
        if e.year is not None:
            parts.append(f"({e.year})")
        if e.original_title and e.original_title != e.title:
            parts.append(f"[orig: {e.original_title}]")
        if e.genres:
            parts.append(f"genres: {', '.join(e.genres)}")
        line = " ".join(parts)
        if e.overview:
            line += f" — {e.overview}"
        lines.append(line)
    return "\n".join(lines)


async def main(model: str) -> None:
    draft = json.load(sys.stdin)
    print(f"loaded {len(draft)} draft rows", file=sys.stderr)

    settings = get_settings()
    async with SessionLocal() as s:
        roster = await build_roster(s)
        tmdb_by_film_id = {
            row.id: row.tmdb_id for row in (await s.execute(select(Film))).scalars().all()
        }
    roster_tmdb_ids = set(tmdb_by_film_id.values())
    system = [
        cached_system_block(
            f"{_INSTRUCTIONS}\n\nROSTER:\n{_roster_text(roster.entries, tmdb_by_film_id)}"
        )
    ]
    print(f"roster: {len(roster_tmdb_ids)} films | proposer model: {model}", file=sys.stderr)

    # id == draft index, so proposals map back unambiguously regardless of model ordering.
    proposals: dict[str, dict] = {}
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for start in range(0, len(draft), BATCH_SIZE):
            chunk = draft[start : start + BATCH_SIZE]
            payload = [
                {
                    "id": str(start + i),
                    "title": row["title"],
                    "summary": (row.get("summary") or "")[:SUMMARY_MAX],
                }
                for i, row in enumerate(chunk)
            ]
            raw = await client.complete(
                model=model,
                system=system,
                messages=[{"role": "user", "content": json.dumps(payload)}],
                max_tokens=MAX_TOKENS,
            )
            for d in json.loads(_extract_json_array(raw)):
                proposals[str(d.get("id"))] = d
            print(f"  proposed {min(start + BATCH_SIZE, len(draft))}/{len(draft)}", file=sys.stderr)

    out = []
    missing = bad_tmdb = 0
    for i, row in enumerate(draft):
        p = proposals.get(str(i))
        relation, tmdb_id, event_type = "TODO", None, None
        if p is None:
            missing += 1
        else:
            relation = (
                p.get("relation") if p.get("relation") in ("about", "mention", "none") else "TODO"
            )
            if relation == "about":
                tmdb_id = p.get("tmdb_id")
                if tmdb_id not in roster_tmdb_ids:  # hallucinated / out-of-roster id
                    bad_tmdb += 1
                    tmdb_id = None  # leave blank so the reviewer must supply it
                event_type = p.get("event_type") if p.get("event_type") in EVENT_TYPES else None
        out.append(
            {
                "url": row["url"],
                "source": row["source"],
                "title": row["title"],
                "summary": row.get("summary", ""),
                "relation": relation,
                "expected_film_tmdb_id": tmdb_id,
                "event_type": event_type,
                "event_group": None,  # cross-story; left for the human review pass
            }
        )

    n_about = sum(1 for r in out if r["relation"] == "about")
    n_mention = sum(1 for r in out if r["relation"] == "mention")
    n_none = sum(1 for r in out if r["relation"] == "none")
    print(
        f"proposals: {n_about} about, {n_mention} mention, {n_none} none, {missing} undecided; "
        f"{bad_tmdb} out-of-roster tmdb ids blanked",
        file=sys.stderr,
    )
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else PROPOSAL_MODEL))
