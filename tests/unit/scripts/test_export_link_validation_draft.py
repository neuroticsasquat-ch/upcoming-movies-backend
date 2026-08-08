"""The draft sampler's pure half: how a target row count is split across sources.

The draft is no longer "every story in the window" — at 98k retained stories that is four
orders of magnitude past what anyone will hand-review. It is a stratified sample, and the
stratification is what keeps the labeled set's source mix production-like instead of
whatever the LIMIT happened to catch.
"""

from scripts.export_link_validation_draft import source_quotas


def test_quotas_are_proportional_to_the_source_mix():
    quotas = source_quotas({"a": 800, "b": 200}, target=100)

    assert quotas == {"a": 80, "b": 20}


def test_quotas_sum_to_the_target_when_the_split_is_not_exact():
    """Largest-remainder, so the rounding residue is allocated rather than dropped."""
    quotas = source_quotas({"a": 10, "b": 10, "c": 10}, target=10)

    assert sum(quotas.values()) == 10
    assert sorted(quotas.values()) == [3, 3, 4]


def test_a_source_is_never_asked_for_more_rows_than_it_has():
    quotas = source_quotas({"big": 1000, "tiny": 3}, target=1003)

    assert quotas == {"big": 1000, "tiny": 3}


def test_a_target_at_or_above_the_corpus_takes_everything():
    quotas = source_quotas({"a": 5, "b": 7}, target=99)

    assert quotas == {"a": 5, "b": 7}


def test_ties_are_broken_deterministically_by_source_name():
    """Two runs of the same corpus must draft the same rows, or the labeled set stops
    being reproducible from its recipe."""
    counts = {"z": 10, "a": 10, "m": 10}

    assert source_quotas(counts, target=10) == source_quotas(counts, target=10)
    assert source_quotas(counts, target=10)["a"] == 4  # the residue goes to the first name


def test_an_empty_corpus_yields_no_quotas():
    assert source_quotas({}, target=50) == {}


def test_a_zero_target_asks_for_nothing():
    assert source_quotas({"a": 10}, target=0) == {"a": 0}


def test_a_source_below_one_proportional_row_is_dropped_rather_than_rounded_up():
    """A 1-in-2000 feed is worth 0.05 rows at this target, and largest-remainder leaves it
    at zero. Recorded because it is the sampler's one silent exclusion: proportional
    stratification does not guarantee every source appears, only that those which do appear
    are in proportion. Raise the target if a rare feed must be represented."""
    quotas = source_quotas({"huge": 1999, "rare": 1}, target=100)

    assert sum(quotas.values()) == 100
    assert quotas == {"huge": 100, "rare": 0}
