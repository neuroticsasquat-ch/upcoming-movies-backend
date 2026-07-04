"""Diff the cluster stage's decisions between two models on a fixed local corpus.

Precondition: a real tmdb+feeds+LINK ingest has populated `news.story` with `linked`,
unclustered stories (link_status='linked', film_id set, no event_story row yet). If the
corpus is already clustered, run `--reset` ONCE first to delete all events so every linked
story becomes unclustered again. Then, in the container:

    task shell
    python scripts/eval_cluster_diff.py --reset                          # expose the corpus
    python scripts/eval_cluster_diff.py --model claude-sonnet-4-6 --out sonnet.json
    python scripts/eval_cluster_diff.py --model claude-haiku-4-5  --out haiku.json
    python scripts/eval_cluster_diff.py --diff sonnet.json haiku.json    # print the diff report

A/B fidelity: `run_model` is READ-ONLY — it builds each film's cluster request and calls the
model, but does NOT apply the decisions (no events created, no stale-stage rejections). Because
`build_cluster_request` makes no writes and each film is built exactly once from the same
starting state, both models see an identical corpus with zero pre-existing events. No reset or
snapshot is needed BETWEEN the two model runs (only the one `--reset` up front to expose the
corpus). The diff aligns stories across runs by their story_id (captured per film), never by the
positional index `n`, so it does not depend on Postgres returning rows in a stable order."""

import argparse
import asyncio
import itertools
import json
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, exists, select

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.cluster import build_cluster_request
from upmovies.llm.client import AnthropicClient
from upmovies.news.models import Event, EventStory, Story


async def _unclustered_film_ids(s) -> list[UUID]:
    clustered = exists().where(EventStory.story_id == Story.id)
    rows = await s.execute(
        select(Story.film_id)
        .where(Story.link_status == "linked", Story.film_id.is_not(None), ~clustered)
        .distinct()
    )
    return [r[0] for r in rows.all() if r[0] is not None]


async def run_model(model: str, attach_limit: int, max_tokens: int, limit: int | None) -> dict:
    """Drive the cluster stage per film with `model`; return per-film RAW decisions for diffing.

    READ-ONLY: builds each film's request and calls the model, but does NOT apply the decisions.
    Records the n->story_id and existing_idx->event_id maps so the diff can align by identity."""
    settings = get_settings()
    out: dict[str, dict] = {}
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client, SessionLocal() as s:
        film_ids = await _unclustered_film_ids(s)
        if limit is not None:
            film_ids = film_ids[:limit]
        for fid in film_ids:
            built = await build_cluster_request(
                s, film_id=fid, attach_limit=attach_limit,
                run_date=datetime.now(UTC).date(),
            )
            if built is None:
                continue
            system, messages, plan = built
            raw = await client.complete(
                model=model, system=system, messages=messages, max_tokens=max_tokens
            )
            out[str(fid)] = {
                "raw": raw,
                "n_unclustered": len(plan.unclustered_story_ids),
                "n_existing_events": len(plan.existing_event_ids),
                # n (1-based) -> story_ids[n-1]; existing_idx (1-based) -> existing_event_ids[idx-1]
                "story_ids": [str(sid) for sid in plan.unclustered_story_ids],
                "existing_event_ids": [str(eid) for eid in plan.existing_event_ids],
            }
    return out


async def reset_cluster_output() -> None:
    """Expose the full corpus: delete event_story + events so every linked story becomes
    unclustered. Stories stay link_status='linked'. Run ONCE before the A/B (not between runs —
    run_model is read-only, so the corpus is identical for both models without re-resetting)."""
    async with SessionLocal() as s:
        await s.execute(delete(EventStory))
        await s.execute(delete(Event))
        await s.commit()


def _parse_film_decisions(film_data: dict) -> dict:
    """Parse one film's raw LLM output into **story_id-keyed** decisions (aligned across runs by
    identity, not positional n). Mirrors apply_cluster_decisions' first-wins story dedup.

    Returns:
      - events_created: int (new-event groups with >=1 story)
      - decision: {story_id: ("attach", existing_event_id) | ("new", group_idx)}
      - groups: {group_idx: set[story_id]}  (new-event groups only)
      - classifications: {group_idx: (type, confidence)}
    """
    raw: str = film_data.get("raw", "")
    story_ids: list[str] = film_data.get("story_ids", [])
    existing_ids: list[str] = film_data.get("existing_event_ids", [])
    empty = {"events_created": 0, "decision": {}, "groups": {}, "classifications": {}}

    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return empty
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return empty

    events_created = 0
    decision: dict[str, tuple[str, object]] = {}
    groups: dict[int, set[str]] = {}
    classifications: dict[int, tuple[str, str]] = {}
    assigned: set[str] = set()

    for gidx, group in enumerate(data.get("events", [])):
        sids: list[str] = []
        for n in group.get("stories") or []:
            if isinstance(n, int) and 1 <= n <= len(story_ids):
                sid = story_ids[n - 1]
                if sid not in assigned:
                    sids.append(sid)
                    assigned.add(sid)
        if not sids:
            continue
        existing_idx = group.get("existing")
        if isinstance(existing_idx, int) and 1 <= existing_idx <= len(existing_ids):
            target = existing_ids[existing_idx - 1]
            for sid in sids:
                decision[sid] = ("attach", target)
        else:
            for sid in sids:
                decision[sid] = ("new", gidx)
            groups[gidx] = set(sids)
            etype, conf = group.get("type", ""), group.get("confidence", "")
            if etype and conf:
                classifications[gidx] = (etype, conf)
            events_created += 1

    return {
        "events_created": events_created,
        "decision": decision,
        "groups": groups,
        "classifications": classifications,
    }


def diff(a_path: str, b_path: str) -> None:
    a: dict[str, dict] = json.load(open(a_path))
    b: dict[str, dict] = json.load(open(b_path))

    film_ids = sorted(set(a) | set(b))
    if not film_ids:
        print("No films in either file.")
        return

    parsed_a = {fid: _parse_film_decisions(a[fid]) for fid in a}
    parsed_b = {fid: _parse_film_decisions(b[fid]) for fid in b}
    both = [fid for fid in film_ids if fid in parsed_a and fid in parsed_b]

    n_stories = sum(len(parsed_a[fid]["decision"]) for fid in parsed_a)
    print(f"Corpus: {len(film_ids)} films, {n_stories} story-decisions (A side).\n")

    # --- Rule 1: Granularity ±1/film >=90%, total within ±10% ---
    within_1 = []
    total_a = total_b = 0
    for fid in film_ids:
        ea = parsed_a.get(fid, {}).get("events_created", 0)
        eb = parsed_b.get(fid, {}).get("events_created", 0)
        total_a += ea
        total_b += eb
        within_1.append(abs(ea - eb) <= 1)
    pct_within_1 = sum(within_1) / len(within_1) if within_1 else 1.0
    total_within_10 = abs(total_a - total_b) / max(total_a, 1) <= 0.10
    rule1 = pct_within_1 >= 0.90 and total_within_10
    print(
        f"Rule 1 (granularity ±1/film ≥90%, total ±10%): {'PASS' if rule1 else 'FAIL'} "
        f"[per-film {pct_within_1:.1%}, total a={total_a} b={total_b} "
        f"{'within 10%' if total_within_10 else 'EXCEEDS 10%'}]"
    )

    # --- Rule 2: Per-story decision agreement >=90% (aligned by story_id) ---
    agree = total = 0
    for fid in both:
        da, db_ = parsed_a[fid]["decision"], parsed_b[fid]["decision"]
        for sid in set(da) | set(db_):
            total += 1
            ad, bd = da.get(sid), db_.get(sid)
            if ad is None or bd is None:
                continue
            # both "new" -> agree (group indices are per-film-local, not comparable);
            # both "attach" -> require the same target event.
            if ad[0] == bd[0] == "new":
                agree += 1
            elif ad[0] == bd[0] == "attach" and ad[1] == bd[1]:
                agree += 1
    pct_agree = agree / total if total else 1.0
    rule2 = pct_agree >= 0.90
    print(
        f"Rule 2 (per-story attach/new agreement ≥90%): {'PASS' if rule2 else 'FAIL'} "
        f"[{agree}/{total} = {pct_agree:.1%}]"
    )

    # --- Rule 3: No catastrophic merges (B groups stories from >=2 distinct A groups) ---
    catastrophic: list[dict] = []
    for fid in both:
        da, db_ = parsed_a[fid], parsed_b[fid]
        a_group_of = {sid: tgt for sid, (kind, tgt) in da["decision"].items() if kind == "new"}
        for b_gidx, b_sids in db_["groups"].items():
            a_groups = {a_group_of[sid] for sid in b_sids if sid in a_group_of}
            if len(a_groups) >= 2:
                catastrophic.append(
                    {
                        "film_id": fid,
                        "b_group": b_gidx,
                        "stories": sorted(b_sids),
                        "a_groups_merged": sorted(a_groups),
                    }
                )
    rule3 = len(catastrophic) == 0
    print(
        f"Rule 3 (no catastrophic merges): {'PASS' if rule3 else 'FAIL'} "
        f"[{len(catastrophic)} merge(s)]"
    )
    for item in catastrophic:
        print(
            f"    film={item['film_id']} b_group={item['b_group']} "
            f"a_groups={item['a_groups_merged']} stories={item['stories']}"
        )

    # --- Rule 4: Type/confidence match >=85% on overlapping new events ---
    tc_agree = tc_total = 0
    for fid in both:
        da, db_ = parsed_a[fid], parsed_b[fid]
        for b_gidx, b_sids in db_["groups"].items():
            if b_gidx not in db_["classifications"]:
                continue
            best_a, best_overlap = None, 0
            for a_gidx, a_sids in da["groups"].items():
                overlap = len(b_sids & a_sids)
                if overlap > best_overlap:
                    best_overlap, best_a = overlap, a_gidx
            if best_a is None or best_a not in da["classifications"]:
                continue
            tc_total += 1
            if da["classifications"][best_a] == db_["classifications"][b_gidx]:
                tc_agree += 1
    pct_tc = tc_agree / tc_total if tc_total else 1.0
    rule4 = pct_tc >= 0.85
    print(
        f"Rule 4 (type/confidence ≥85% on matched new events): {'PASS' if rule4 else 'FAIL'} "
        f"[{tc_agree}/{tc_total} = {pct_tc:.1%}]"
    )

    # --- Supplementary: pairwise co-grouping agreement (Rand-style partition agreement) ---
    # For every pair of stories present in BOTH runs, do the two models agree on whether the
    # pair is co-grouped? This is the most direct measure of clustering agreement and does not
    # depend on group-index labels.
    pair_agree = pair_total = 0
    for fid in both:
        da, db_ = parsed_a[fid]["decision"], parsed_b[fid]["decision"]
        common = sorted(set(da) & set(db_))
        for s1, s2 in itertools.combinations(common, 2):
            pair_total += 1
            if (da[s1] == da[s2]) == (db_[s1] == db_[s2]):
                pair_agree += 1
    pct_pairs = pair_agree / pair_total if pair_total else 1.0
    print(
        f"Supplementary (pairwise co-grouping agreement): "
        f"{pair_agree}/{pair_total} = {pct_pairs:.1%}"
    )

    all_pass = rule1 and rule2 and rule3 and rule4
    verdict = "PASS — no material regression" if all_pass else "FAIL — regression"
    print()
    print(f"Gate (Rules 1-4): {verdict}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model")
    p.add_argument("--out")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--diff", nargs=2, metavar=("SONNET", "HAIKU"))
    p.add_argument("--attach-limit", type=int, default=25)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--limit", type=int, default=None, help="cap number of films (smoke runs)")
    a = p.parse_args()
    if a.reset:
        asyncio.run(reset_cluster_output())
        print("reset: deleted all events; linked stories are now unclustered")
    elif a.diff:
        diff(*a.diff)
    elif a.model and a.out:
        result = asyncio.run(run_model(a.model, a.attach_limit, a.max_tokens, a.limit))
        json.dump(result, open(a.out, "w"), indent=2)
        print(f"wrote {a.out}: {len(result)} films")
    else:
        p.error("need --model+--out, or --reset, or --diff A B")


if __name__ == "__main__":
    main()
