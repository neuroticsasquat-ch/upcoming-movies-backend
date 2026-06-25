"""Measure Stage-2 clustering accuracy (purity + pairwise dedup) against the labeled
fixture, using the real cluster_model. Approach A: in-memory, read-only DB for film
metadata, no DB writes. Run in the container with a real key in .env:

    task shell
    python scripts/validate_clustering.py [tests/fixtures/link/validation_set.json]

Prints cluster purity, pairwise precision/recall/F1, event_type agreement, and the
NEU-300 gate verdict. Costs one Sonnet cluster call per scoreable film. Record the
numbers in docs/specs/2026-06-25-neu-300-stage2-cluster-purity-design.md."""

import asyncio
import sys
from collections import Counter, defaultdict

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.cluster import assemble_cluster_payload, parse_cluster_groups
from upmovies.link.metrics import compute_cluster_metrics
from upmovies.link.validation import ValidationItem, load_validation_set
from upmovies.llm.client import AnthropicClient

DEFAULT_FIXTURE = "tests/fixtures/link/validation_set.json"
_SUMMARY_MAX = 500  # mirror cluster.build_cluster_request's _SUMMARY_MAX

# NEU-300 gate (see the spec).
_GREEN = (0.90, 0.80)
_FLOOR = (0.80, 0.65)


def _clustering_rows(items: list[ValidationItem]) -> dict[int, list[ValidationItem]]:
    """Rows that would actually link and thus cluster: 'about' + production-news + has
    an expected film, grouped by expected tmdb_id."""
    by_film: dict[int, list[ValidationItem]] = defaultdict(list)
    for it in items:
        if (
            it.relation == "about"
            and it.is_production_news is not False
            and it.expected_film_tmdb_id is not None
        ):
            by_film[it.expected_film_tmdb_id].append(it)
    return by_film


async def main(path: str) -> None:
    settings = get_settings()
    items = load_validation_set(path)
    by_film = _clustering_rows(items)

    async with SessionLocal() as s:
        film_by_tmdb = {f.tmdb_id: f for f in (await s.execute(select(Film))).scalars().all()}

    gold_group_by_key: dict[str, str | None] = {}
    gold_type_by_key: dict[str, str | None] = {}
    predicted_clusters: list[set[str]] = []
    predicted_type_by_cluster: list[str | None] = []
    n_films = n_no_film = n_unlabeled = n_unparseable = 0

    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for tmdb_id, film_items in by_film.items():
            film = film_by_tmdb.get(tmdb_id)
            if film is None:
                n_no_film += 1
                print(f"WARNING: film tmdb={tmdb_id} not in DB; skipping {len(film_items)} row(s)")
                continue
            labeled = [it for it in film_items if it.event_group]
            n_unlabeled += len(film_items) - len(labeled)
            if not labeled:
                continue
            n_films += 1
            for it in labeled:
                gold_group_by_key[it.url] = it.event_group
                gold_type_by_key[it.url] = it.event_type

            new_payload = [
                {"n": i, "title": it.title, "summary": (it.summary or "")[:_SUMMARY_MAX]}
                for i, it in enumerate(labeled, start=1)
            ]
            system, messages = assemble_cluster_payload(
                film_title=film.title,
                film_year=film.release_date.year if film.release_date else None,
                existing_payload=[],
                new_payload=new_payload,
            )
            raw = await client.complete(
                model=settings.cluster_model,
                system=system,
                messages=messages,
                max_tokens=settings.link_cluster_max_tokens,
            )
            groups = parse_cluster_groups(raw, n_stories=len(labeled))
            if groups is None:
                n_unparseable += 1
                print(f"WARNING: unparseable cluster response for tmdb={tmdb_id} ({film.title})")
                continue
            assigned: set[str] = set()
            for g in groups:
                cluster: set[str] = set()
                for n in g.story_indices:
                    url = labeled[n - 1].url
                    if url in assigned:
                        continue
                    assigned.add(url)
                    cluster.add(url)
                if cluster:
                    predicted_clusters.append(cluster)
                    predicted_type_by_cluster.append(g.event_type)

    m = compute_cluster_metrics(predicted_clusters, gold_group_by_key)

    # Secondary (non-gating): predicted new-event type vs the majority gold type of members.
    type_total = type_correct = 0
    for cluster, ptype in zip(predicted_clusters, predicted_type_by_cluster, strict=True):
        if ptype is None:
            continue
        gold_types = Counter(gold_type_by_key.get(u) for u in cluster)
        majority = max(gold_types, key=lambda k: gold_types[k]) if gold_types else None
        type_total += 1
        if ptype == majority:
            type_correct += 1
    type_agreement = type_correct / type_total if type_total else 0.0

    if m.purity >= _GREEN[0] and m.pairwise_f1 >= _GREEN[1]:
        verdict = "GREEN — good enough; NEU-282 stays deferred (not triggered)."
    elif m.purity >= _FLOOR[0] and m.pairwise_f1 >= _FLOOR[1]:
        verdict = "TUNE — run one cluster-prompt tuning iteration, then re-measure."
    else:
        verdict = "ESCALATE — below floor; scope NEU-282 (embeddings) per the gate."

    print("\n=== STAGE-2 CLUSTERING (gold event_group baseline) ===")
    print(f"model={settings.cluster_model}  scoreable_films={n_films}  items={m.n_items}")
    print(
        f"skipped: {n_no_film} film(s) not in DB, {n_unlabeled} unlabeled row(s), "
        f"{n_unparseable} unparseable"
    )
    print(f"purity={m.purity:.3f}")
    print(
        f"pairwise: precision={m.pairwise_precision:.3f} recall={m.pairwise_recall:.3f} "
        f"f1={m.pairwise_f1:.3f}  "
        f"(predicted_pairs={m.n_predicted_pairs}, gold_pairs={m.n_gold_pairs})"
    )
    print(f"event_type agreement (secondary)={type_agreement:.3f} over {type_total} new event(s)")
    if m.n_gold_pairs < 10:
        print(
            f"NOTE: only {m.n_gold_pairs} gold pair(s) — treat pairwise as DIRECTIONAL; "
            f"prefer keeping NEU-282 deferred."
        )
    print(f"GATE: {verdict}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FIXTURE))
