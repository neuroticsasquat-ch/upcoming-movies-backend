"""Diff the cluster stage's decisions between two models on a fixed local corpus.

Precondition: a real tmdb+feeds+LINK ingest has populated `news.story` with `linked`,
unclustered stories (link_status='linked', film_id set, no event_story row yet). Then,
in the container, run ONCE per model (resetting cluster output between runs — see --reset):

    task shell
    python scripts/eval_cluster_diff.py --model claude-sonnet-4-6 --out sonnet.json
    python scripts/eval_cluster_diff.py --reset                       # clear cluster output
    python scripts/eval_cluster_diff.py --model claude-haiku-4-5  --out haiku.json
    python scripts/eval_cluster_diff.py --diff sonnet.json haiku.json # print the diff report

Records nothing to prod run tables; it reuses build_cluster_request/apply_cluster_decisions
against the live DB so the decisions are faithful to production."""

import argparse
import asyncio
import json
from uuid import UUID

from sqlalchemy import delete, exists, select

from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.link.cluster import apply_cluster_decisions, build_cluster_request
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


async def run_model(model: str, attach_limit: int, max_tokens: int) -> dict:
    """Drive the cluster stage per film with `model`; return per-film decisions for diffing.
    Mutates the DB exactly as production would (creates events / event_story rows)."""
    settings = get_settings()
    out: dict[str, dict] = {}
    async with AnthropicClient(api_key=settings.anthropic_api_key) as client, SessionLocal() as s:
        film_ids = await _unclustered_film_ids(s)
        for fid in film_ids:
            built = await build_cluster_request(s, film_id=fid, attach_limit=attach_limit)
            if built is None:
                continue
            system, messages, plan = built
            raw = await client.complete(
                model=model, system=system, messages=messages, max_tokens=max_tokens
            )
            # Snapshot the RAW decision (model output) before applying — that's the diff unit.
            out[str(fid)] = {"raw": raw, "n_unclustered": len(plan.unclustered_story_ids)}
            await apply_cluster_decisions(s, plan=plan, raw=raw)
            await s.commit()
    return out


async def reset_cluster_output() -> None:
    """Undo a run: delete event_story + events so the next model sees the same unclustered
    corpus. Stories stay link_status='linked'. (Stale-stage rejects are NOT restored — keep
    the corpus free of stale beats, or reset from a DB snapshot instead for full fidelity.)"""
    async with SessionLocal() as s:
        await s.execute(delete(EventStory))
        await s.execute(delete(Event))
        await s.commit()


def _parse_film_decisions(film_data: dict) -> dict:
    """Extract per-film decisions from raw LLM output into structured form.

    Returns a dict with:
      - events_created: int (new events, not attaches)
      - attach_decisions: list of (story_n, "attach", existing_idx) | (story_n, "new", group_idx)
      - new_event_classifications: list of (group_idx, type, confidence)
    """
    raw = film_data.get("raw", "")
    # Mirror _extract_json_object from cluster.py — find the outermost JSON object.
    start = raw.find("{")
    end = raw.rfind("}")
    if start == -1 or end == -1 or end < start:
        return {"events_created": 0, "attach_decisions": [], "new_event_classifications": []}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {"events_created": 0, "attach_decisions": [], "new_event_classifications": []}

    events_created = 0
    attach_decisions: list[tuple[int, str, int]] = []
    new_event_classifications: list[tuple[int, str, str]] = []

    for group_idx, group in enumerate(data.get("events", [])):
        existing_idx = group.get("existing")
        stories = [n for n in (group.get("stories") or []) if isinstance(n, int)]
        if isinstance(existing_idx, int):
            for n in stories:
                attach_decisions.append((n, "attach", existing_idx))
        else:
            etype = group.get("type", "")
            conf = group.get("confidence", "")
            for n in stories:
                attach_decisions.append((n, "new", group_idx))
            if etype and conf:
                new_event_classifications.append((group_idx, etype, conf))
            events_created += 1

    return {
        "events_created": events_created,
        "attach_decisions": attach_decisions,
        "new_event_classifications": new_event_classifications,
    }


def diff(a_path: str, b_path: str) -> None:
    a: dict[str, dict] = json.load(open(a_path))
    b: dict[str, dict] = json.load(open(b_path))

    film_ids = sorted(set(a) | set(b))
    if not film_ids:
        print("No films in either file.")
        return

    # --- Rule 1: Granularity ±1/film ≥90%, total within ±10% ---
    per_film_within_1: list[bool] = []
    total_a = 0
    total_b = 0
    for fid in film_ids:
        da = _parse_film_decisions(a[fid]) if fid in a else {"events_created": 0}
        db_ = _parse_film_decisions(b[fid]) if fid in b else {"events_created": 0}
        ea, eb = da["events_created"], db_["events_created"]
        total_a += ea
        total_b += eb
        per_film_within_1.append(abs(ea - eb) <= 1)

    pct_within_1 = sum(per_film_within_1) / len(per_film_within_1) if per_film_within_1 else 1.0
    total_within_10pct = abs(total_a - total_b) / max(total_a, 1) <= 0.10
    rule1_pass = pct_within_1 >= 0.90 and total_within_10pct
    print(
        f"Rule 1 (granularity ±1/film ≥90%, total ±10%): "
        f"{'PASS' if rule1_pass else 'FAIL'} "
        f"[per-film {pct_within_1:.1%}, total a={total_a} b={total_b} "
        f"{'within 10%' if total_within_10pct else 'EXCEEDS 10%'}]"
    )

    # --- Rule 2: Attach/new agreement ≥90% ---
    # For each film, compare each story's decision (attach vs new) by story n.
    # A story n's decision: ("attach", existing_idx) or ("new",).  Target event is included for
    # attach so same-event vs wrong-event disagrees are both caught.
    agree_count = 0
    total_story_count = 0
    for fid in film_ids:
        if fid not in a or fid not in b:
            continue
        da = _parse_film_decisions(a[fid])
        db_ = _parse_film_decisions(b[fid])
        # Build n -> (kind, target) maps
        a_by_n: dict[int, tuple[str, int]] = {
            n: (kind, tgt) for n, kind, tgt in da["attach_decisions"]
        }
        b_by_n: dict[int, tuple[str, int]] = {
            n: (kind, tgt) for n, kind, tgt in db_["attach_decisions"]
        }
        all_ns = set(a_by_n) | set(b_by_n)
        for n in all_ns:
            ad = a_by_n.get(n)
            bd = b_by_n.get(n)
            total_story_count += 1
            if ad is None or bd is None:
                continue  # story missing from one side — counts as disagree (not incremented)
            # For attach decisions, require same kind AND same existing-event target.
            # For new decisions, only require same kind (group indices differ across models).
            if ad[0] == bd[0] == "attach" and ad[1] == bd[1]:
                agree_count += 1
            elif ad[0] == bd[0] == "new":
                agree_count += 1

    pct_agree = agree_count / total_story_count if total_story_count else 1.0
    rule2_pass = pct_agree >= 0.90
    print(
        f"Rule 2 (attach/new agreement ≥90%): "
        f"{'PASS' if rule2_pass else 'FAIL'} "
        f"[{agree_count}/{total_story_count} = {pct_agree:.1%}]"
    )

    # --- Rule 3: No catastrophic merges (B collapses two A events into one) ---
    # A catastrophic merge happens when B groups stories from two distinct A groups into one
    # B group. We detect this by checking, for each B "new" group, whether the A decisions
    # for its stories span more than one distinct A group index.
    catastrophic: list[dict] = []
    for fid in film_ids:
        if fid not in a or fid not in b:
            continue
        da = _parse_film_decisions(a[fid])
        db_ = _parse_film_decisions(b[fid])
        a_by_n = {n: (kind, tgt) for n, kind, tgt in da["attach_decisions"]}
        # Group B's new-event assignments: b_group_idx -> set of story ns
        b_new_groups: dict[int, list[int]] = {}
        for n, kind, tgt in db_["attach_decisions"]:
            if kind == "new":
                b_new_groups.setdefault(tgt, []).append(n)
        for b_gidx, ns in b_new_groups.items():
            a_groups = {a_by_n[n][1] for n in ns if n in a_by_n and a_by_n[n][0] == "new"}
            if len(a_groups) >= 2:
                catastrophic.append(
                    {
                        "film_id": fid,
                        "b_group": b_gidx,
                        "stories": ns,
                        "a_groups_merged": sorted(a_groups),
                    }
                )

    rule3_pass = len(catastrophic) == 0
    print(
        f"Rule 3 (no catastrophic merges): "
        f"{'PASS' if rule3_pass else 'FAIL'} "
        f"[{len(catastrophic)} merge(s)]"
    )
    if catastrophic:
        print("  Disagreement list (eyeball for severity):")
        for item in catastrophic:
            print(
                f"    film={item['film_id']} b_group={item['b_group']} "
                f"stories={item['stories']} a_groups={item['a_groups_merged']}"
            )

    # --- Rule 4: Type/confidence match ≥85% on new events both models created ---
    # For each film, match B's new-event groups to A's by story-set overlap (best match),
    # then compare type and confidence on matched pairs.
    type_conf_agree = 0
    type_conf_total = 0
    for fid in film_ids:
        if fid not in a or fid not in b:
            continue
        da = _parse_film_decisions(a[fid])
        db_ = _parse_film_decisions(b[fid])

        # Build group_idx -> {story_ns} for "new" decisions on each side.
        def _new_groups(decisions: list[tuple[int, str, int]]) -> dict[int, set[int]]:
            g: dict[int, set[int]] = {}
            for n, kind, tgt in decisions:
                if kind == "new":
                    g.setdefault(tgt, set()).add(n)
            return g

        a_groups = _new_groups(da["attach_decisions"])
        b_groups = _new_groups(db_["attach_decisions"])
        a_cls: dict[int, tuple[str, str]] = {
            gidx: (etype, conf) for gidx, etype, conf in da["new_event_classifications"]
        }
        b_cls: dict[int, tuple[str, str]] = {
            gidx: (etype, conf) for gidx, etype, conf in db_["new_event_classifications"]
        }

        # Match each B new-group to the A new-group with the highest story overlap.
        for b_gidx, b_ns in b_groups.items():
            if b_gidx not in b_cls:
                continue
            best_a_gidx: int | None = None
            best_overlap = 0
            for a_gidx, a_ns in a_groups.items():
                overlap = len(b_ns & a_ns)
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_a_gidx = a_gidx
            if best_a_gidx is None or best_a_gidx not in a_cls:
                continue
            type_conf_total += 1
            if a_cls[best_a_gidx] == b_cls[b_gidx]:
                type_conf_agree += 1

    pct_tc = type_conf_agree / type_conf_total if type_conf_total else 1.0
    rule4_pass = pct_tc >= 0.85
    print(
        f"Rule 4 (type/confidence ≥85% on matched new events): "
        f"{'PASS' if rule4_pass else 'FAIL'} "
        f"[{type_conf_agree}/{type_conf_total} = {pct_tc:.1%}]"
    )

    all_pass = rule1_pass and rule2_pass and rule3_pass and rule4_pass
    print()
    verdict = "PASS — no material regression" if all_pass else "FAIL — regression detected"
    print(f"Overall: {verdict}")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--model")
    p.add_argument("--out")
    p.add_argument("--reset", action="store_true")
    p.add_argument("--diff", nargs=2, metavar=("SONNET", "HAIKU"))
    p.add_argument("--attach-limit", type=int, default=25)
    p.add_argument("--max-tokens", type=int, default=4096)
    a = p.parse_args()
    if a.reset:
        asyncio.run(reset_cluster_output())
    elif a.diff:
        diff(*a.diff)
    elif a.model and a.out:
        result = asyncio.run(run_model(a.model, a.attach_limit, a.max_tokens))
        json.dump(result, open(a.out, "w"), indent=2)
        print(f"wrote {a.out}: {len(result)} films")
    else:
        p.error("need --model+--out, or --reset, or --diff A B")


if __name__ == "__main__":
    main()
