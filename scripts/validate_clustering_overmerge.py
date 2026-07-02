"""Over-merge diagnostic for Stage-2 clustering (NEU-300 follow-up).

`validate_clustering.py` mirrors production: it clusters only the newsworthy rows, so a
trailer-dominated fixture collapses to ~one beat per film and purity/precision are forced to
1.0 (nothing to over-merge). This script instead clusters a film's FULL `about` row set —
including rows excluded as not-news — and treats each row's gold beat as its `event_group`,
or a per-row singleton when it has none. A film with one multi-story beat plus several
distinct singletons is then a real over-merge test: the clusterer must keep the dominant beat
PURE and not absorb the distractor pieces. (This also measures robustness to Stage-1 leakage.)

Run in the container (optional first arg = a single tmdb_id for a verbose breakdown):
    docker compose exec -T upmovies-backend python scripts/validate_clustering_overmerge.py 969681
    docker compose exec -T upmovies-backend python scripts/validate_clustering_overmerge.py        # all films
"""

# ruff: noqa: E501  -- long example/print lines in a localdev diagnostic

import asyncio
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.cluster import assemble_cluster_payload, parse_cluster_groups
from upmovies.link.metrics import compute_cluster_metrics
from upmovies.link.validation import load_validation_set
from upmovies.llm.client import AnthropicClient

FIX = "tests/fixtures/link/validation_set.json"
_SUMMARY_MAX = 500


def _gold_key(it) -> str:
    return it.event_group or f"__singleton__{it.url}"


async def main(only_tmdb: int | None) -> None:
    settings = get_settings()
    items = load_validation_set(FIX)
    by_film: dict[int, list] = defaultdict(list)
    for it in items:
        if it.relation == "about" and it.expected_film_tmdb_id is not None:
            by_film[it.expected_film_tmdb_id].append(it)
    async with SessionLocal() as s:
        film_by_tmdb = {f.tmdb_id: f for f in (await s.execute(select(Film))).scalars().all()}

    gold: dict[str, str] = {}
    predicted: list[set[str]] = []
    n_films = 0

    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for tmdb, fitems in by_film.items():
            if only_tmdb and tmdb != only_tmdb:
                continue
            film = film_by_tmdb.get(tmdb)
            if film is None:
                continue
            # Only films with a multi-story beat can exercise over-merge; skip pure-singleton films.
            if max(Counter(_gold_key(it) for it in fitems).values()) < 2:
                continue
            n_films += 1
            for it in fitems:
                gold[it.url] = _gold_key(it)
            new_payload = [
                {"n": i, "title": it.title, "summary": (it.summary or "")[:_SUMMARY_MAX]}
                for i, it in enumerate(fitems, start=1)
            ]
            system, messages = assemble_cluster_payload(
                film_title=film.title,
                film_year=film.release_date.year if film.release_date else None,
                existing_payload=[],
                new_payload=new_payload,
                run_date=datetime.now(UTC).date(),
            )
            raw = await client.complete(
                model=settings.cluster_model,
                system=system,
                messages=messages,
                max_tokens=settings.link_cluster_max_tokens,
            )
            groups = parse_cluster_groups(raw, n_stories=len(fitems))
            if groups is None:
                print(f"WARNING: unparseable cluster response for {film.title}")
                continue
            assigned: set[str] = set()
            film_clusters: list[tuple[object, list[str]]] = []
            for g in groups:
                cl: list[str] = []
                for n in g.story_indices:
                    u = fitems[n - 1].url
                    if u in assigned:
                        continue
                    assigned.add(u)
                    cl.append(u)
                if cl:
                    predicted.append(set(cl))
                    film_clusters.append((g, cl))
            if only_tmdb:
                _print_film(film.title, fitems, film_clusters, gold)

    m = compute_cluster_metrics(predicted, gold)
    print("\n=== OVER-MERGE DIAGNOSTIC (full about-set; distractors as singletons) ===")
    print(f"films={n_films}  items={m.n_items}")
    print(f"purity={m.purity:.3f}")
    print(
        f"pairwise: precision={m.pairwise_precision:.3f} recall={m.pairwise_recall:.3f} "
        f"f1={m.pairwise_f1:.3f}  (predicted_pairs={m.n_predicted_pairs}, gold_pairs={m.n_gold_pairs})"
    )
    print("precision < 1.0  =>  the clusterer OVER-MERGED distinct pieces into a cluster")
    print("recall    < 1.0  =>  the clusterer OVER-SPLIT a real multi-story beat")


def _print_film(title, fitems, film_clusters, gold) -> None:
    by_url = {it.url: it for it in fitems}
    print(f"\n{title}: {len(fitems)} about rows -> {len(film_clusters)} predicted event(s)")
    for g, cl in film_clusters:
        golds = {gold[u] for u in cl}
        flag = "   <== MIXED (over-merge)" if len(golds) > 1 else ""
        print(f"  event [{getattr(g, 'event_type', None)}] size {len(cl)}{flag}")
        for u in cl:
            it = by_url[u]
            real = "real-beat " if not gold[u].startswith("__singleton__") else "singleton "
            print(f"     {real} {it.title[:62]}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else None))
