"""Explain a link path's misses and sweep the confidence floor. Runs the linker once at
floor 0.0 (so every story carries the model's raw pick + confidence), then classifies at the
real floor and prints each false negative / false positive with a diagnosis, plus a
precision/recall sweep across candidate floors.

    task shell
    python scripts/diagnose_linking.py --mode retrieval
    python scripts/diagnose_linking.py --mode roster tests/fixtures/link/validation_set.json

**One run answers the whole floor question.** The floor is a post-hoc threshold on the
confidence the model already returned, so classifying one floor-0.0 run at each candidate
floor costs nothing extra. That is what makes this the cheap first move when precision is
the thing under investigation (NEU-1011) — `validate_linking` prices one floor per run.

**`--mode retrieval` is the path that ships.** The roster mode is the incumbent baseline and
goes away with it (NEU-1004). Both read the catalog as of the fixture's `as_of_date`, for the
reason NEU-1010 records: a dated fixture scored against today's catalog loses its own
subjects and reads that loss as the path's failure."""

import argparse
import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from scripts.validate_linking import _predicted_tmdb_ids
from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import (
    StoryCandidates,
    link_retrieval_story_batch,
    link_story_batch,
    reject_zero_candidate_stories,
    story_dek,
)
from upmovies.link.metrics import compute_link_metrics
from upmovies.link.retrieval.index import build_candidate_index
from upmovies.link.retrieval.select import select_candidates
from upmovies.link.roster import build_roster
from upmovies.link.validation import films_ingested_after, load_validation_set
from upmovies.llm.client import AnthropicClient, CallLog
from upmovies.news.models import Story

DEFAULT_FIXTURE = "tests/fixtures/link/validation_set.json"
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9]


async def main(path: str, *, mode: str, threshold: float, limit: int) -> None:
    settings = get_settings()
    validation_set = load_validation_set(path)
    items = validation_set.items
    catalog_date = validation_set.as_of_date or datetime.now(UTC).date()

    async with SessionLocal() as s:
        roster = await build_roster(s, as_of=catalog_date) if mode == "roster" else None
        index = await build_candidate_index(s, as_of=catalog_date) if mode == "retrieval" else None
        films = (await s.execute(select(Film))).scalars().all()
    unscoreable = films_ingested_after(
        validation_set.as_of_date, {f.tmdb_id: f.created_at.date() for f in films}
    )
    tmdb_by_film_id = {f.id: f.tmdb_id for f in films}
    label_by_tmdb = {
        f.tmdb_id: (f"{f.title} ({f.release_date.year})" if f.release_date else f.title)
        for f in films
    }

    stories = [
        Story(id=uuid4(), source=it.source, url=it.url, title=it.title, raw={"summary": it.summary})
        for it in items
    ]
    item_by_id = {str(st.id): it for st, it in zip(stories, items, strict=True)}

    # floor=0.0 so the model's pick + confidence is captured for every story.
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client:
        for i in range(0, len(stories), settings.link_batch_size):
            chunk = stories[i : i + settings.link_batch_size]
            if roster is not None:
                await link_story_batch(
                    client=client,
                    model=settings.link_model,
                    roster=roster,
                    stories=chunk,
                    floor=0.0,
                    run_date=catalog_date,
                    calls=CallLog(),
                )
                continue
            assert index is not None
            batch = [
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
                for story in chunk
            ]
            reject_zero_candidate_stories([e.story for e in batch if e.candidates.is_empty])
            await link_retrieval_story_batch(
                client=client,
                model=settings.link_model,
                batch=[e for e in batch if not e.candidates.is_empty],
                floor=0.0,
                run_date=catalog_date,
                calls=CallLog(),
            )

    floor = settings.link_confidence_floor
    # Shared with validate_linking so the two tools cannot disagree about which picks are
    # scoreable — post-labeling films read as "no link" in both (NEU-1011).
    pick_by_story = dict(
        zip(
            (str(st.id) for st in stories),
            _predicted_tmdb_ids(stories, tmdb_by_film_id, unscoreable=unscoreable),
            strict=True,
        )
    )
    rows = []  # (item, pick_tmdb, conf, note)
    for st in stories:
        it = item_by_id[str(st.id)]
        rows.append((it, pick_by_story[str(st.id)], st.link_confidence, st.link_note))

    def label(t):
        return label_by_tmdb.get(t, "?") if t is not None else "—"

    print(
        f"fixture: {len(items)} items | mode={mode} | model={settings.link_model} | "
        f"real floor={floor} | catalog as of {catalog_date}"
    )
    if mode == "retrieval":
        print(f"retrieval: threshold={threshold} max_candidates={limit}")
    print()

    print("=== FALSE NEGATIVES (should link, missed at real floor) ===")
    fn_below = fn_wrong = fn_declined = 0
    for it, pick, conf, note in rows:
        exp = it.expected_film_tmdb_id
        if exp is None:
            continue
        linked = pick is not None and conf is not None and conf >= floor
        if linked and pick == exp:
            continue  # TP
        if pick == exp:  # right film, lost to the floor
            fn_below += 1
            diag = f"BELOW FLOOR (conf={conf:.2f})"
        elif pick is not None:
            fn_wrong += 1
            diag = f"WRONG FILM → {label(pick)} (conf={conf:.2f})"
        else:
            fn_declined += 1
            diag = f"MODEL DECLINED (note={note})"
        print(f"  [{diag}]")
        print(f"    expected: {label(exp)}")
        print(f"    story:    {it.title[:90]}")
        print(f"    {it.url}")
    print(f"  -> {fn_below} below-floor, {fn_wrong} wrong-film, {fn_declined} declined\n")

    print("=== FALSE POSITIVES (linked at real floor, but shouldn't be / wrong) ===")
    for it, pick, conf, _note in rows:
        exp = it.expected_film_tmdb_id
        linked = pick is not None and conf is not None and conf >= floor
        if linked and pick != exp:
            print(
                f"    picked: {label(pick)} (conf={conf:.2f}) | "
                f"expected: {label(exp)} | relation={it.relation}"
            )
            print(f"    story:  {it.title[:90]}")
            print(f"    {it.url}")
    print()

    print("=== FLOOR SWEEP ===")
    print(f"  {'floor':>6} {'P':>6} {'R':>6} {'F1':>6} {'FP':>4} {'FN':>4}")
    for f in SWEEP:
        pairs = [
            (
                pick if (pick is not None and conf is not None and conf >= f) else None,
                it.expected_film_tmdb_id,
            )
            for it, pick, conf, _note in rows
        ]
        m = compute_link_metrics(pairs)
        star = "  <- current" if abs(f - floor) < 1e-9 else ""
        print(
            f"  {f:>6.2f} {m.precision:>6.3f} {m.recall:>6.3f} {m.f1:>6.3f} "
            f"{m.false_positives:>4} {m.false_negatives:>4}{star}"
        )


if __name__ == "__main__":
    _settings = get_settings()
    _parser = argparse.ArgumentParser(description=__doc__)
    _parser.add_argument("fixture", nargs="?", default=DEFAULT_FIXTURE)
    _parser.add_argument("--mode", choices=("roster", "retrieval"), default="retrieval")
    _parser.add_argument("--threshold", type=float, default=_settings.link_retrieval_threshold)
    _parser.add_argument("--limit", type=int, default=_settings.link_retrieval_max_candidates)
    _args = _parser.parse_args()
    asyncio.run(main(_args.fixture, mode=_args.mode, threshold=_args.threshold, limit=_args.limit))
