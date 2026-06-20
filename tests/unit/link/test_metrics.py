from upmovies.link.metrics import cluster_purity, compute_link_metrics


def test_all_correct_links():
    m = compute_link_metrics([(1, 1), (2, 2)])
    assert (m.true_positives, m.false_positives, m.false_negatives) == (2, 0, 0)
    assert m.precision == 1.0 and m.recall == 1.0 and m.f1 == 1.0


def test_correct_rejections_are_true_negatives():
    m = compute_link_metrics([(None, None), (None, None)])
    assert m.true_negatives == 2
    assert m.precision == 0.0 and m.recall == 0.0 and m.f1 == 0.0


def test_overlink_is_false_positive():
    m = compute_link_metrics([(1, None)])
    assert m.false_positives == 1 and m.false_negatives == 0 and m.precision == 0.0


def test_missed_link_is_false_negative():
    m = compute_link_metrics([(None, 5)])
    assert m.false_negatives == 1 and m.recall == 0.0


def test_wrong_film_is_both_fp_and_fn():
    m = compute_link_metrics([(1, 2)])
    assert m.false_positives == 1 and m.false_negatives == 1


def test_cluster_purity_perfect():
    assert cluster_purity([{"a", "b"}, {"c"}], {"a": "G1", "b": "G1", "c": "G2"}) == 1.0


def test_cluster_purity_mixed():
    assert cluster_purity([{"a", "b", "c"}], {"a": "G1", "b": "G1", "c": "G2"}) == 2 / 3
