"""Explain the linking baseline's misses and sweep the confidence floor. Runs the linker
once at floor 0.0 (so every story carries the model's raw pick + confidence), then classifies
at the real floor and prints each false negative / false positive with a diagnosis, plus a
precision/recall sweep across candidate floors.

    task shell
    python scripts/diagnose_linking.py [tests/fixtures/link/validation_set.json]"""

import asyncio
import sys
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select

from upmovies.catalog.models import Film
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.linker import link_story_batch
from upmovies.link.metrics import compute_link_metrics
from upmovies.link.roster import build_roster
from upmovies.link.validation import load_validation_set
from upmovies.llm.client import AnthropicClient
from upmovies.news.models import Story

DEFAULT_FIXTURE = "tests/fixtures/link/validation_set.json"
SWEEP = [0.5, 0.6, 0.7, 0.75, 0.8, 0.9]


async def main(path: str) -> None:
    settings = get_settings()
    items = load_validation_set(path)

    async with SessionLocal() as s:
        roster = await build_roster(s)
        films = (await s.execute(select(Film))).scalars().all()
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
            await link_story_batch(
                client=client,
                model=settings.link_model,
                roster=roster,
                stories=stories[i : i + settings.link_batch_size],
                floor=0.0,
                run_date=datetime.now(UTC).date(),
            )

    floor = settings.link_confidence_floor
    rows = []  # (item, pick_tmdb, conf, note)
    for st in stories:
        it = item_by_id[str(st.id)]
        pick = tmdb_by_film_id.get(st.film_id) if st.film_id is not None else None
        rows.append((it, pick, st.link_confidence, st.link_note))

    def label(t):
        return label_by_tmdb.get(t, "?") if t is not None else "—"

    print(f"fixture: {len(items)} items | model={settings.link_model} | real floor={floor}\n")

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
                f"    picked: {label(pick)} (conf={conf:.2f}) | expected: {label(exp)} | relation={it.relation}"
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
            f"  {f:>6.2f} {m.precision:>6.3f} {m.recall:>6.3f} {m.f1:>6.3f} {m.false_positives:>4} {m.false_negatives:>4}{star}"
        )


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FIXTURE))
