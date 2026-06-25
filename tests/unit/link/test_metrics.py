from upmovies.link.metrics import (
    cluster_purity,
    compute_cluster_metrics,
    compute_link_metrics,
    compute_news_value_metrics,
)


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


def test_news_value_all_real_news_linked():
    m = compute_news_value_metrics([(True, True, None), (True, None, None)])
    assert (m.true_positives, m.false_positives, m.false_negatives) == (2, 0, 0)
    assert m.precision == 1.0 and m.recall == 1.0
    assert m.leaks_by_category == {}


def test_news_value_linked_excluded_is_a_leak():
    m = compute_news_value_metrics([(True, False, "reaction")])
    assert m.false_positives == 1 and m.precision == 0.0
    assert m.leaks_by_category == {"reaction": 1}


def test_news_value_leak_without_category_buckets_other():
    m = compute_news_value_metrics([(True, False, None)])
    assert m.leaks_by_category == {"other": 1}


def test_news_value_dropped_excluded_is_true_negative():
    m = compute_news_value_metrics([(False, False, "roundup")])
    assert m.true_negatives == 1 and not m.leaks_by_category


def test_news_value_dropped_real_news_is_false_negative():
    m = compute_news_value_metrics([(False, True, None)])
    assert m.false_negatives == 1 and m.recall == 0.0


def test_cluster_metrics_perfect():
    pred = [{"a", "b"}, {"c"}]
    gold = {"a": "g1", "b": "g1", "c": "g2"}
    m = compute_cluster_metrics(pred, gold)
    assert m.purity == 1.0
    assert m.pairwise_precision == 1.0
    assert m.pairwise_recall == 1.0
    assert m.pairwise_f1 == 1.0
    assert m.n_items == 3


def test_cluster_metrics_over_merge_drops_precision():
    pred = [{"a", "b"}]  # model merged two distinct beats
    gold = {"a": "g1", "b": "g2"}
    m = compute_cluster_metrics(pred, gold)
    assert m.pairwise_precision == 0.0  # 1 predicted pair, 0 gold pairs
    assert m.pairwise_recall == 1.0  # no gold pair to miss
    assert m.purity == 0.5
    assert m.n_predicted_pairs == 1 and m.n_gold_pairs == 0


def test_cluster_metrics_over_split_drops_recall():
    pred = [{"a"}, {"b"}]  # model fragmented one beat → duplicate events
    gold = {"a": "g1", "b": "g1"}
    m = compute_cluster_metrics(pred, gold)
    assert m.pairwise_recall == 0.0  # missed the one gold pair
    assert m.pairwise_precision == 1.0  # no predicted pair → none false
    assert m.purity == 1.0  # purity is blind to splitting
    assert m.n_predicted_pairs == 0 and m.n_gold_pairs == 1
