"""Read-only probe: what would an entity-scoped undated sweep admit? (NEU-1073)

Spec: `upcoming-movies/docs/specs/backlotter-undated-film-discovery-project-spec.md` §4.3.

The undated-discovery admission bar has one constant nobody can guess — the corroboration
threshold, the number of distinct seed-grade attachments a film needs before it is real
enough to admit (§4.2). This script produces the table that sets it: every candidate the
sweep would reach, one row each, so the threshold and the directors-tranche flag are a
`GROUP BY` over its output rather than an argument.

It **writes nothing**. Two reads come out of `catalog` (the seed people, the films we
already hold) and everything after that is TMDB and a CSV, so it is safely re-runnable
and carries no `ingest_run` row.

What it does, per §3.2 and §4.1:

1. Seed people — anyone holding a **director**, **writer** (`Writer`/`Screenplay`) or
   **top-5 billed cast** credit on an active film (~7,519 today).
2. One `/person/{id}/movie_credits` each — a whole filmography per request, undated
   entries included.
3. Keep undated entries where the seed person's role **on that candidate film** is itself
   seed-grade, and which are not already in `catalog.film`.
4. One `/movie/{id}` per distinct survivor — the credits list carries no `status`.
5. Drop anything the details payload reveals to be dated after all, drop
   `Released`/`Canceled` via `classify_skip`, and write the rest.

Runtime is dominated by step 2: ~7,519 requests at the configured 40 req/10 s is ~31
minutes, plus one request per distinct candidate. Run it in the container:

    task shell
    python scripts/probe_undated_candidates.py --out /tmp/undated_probe.csv
    python scripts/probe_undated_candidates.py --out /tmp/smoke.csv --limit 50  # smoke run

Rows are flushed as they are judged, so an interrupted run still leaves usable output.
The seed-set and role rules moved to `upmovies.ingest.sweep.seeds` when the sweep landed
(NEU-1077) and are imported from there: a probe measuring a different definition of seed
grade than the sweep applies would be a measurement of nothing.
"""

import argparse
import asyncio
import csv
import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx

from upmovies.catalog.seed_grade import ROLE_ORDER
from upmovies.config import get_settings
from upmovies.db import SessionLocal
from upmovies.ingest.sweep.seeds import (
    CandidateTally,
    SeedAttachment,
    SessionFactory,
    load_known_film_tmdb_ids,
    load_seed_person_ids,
    seed_attachments,
    tally_attachments,
)
from upmovies.ingest.tmdb.client import TMDBClient
from upmovies.ingest.tmdb.filters import classify_skip
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

log = logging.getLogger("probe_undated_candidates")

REPORT_COLUMNS: tuple[str, ...] = (
    "tmdb_id",
    "title",
    "status",
    "original_language",
    "popularity",
    "seed_attachment_count",
    "seed_roles_matched",
    "runtime",
)


@dataclass
class ProbeSummary:
    seed_people: int = 0
    person_fetch_failures: int = 0
    candidates_found: int = 0
    skipped_already_known: int = 0
    skipped_dated_on_details: int = 0
    skipped_excluded_status: int = 0
    details_fetch_failures: int = 0
    candidates_reported: int = 0


def report_row(tally: CandidateTally, details: TMDBMovieDetails) -> dict[str, object]:
    """One report row. The columns are a contract — M4's tuning reads them directly."""
    return {
        "tmdb_id": tally.tmdb_id,
        "title": details.title,
        "status": details.status,
        "original_language": details.original_language,
        "popularity": details.popularity,
        "seed_attachment_count": tally.seed_attachment_count,
        "seed_roles_matched": "|".join(r for r in ROLE_ORDER if r in tally.roles),
        "runtime": details.runtime,
    }


async def run_probe(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    out_path: Path,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    limit: int | None = None,
    log_every: int = 250,
) -> ProbeSummary:
    """Sweep the seed set, judge what it reaches, write the report. Reads only."""
    summary = ProbeSummary()

    async with session_factory() as session:
        seed_ids = await load_seed_person_ids(
            session,
            today=today,
            excluded_statuses=excluded_statuses,
            dormancy_days=dormancy_days,
        )
        known_tmdb_ids = await load_known_film_tmdb_ids(session)
    if limit is not None:
        seed_ids = seed_ids[:limit]
    summary.seed_people = len(seed_ids)
    log.info("sweeping %d seed people against %d known films", len(seed_ids), len(known_tmdb_ids))

    attachments: list[SeedAttachment] = []
    for i, person_id in enumerate(seed_ids, start=1):
        try:
            credits = await client.person_movie_credits(person_id)
        except httpx.HTTPError as e:
            # One unreachable person must not cost the other 7,518 — nor one malformed
            # payload, which is why the broad catch below mirrors the production loop.
            summary.person_fetch_failures += 1
            log.warning("person %d credits failed: %s", person_id, e)
            continue
        except Exception:
            summary.person_fetch_failures += 1
            log.exception("unexpected error reading credits for person %d", person_id)
            continue
        attachments.extend(seed_attachments(person_id, credits))
        if i % log_every == 0:
            log.info("%d/%d seed people, %d attachments", i, len(seed_ids), len(attachments))

    tallies = tally_attachments(attachments)
    summary.candidates_found = len(tallies)
    unknown = [t for t in tallies.values() if t.tmdb_id not in known_tmdb_ids]
    summary.skipped_already_known = len(tallies) - len(unknown)
    # Strongest corroboration first, so an interrupted run leaves the interesting rows.
    unknown.sort(key=lambda t: (-t.seed_attachment_count, t.tmdb_id))
    log.info("%d candidates, %d new — fetching details", len(tallies), len(unknown))

    with out_path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(REPORT_COLUMNS))
        writer.writeheader()
        for i, tally in enumerate(unknown, start=1):
            try:
                details = await client.movie_details(tally.tmdb_id)
            except httpx.HTTPError as e:
                summary.details_fetch_failures += 1
                log.warning("details for %d failed: %s", tally.tmdb_id, e)
                continue
            except Exception:
                summary.details_fetch_failures += 1
                log.exception("unexpected error judging film %d", tally.tmdb_id)
                continue
            # A credits entry can omit a release date that `/movie/{id}` does carry, and a
            # dated film is not a candidate. Judging on the details payload keeps a stale
            # summary from inflating the very distribution the threshold is read off.
            if details.release_date is not None:
                summary.skipped_dated_on_details += 1
                continue
            # min_runtime=0 disables the short-film rule: undated films rarely have a
            # known runtime, and runtime is a reported column here, not a filter.
            if classify_skip(details, excluded_statuses=excluded_statuses, min_runtime=0):
                summary.skipped_excluded_status += 1
                continue
            writer.writerow(report_row(tally, details))
            fh.flush()
            summary.candidates_reported += 1
            if i % log_every == 0:
                log.info("%d/%d candidates judged", i, len(unknown))

    return summary


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True, help="CSV report destination")
    parser.add_argument(
        "--limit", type=int, default=None, help="sweep only the first N seed people (smoke run)"
    )
    return parser.parse_args(argv)


async def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = _parse_args(argv)
    settings = get_settings()
    async with TMDBClient(
        base_url=settings.tmdb_base_url,
        api_key=settings.tmdb_api_key,
        rate_calls=settings.tmdb_rate_limit_requests,
        rate_window=settings.tmdb_rate_limit_window_seconds,
        retry_max_attempts=settings.tmdb_retry_max_attempts,
    ) as client:
        summary = await run_probe(
            session_factory=SessionLocal,
            client=client,
            out_path=args.out,
            today=date.today(),
            excluded_statuses=settings.tmdb_excluded_statuses,
            dormancy_days=settings.sweep_dormancy_days,
            limit=args.limit,
        )
    log.info("wrote %s: %s", args.out, summary)


if __name__ == "__main__":
    asyncio.run(main())
