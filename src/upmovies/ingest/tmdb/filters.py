"""Pure ingest-time skip rules for TMDB films. Kept free of DB/HTTP so the policy is
unit-testable in isolation and lives in one place."""

from datetime import date

from upmovies.ingest.tmdb.schemas import TMDBMovieDetails

UNRELEASED_DEAD_STATUSES = frozenset({"Released", "Canceled"})
"""The statuses that end a film's "unreleased" life whatever its date says. Not
`TMDB_EXCLUDED_STATUSES`: that set is a setting, and this rule is not tunable (NEU-1505)."""


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


def is_unreleased(details: TMDBMovieDetails, *, today: date) -> bool:
    """Whether the film has yet to open, for EF-4's admission exception (NEU-1505, D-1505.2).

    Literal: the primary `release_date` is unknown or on/after `today`, and the status is
    neither `Released` nor `Canceled`. No grace period for a festival premiere, and not the
    365-day alert window either — a film that opened six months ago did not just "attach"
    its director. ADR-0019 decision 4 was written for a director's *next* film entering the
    catalog with the director already on it; this is the predicate that says which films
    those are."""
    if details.status in UNRELEASED_DEAD_STATUSES:
        return False
    return details.release_date is None or details.release_date >= today
