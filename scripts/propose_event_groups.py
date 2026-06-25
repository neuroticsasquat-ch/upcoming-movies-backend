"""Pre-fill gold event_group labels so building the Stage-2 validation set is a
review-and-correct pass. For each tracked film's linkable 'about' rows, a STRONG model
(Opus — deliberately distinct from the Sonnet cluster_model under test, to avoid measuring
the model against itself) groups them into production beats and names each beat. Writes the
full fixture with event_group filled to stdout, logs to stderr:

    task shell
    python scripts/propose_event_groups.py < tests/fixtures/link/validation_set.json \\
        > tests/fixtures/link/validation_eventgroups.json

Then open the output, correct the beat groupings (read every one — a model-assisted set
inherits the proposer's blind spots), and save the curated result as
tests/fixtures/link/validation_set.json."""

import asyncio
import json
import sys
from collections import defaultdict

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import _extract_json_array
from upmovies.llm.client import AnthropicClient

PROPOSAL_MODEL = "claude-opus-4-8"  # stronger than cluster_model (Sonnet); override via argv[1]
MAX_TOKENS = 2048
SUMMARY_MAX = 500

_INSTRUCTIONS = """You group an upcoming-movies tracker's news stories about ONE film into \
distinct production BEATS — real moments in its life (a casting, a trailer drop, a \
release-date change, filming start/wrap, a director/studio change, etc.). Stories reporting \
the SAME beat share a group; different beats are different groups. Five outlets reporting the \
same casting are ONE beat.

You are given the FILM and a list of its stories (each an integer id "n", a headline, and a \
short dek). Assign every story a short kebab-case beat slug naming the beat (e.g. \
"doctor-doom-casting", "first-trailer", "release-date-shift", "filming-wrap").

Return ONLY a JSON array — no prose, no markdown — one object per input story:
[{"n": <id>, "beat": "<kebab-slug>"}]"""


async def main(model: str) -> None:
    rows = json.load(sys.stdin)
    print(f"loaded {len(rows)} fixture rows", file=sys.stderr)

    settings = get_settings()
    async with SessionLocal() as s:
        film_by_tmdb = {f.tmdb_id: f for f in (await s.execute(select(Film))).scalars().all()}

    # row index -> proposed event_group; only for linkable 'about' rows with a known film.
    by_film: dict[int, list[int]] = defaultdict(list)
    for i, row in enumerate(rows):
        if (
            row.get("relation") == "about"
            and row.get("is_production_news") is not False
            and row.get("expected_film_tmdb_id") in film_by_tmdb
        ):
            by_film[row["expected_film_tmdb_id"]].append(i)

    print(f"{len(by_film)} film(s) with linkable about-rows | proposer model: {model}", file=sys.stderr)

    event_group: dict[int, str] = {}
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for tmdb_id, idxs in by_film.items():
            film = film_by_tmdb[tmdb_id]
            payload = [
                {
                    "n": j,
                    "title": rows[idx]["title"],
                    "summary": (rows[idx].get("summary") or "")[:SUMMARY_MAX],
                }
                for j, idx in enumerate(idxs)
            ]
            user = {"film": {"title": film.title, "year": film.release_date.year if film.release_date else None},
                    "stories": payload}
            raw = await client.complete(
                model=model,
                system=[{"type": "text", "text": _INSTRUCTIONS}],
                messages=[{"role": "user", "content": json.dumps(user)}],
                max_tokens=MAX_TOKENS,
            )
            try:
                proposals = {int(d["n"]): d.get("beat") for d in json.loads(_extract_json_array(raw))}
            except (json.JSONDecodeError, KeyError, ValueError, TypeError):
                print(f"  WARNING: unparseable proposal for tmdb={tmdb_id} ({film.title})", file=sys.stderr)
                proposals = {}
            for j, idx in enumerate(idxs):
                beat = proposals.get(j)
                if isinstance(beat, str) and beat.strip():
                    event_group[idx] = f"{tmdb_id}-{beat.strip()}"
            print(f"  proposed {film.title} ({len(idxs)} rows)", file=sys.stderr)

    out = [{**row, "event_group": event_group.get(i, row.get("event_group"))} for i, row in enumerate(rows)]
    n_labeled = sum(1 for r in out if r.get("event_group"))
    print(f"event_group proposed on {n_labeled} row(s)", file=sys.stderr)
    print(json.dumps(out, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else PROPOSAL_MODEL))
