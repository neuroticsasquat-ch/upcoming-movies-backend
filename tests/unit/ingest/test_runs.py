"""NEU-986/NEU-987: the rule that decides whether a stage's tally is a total outage.
NEU-988: the two halves of that rule are named by `StageKind`, classified once in
`STAGE_KINDS` rather than restated at each construction site."""

import pytest

from upmovies.ingest.runs import STAGE_KINDS, StageCounts, StageKind, total_failure_error


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
    """The lossy rule is unchanged from NEU-986: a failed item is not retried
    unconditionally, so even a backlog of one is worth failing the run over."""
    counts = StageCounts(processed=processed, failed=failed)
    assert counts.total_failure(StageKind.LOSSY) is expected


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
    counts = StageCounts(processed=processed, failed=failed)
    assert counts.total_failure(StageKind.SELF_HEALING) is expected


def test_counts_default_to_a_clean_empty_stage():
    assert StageCounts() == StageCounts(processed=0, failed=0)
    assert StageCounts().total_failure(StageKind.LOSSY) is False
    assert StageCounts().total_failure(StageKind.SELF_HEALING) is False


def test_both_kinds_are_named_neither_is_the_absence_of_the_other():
    """NEU-988: `lossy` is a value, not `False`. The two thresholds differ by exactly the
    denominator NEU-987 introduced."""
    assert StageKind.LOSSY.failure_floor == 1
    assert StageKind.SELF_HEALING.failure_floor == 2
    assert {k.value for k in StageKind} == {"lossy", "self_healing"}


def test_every_guarded_stage_is_classified():
    """The guarded stages, and only those. `source_judge` is a link sub-stage that keeps no
    counters and is not guarded (CONTEXT.md), so it is deliberately absent — the loud
    lookup below is what forces that decision if it ever grows counters."""
    assert STAGE_KINDS == {
        "link": StageKind.LOSSY,
        "cluster": StageKind.SELF_HEALING,
        "summarize": StageKind.SELF_HEALING,
    }


def test_unclassified_stage_fails_loudly_rather_than_defaulting():
    """NEU-988: a stage added later must not pick up a rule by omission."""
    with pytest.raises(ValueError, match="source_judge"):
        total_failure_error(source_judge=StageCounts(processed=0, failed=1))


def test_total_failure_error_names_only_the_stages_that_failed_totally():
    error = total_failure_error(
        link=StageCounts(processed=0, failed=1),
        cluster=StageCounts(processed=0, failed=1),
    )
    assert error is not None
    assert "link stage" in error
    assert "cluster stage" not in error  # self-healing, single failure — below the bar


def test_total_failure_error_is_none_when_no_stage_failed_totally():
    assert (
        total_failure_error(
            link=StageCounts(processed=3, failed=1),
            cluster=StageCounts(processed=0, failed=1),
        )
        is None
    )
