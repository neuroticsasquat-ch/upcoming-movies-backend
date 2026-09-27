from datetime import date, timedelta

import pytest

from upmovies.ingest.tmdb.filters import classify_skip, is_unreleased
from upmovies.ingest.tmdb.schemas import TMDBMovieDetails


def _details(**overrides) -> TMDBMovieDetails:
    base = {"id": 1, "title": "X", "status": "Planned", "runtime": 120}
    base.update(overrides)
    return TMDBMovieDetails(**base)


@pytest.mark.parametrize(
    ("runtime", "min_runtime", "expected"),
    [
        (7, 60, "short"),  # well under the floor
        (59, 60, "short"),  # just under the floor
        (60, 60, None),  # boundary is kept (strict <)
        (0, 60, None),  # 0 = unfinished, kept
        (None, 60, None),  # unknown runtime, kept
        (7, 0, None),  # min_runtime=0 disables the rule
        (200, 60, None),  # long feature, kept
    ],
)
def test_classify_skip_runtime_rule(runtime, min_runtime, expected):
    details = _details(runtime=runtime, status="Planned")
    assert (
        classify_skip(details, excluded_statuses=frozenset(), min_runtime=min_runtime) == expected
    )


def test_excluded_status_is_skipped():
    details = _details(runtime=120, status="Released")
    result = classify_skip(
        details, excluded_statuses=frozenset({"Released", "Canceled"}), min_runtime=60
    )
    assert result == "excluded_status"


def test_excluded_status_takes_precedence_over_short():
    # A short that is ALSO an excluded status reports the status reason first.
    details = _details(runtime=7, status="Canceled")
    result = classify_skip(details, excluded_statuses=frozenset({"Canceled"}), min_runtime=60)
    assert result == "excluded_status"


def test_normal_film_is_kept():
    details = _details(runtime=120, status="Planned")
    assert classify_skip(details, excluded_statuses=frozenset(), min_runtime=60) is None


# --- is_unreleased (NEU-1505, D-1505.2) ---------------------------------------------------------

TODAY = date(2026, 9, 27)


@pytest.mark.parametrize(
    ("release_date", "status", "expected"),
    [
        (None, None, True),  # nothing known: the most upcoming film there is
        (TODAY, "Post Production", True),  # opens today: not yet released
        (TODAY + timedelta(days=365), "Planned", True),
        (TODAY - timedelta(days=1), "Post Production", False),  # the date alone decides
        (TODAY - timedelta(days=1), None, False),
        (None, "Released", False),  # the status alone decides
        (TODAY + timedelta(days=30), "Canceled", False),  # called off, however far out
    ],
)
def test_is_unreleased(release_date, status, expected):
    details = _details(release_date=release_date, status=status)
    assert is_unreleased(details, today=TODAY) is expected
