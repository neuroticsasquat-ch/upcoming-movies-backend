"""Pure ingest-time skip rules for TMDB films. Kept free of DB/HTTP so the policy is
unit-testable in isolation and lives in one place."""

from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


def classify_skip(
    details: TMDBMovieDetails,
    *,
    excluded_statuses: frozenset[str],
    min_runtime: int,
) -> str | None:
    """Return a reason this film should NOT be ingested, or None to keep it.

    Reasons:
      - "excluded_status": status is in the excluded set (e.g. Released, Canceled).
      - "short": a KNOWN runtime below min_runtime. A runtime of 0 or None means
        unknown/unfinished and is kept. min_runtime=0 disables the rule.

    The status check takes precedence over the runtime check.
    """
    if details.status in excluded_statuses:
        return "excluded_status"
    if details.runtime is not None and 0 < details.runtime < min_runtime:
        return "short"
    return None
