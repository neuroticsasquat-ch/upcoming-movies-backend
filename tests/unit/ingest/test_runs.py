"""NEU-986: the rule that decides whether a stage's tally is a total outage."""

import pytest

from upmovies.ingest.runs import StageCounts


@pytest.mark.parametrize(
    ("processed", "failed", "expected"),
    [
        (0, 0, False),  # nothing to do — an idempotent no-op, not an outage
        (0, 1, True),  # the degenerate case NEU-743 guarded against
        (0, 377, True),
        (1, 0, False),
        (1, 1, False),  # partial failure — per-item isolation still applies
        (1, 999, False),  # deliberately narrow: no proportional threshold
    ],
)
def test_total_failure_is_zero_processed_with_at_least_one_failure(processed, failed, expected):
    assert StageCounts(processed=processed, failed=failed).total_failure is expected


def test_counts_default_to_a_clean_empty_stage():
    assert StageCounts() == StageCounts(processed=0, failed=0)
    assert StageCounts().total_failure is False
