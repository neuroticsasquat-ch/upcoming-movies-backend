import pytest

from upmovies.ingest.tmdb.filters import classify_skip
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
