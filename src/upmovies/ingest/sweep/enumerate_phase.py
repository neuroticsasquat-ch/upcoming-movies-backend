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
from upmovies.ingest.runs import format_skip_detail, record_progress
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

# Runtime is not one of the admission bar's clauses (§4.1), so `classify_skip` is asked
# about status alone — its short-film rule is disabled rather than tuned. An undated film
# rarely carries a real runtime, and rejecting one for the zero TMDB reports would drop
# precisely the just-announced projects the sweep exists to find.
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
    skipped_below_corroboration: int = 0
    """Reached by fewer than `corroboration_threshold` distinct seed people — the vaporware
    filter (§4.2), and the one skip reason a config change alone can undo."""
    candidate_failures: int = 0
    admitted: int = 0
    withheld: int = 0
    """Reached only at seed grades whose tranche is closed. A statement about the rollout,
    not about the film — which is why it is counted before the corroboration threshold."""
    attachment_histogram: Counter[int] = field(default_factory=Counter)
    """Candidates that cleared *status*, by `seed_attachment_count` — deliberately not
    narrowed by the corroboration threshold or the tranches. This is the distribution the
    M4 tuning ticket reads the threshold off (§4.3), so it has to show what today's
    settings excluded, and it is also the honest answer to "what would opening a tranche
    admit"."""
    aborted: bool = False
    abort_error: str | None = None

    @property
    def skip_counts(self) -> Counter[str]:
        """Everything reached but not admitted, by reason, for the run's `detail` line.

        Zero-valued reasons are dropped rather than reported as `reason=0`: the line is read
        at a glance on `/admin/runs`, and the reasons that fired are the information.
        """
        counts = Counter(
            {
                "already_known": self.skipped_already_known,
                "dated": self.skipped_dated_on_details,
                "status": self.skipped_excluded_status,
                "corroboration": self.skipped_below_corroboration,
                "no_tranche": self.withheld,
            }
        )
        return Counter({reason: n for reason, n in counts.items() if n})


async def run_sweep_enumerate(
    *,
    session_factory: SessionFactory,
    client: TMDBClient,
    run_id: UUID,
    today: date,
    excluded_statuses: frozenset[str],
    dormancy_days: int,
    corroboration_threshold: int,
    tranches: AdmissionTranches,
    failure_threshold: int = 10,
    log_every: int = 250,
) -> EnumerateResult:
    """Enumerate the seed set's undated candidates and admit those clearing the bar whose
    seed grade has an open tranche."""
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
        corroboration_threshold=corroboration_threshold,
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
    corroboration_threshold: int,
    tranches: AdmissionTranches,
    result: EnumerateResult,
    guard: AbortGuard,
    log_every: int,
) -> None:
    """One `/movie/{id}` per candidate — the credits list carries no `status` — then the
    admission bar (§4.1: status, seed-grade role, corroboration) and the tranche gate.

    The full details fetch is what admission writes, not a stub row: `upsert_film` lands the
    credits that make the film contribute its own seed people back on the next sweep (§3.3)
    and the alternative titles the link stage's candidate retrieval matches on."""
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
                # Counted before the threshold rather than after it: this histogram is what
                # the M4 tuning ticket reads the threshold *off* (§4.3), so letting today's
                # value truncate it would hide the evidence that it should be lowered.
                result.attachment_histogram[tally.seed_attachment_count] += 1
                # The tranche gate is asked first, and the order is the reported reason.
                # It is the rollout posture — an operator's deliberate setting — whereas the
                # threshold is a judgement about the film. While a grade is closed nothing
                # is being judged at all, so reporting a threshold that was never consulted
                # would read as a verdict the sweep did not reach.
                if not tranches.admits(tally.roles):
                    result.withheld += 1
                    log.debug(
                        "enumerate: withheld %d (%s, %d seed attachments)",
                        tally.tmdb_id,
                        _roles(tally),
                        tally.seed_attachment_count,
                    )
                elif tally.seed_attachment_count < corroboration_threshold:
                    result.skipped_below_corroboration += 1
                else:
                    async with owned_session(session_factory) as s:
                        await upsert_film(s, details)
                        await record_progress(s, run_id, processed_delta=1)
                        await s.commit()
                    result.admitted += 1
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
        "enumerate: %d admitted, %s (%s), %d person failures, %d candidate failures, "
        "attachments %s",
        result.admitted,
        format_skip_detail(result.skip_counts),
        tranches,
        result.person_failures,
        result.candidate_failures,
        dict(sorted(result.attachment_histogram.items())),
    )
