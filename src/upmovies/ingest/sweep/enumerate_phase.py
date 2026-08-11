"""The sweep's enumerate phase: walk the seed people's credits, judge what they reach, and
admit the candidates whose seed grade has an open tranche (spec §3.2, §3.3, §4.1).

The shape follows `run_tmdb_ingest` — commits per item so one failure never rolls back the
others, `record_progress` against the run id, and an abort after N consecutive failures so
a TMDB outage stops the pass rather than burning 7,519 requests on it.

Two deliberate departures from that pipeline:

- **It does not finalize the run.** A sweep is two phases (enumerate, then refresh) sharing
  one `ingest_run` row, so the terminal status is the entrypoint's to write (NEU-1079). An
  abort is *reported* — `EnumerateResult.aborted` — rather than written.
- **`items_processed` counts admissions, not candidates judged.** It means the same thing
  here as on a `tmdb` run: rows written. While every tranche is closed the run row honestly
  reads zero, and what the sweep *would* have admitted is in the returned result and the
  closing log line.
"""

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from uuid import UUID

import httpx

from upmovies.catalog.seed_grade import ROLE_ORDER
from upmovies.ingest.runs import record_progress
from upmovies.ingest.sweep.admission import AdmissionTranches
from upmovies.ingest.sweep.phase import AbortGuard, owned_session
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
from upmovies.ingest.tmdb.upsert import upsert_film

log = logging.getLogger(__name__)

# The admission bar is status alone (§4.1). `classify_skip`'s short-film rule is disabled
# rather than tuned: an undated film rarely carries a real runtime, and rejecting one for
# the zero TMDB reports would drop precisely the just-announced projects the sweep exists
# to find.
_MIN_RUNTIME = 0


@dataclass
class EnumerateResult:
    """What one enumerate phase reached, judged and wrote."""

    seed_people: int = 0
    person_failures: int = 0
    candidates_found: int = 0
    """Distinct undated films the seed set reached, before any of the filters below."""
    skipped_already_known: int = 0
    skipped_dated_on_details: int = 0
    skipped_excluded_status: int = 0
    candidate_failures: int = 0
    admitted: int = 0
    withheld: int = 0
    """Cleared the admission bar but its seed grade has no open tranche — the count that
    says what the sweep would admit if a tranche were opened today."""
    attachment_histogram: Counter[int] = field(default_factory=Counter)
    """Candidates that cleared the bar, by `seed_attachment_count`. The corroboration
    threshold is read off this distribution in M4 (§4.2), so it is collected from the
    first run rather than added when the threshold lands."""
    aborted: bool = False
    abort_error: str | None = None


async def run_sweep_enumerate(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    tranches: AdmissionTranches,
    failure_threshold: int = 10,
    log_every: int = 250,
) -> EnumerateResult:
    """Enumerate the seed set's undated candidates and admit those a tranche allows."""
    result = EnumerateResult()
    guard = AbortGuard(session_factory, run_id, failure_threshold)

    async with owned_session(session_factory) as s:
        seed_ids = await load_seed_person_ids(
            s, today=today, excluded_statuses=excluded_statuses, dormancy_days=dormancy_days
        )
        known_tmdb_ids = await load_known_film_tmdb_ids(s)
    result.seed_people = len(seed_ids)
    log.info("enumerate: %d seed people, %d known films", len(seed_ids), len(known_tmdb_ids))

    attachments = await _collect_attachments(
        client=client, seed_ids=seed_ids, result=result, guard=guard, log_every=log_every
    )
    if result.aborted:
        return result

    tallies = tally_attachments(attachments)
    result.candidates_found = len(tallies)
    unknown = [t for t in tallies.values() if t.tmdb_id not in known_tmdb_ids]
    result.skipped_already_known = len(tallies) - len(unknown)
    # Strongest corroboration first, so an aborted pass has judged the likeliest films.
    unknown.sort(key=lambda t: (-t.seed_attachment_count, t.tmdb_id))
    log.info("enumerate: %d candidates reached, %d new", len(tallies), len(unknown))

    await _judge_candidates(
        session_factory=session_factory,
        client=client,
        run_id=run_id,
        candidates=unknown,
        excluded_statuses=excluded_statuses,
        tranches=tranches,
        result=result,
        guard=guard,
        log_every=log_every,
    )
    _log_outcome(result, tranches)
    return result


async def _collect_attachments(
    *,
    client: TMDBClient,
    seed_ids: list[int],
    result: EnumerateResult,
    guard: AbortGuard,
    log_every: int,
) -> list[SeedAttachment]:
    """One `/person/{id}/movie_credits` per seed person — a whole filmography per request,
    undated entries included."""
    attachments: list[SeedAttachment] = []
    for i, person_id in enumerate(seed_ids, start=1):
        try:
            credits = await client.person_movie_credits(person_id)
            attachments.extend(seed_attachments(person_id, credits))
            guard.succeeded()
            if i % log_every == 0:
                log.info("enumerate: %d/%d seed people", i, len(seed_ids))
            continue
        except httpx.HTTPError as e:
            log.warning("credits for person %d failed: %s", person_id, e)
        except Exception:
            # One malformed payload must not cost the other 7,518 people.
            log.exception("unexpected error reading credits for person %d", person_id)
        result.person_failures += 1
        if await guard.failed():
            _abort(result, f"aborted after {guard.consecutive} consecutive failures")
            return attachments
    return attachments


async def _judge_candidates(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    candidates: list[CandidateTally],
    excluded_statuses: frozenset[str],
    tranches: AdmissionTranches,
    result: EnumerateResult,
    guard: AbortGuard,
    log_every: int,
) -> None:
    """One `/movie/{id}` per candidate — the credits list carries no `status` — then the
    admission bar and the tranche gate."""
    for i, tally in enumerate(candidates, start=1):
        try:
            details = await client.movie_details(tally.tmdb_id)
            # A credits entry can omit a release date that `/movie/{id}` does carry, and a
            # dated film belongs to discover rather than to the sweep.
            if details.release_date is not None:
                result.skipped_dated_on_details += 1
            elif classify_skip(
                details, excluded_statuses=excluded_statuses, min_runtime=_MIN_RUNTIME
            ):
                result.skipped_excluded_status += 1
            else:
                result.attachment_histogram[tally.seed_attachment_count] += 1
                if tranches.admits(tally.roles):
                    async with owned_session(session_factory) as s:
                        await upsert_film(s, details)
                        await record_progress(s, run_id, processed_delta=1)
                        await s.commit()
                    result.admitted += 1
                else:
                    result.withheld += 1
                    log.debug(
                        "enumerate: withheld %d (%s, %d seed attachments)",
                        tally.tmdb_id,
                        _roles(tally),
                        tally.seed_attachment_count,
                    )
            guard.succeeded()
            if i % log_every == 0:
                log.info("enumerate: %d/%d candidates judged", i, len(candidates))
            continue
        except httpx.HTTPError as e:
            log.warning("judging film %d failed: %s", tally.tmdb_id, e)
        except Exception:
            log.exception("unexpected error judging film %d", tally.tmdb_id)
        result.candidate_failures += 1
        if await guard.failed():
            _abort(result, f"aborted after {guard.consecutive} consecutive failures")
            return


def _roles(tally: CandidateTally) -> str:
    return "|".join(r for r in ROLE_ORDER if r in tally.roles)


def _abort(result: EnumerateResult, error: str) -> None:
    result.aborted = True
    result.abort_error = error
    log.error("enumerate: %s", error)


def _log_outcome(result: EnumerateResult, tranches: AdmissionTranches) -> None:
    log.info(
        "enumerate: %d admitted, %d withheld (%s), skipped %d known / %d dated / %d status, "
        "%d person failures, %d candidate failures, attachments %s",
        result.admitted,
        result.withheld,
        tranches,
        result.skipped_already_known,
        result.skipped_dated_on_details,
        result.skipped_excluded_status,
        result.person_failures,
        result.candidate_failures,
        dict(sorted(result.attachment_histogram.items())),
    )
