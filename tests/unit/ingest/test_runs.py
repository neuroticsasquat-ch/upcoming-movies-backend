"""NEU-986/NEU-987: the rule that decides whether a stage's tally is a total outage."""

import pytest

from upmovies.ingest.runs import StageCounts, total_failure_error


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
def test_lossy_stage_fails_on_zero_processed_with_any_failure(processed, failed, expected):
    """The default (lossy) rule is unchanged from NEU-986: a failed item is not retried
    unconditionally, so even a backlog of one is worth failing the run over."""
    assert StageCounts(processed=processed, failed=failed).total_failure is expected


@pytest.mark.parametrize(
    ("processed", "failed", "expected"),
    [
        (0, 0, False),  # nothing to do — still a no-op
        (0, 1, False),  # NEU-987: one bad item on an empty backlog is not an outage
        (0, 2, True),  # two candidates, both failed — a real outage
        (0, 377, True),
        (1, 0, False),
        (1, 1, False),  # partial failure — per-item isolation still applies
        (1, 999, False),
    ],
)
def test_self_healing_stage_needs_more_than_one_failure(processed, failed, expected):
    """NEU-987: a self-healing stage retries a failed item unconditionally on the next run,
    so `failed == 1` is indistinguishable from one pathological item and must not fire."""
    counts = StageCounts(processed=processed, failed=failed, self_healing=True)
    assert counts.total_failure is expected


def test_counts_default_to_a_clean_lossy_empty_stage():
    assert StageCounts() == StageCounts(processed=0, failed=0, self_healing=False)
    assert StageCounts().total_failure is False
    assert StageCounts().self_healing is False


def test_total_failure_error_names_only_the_stages_that_failed_totally():
    error = total_failure_error(
        link=StageCounts(processed=0, failed=1),
        cluster=StageCounts(processed=0, failed=1, self_healing=True),
    )
    assert error is not None
    assert "link stage" in error
    assert "cluster stage" not in error  # self-healing, single failure — below the bar


def test_total_failure_error_is_none_when_no_stage_failed_totally():
    assert (
        total_failure_error(
            link=StageCounts(processed=3, failed=1),
            cluster=StageCounts(processed=0, failed=1, self_healing=True),
        )
        is None
    )
