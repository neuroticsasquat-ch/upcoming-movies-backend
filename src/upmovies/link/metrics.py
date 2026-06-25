"""Pure scoring functions for the linking/clustering accuracy baseline. No I/O — the
runner feeds these (predicted, expected) pairs and predicted clusters."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from itertools import combinations


@dataclass
class LinkMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    f1: float


def compute_link_metrics(pairs: Iterable[tuple[int | None, int | None]]) -> LinkMetrics:
    """Each pair is (predicted_tmdb_id, expected_tmdb_id); None means 'no link'. A wrong
    link counts as both a false positive and a false negative; a correct rejection is a
    true negative (precision/recall are about the linking positives)."""
    tp = fp = fn = tn = 0
    for predicted, expected in pairs:
        if predicted is not None and predicted == expected:
            tp += 1
        elif predicted is not None:  # linked, but wrong (or should have been none)
            fp += 1
            if expected is not None:
                fn += 1
        elif expected is not None:  # should have linked, didn't
            fn += 1
        else:  # both None — correct rejection
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return LinkMetrics(tp, fp, fn, tn, precision, recall, f1)


def cluster_purity(
    clusters: Iterable[set[str]], gold_group_by_key: Mapping[str, str | None]
) -> float:
    """Standard cluster purity: for each predicted cluster, credit its majority gold
    group; sum and divide by the number of clustered items."""
    total = correct = 0
    for cluster in clusters:
        if not cluster:
            continue
        counts = Counter(gold_group_by_key.get(key) for key in cluster)
        total += len(cluster)
        correct += max(counts.values())
    return correct / total if total else 0.0


@dataclass
class NewsValueMetrics:
    true_positives: int
    false_positives: int
    false_negatives: int
    true_negatives: int
    precision: float
    recall: float
    leaks_by_category: dict[str, int]


@dataclass
class ClusterMetrics:
    purity: float
    pairwise_precision: float
    pairwise_recall: float
    pairwise_f1: float
    n_items: int
    n_predicted_pairs: int
    n_gold_pairs: int


def _co_pairs(groups: Iterable[Iterable[str]]) -> set[frozenset[str]]:
    """All unordered same-group key pairs across the given groups."""
    pairs: set[frozenset[str]] = set()
    for group in groups:
        for a, b in combinations(sorted(set(group)), 2):
            pairs.add(frozenset((a, b)))
    return pairs


def compute_cluster_metrics(
    predicted_clusters: Iterable[set[str]],
    gold_group_by_key: Mapping[str, str | None],
) -> ClusterMetrics:
    """Score predicted clusters (sets of story keys) against gold event_group slugs.
    Purity catches over-merging; pairwise recall catches over-splitting (the dedup axis).
    Empty pair-sets score 1.0 by convention (no pairs to get wrong)."""
    clusters = [set(c) for c in predicted_clusters]
    purity = cluster_purity(clusters, gold_group_by_key)

    gold_groups: dict[str, set[str]] = {}
    for key, slug in gold_group_by_key.items():
        if slug is not None:
            gold_groups.setdefault(slug, set()).add(key)

    predicted_pairs = _co_pairs(clusters)
    gold_pairs = _co_pairs(gold_groups.values())
    tp = len(predicted_pairs & gold_pairs)
    pp = len(predicted_pairs)
    gp = len(gold_pairs)
    precision = tp / pp if pp else 1.0
    recall = tp / gp if gp else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    n_items = sum(len(c) for c in clusters)
    return ClusterMetrics(purity, precision, recall, f1, n_items, pp, gp)


def compute_news_value_metrics(
    rows: Iterable[tuple[bool, bool | None, str | None]],
) -> NewsValueMetrics:
    """Score the production-news axis over 'about' rows. Each row is
    (linked, is_production_news, exclusion_category); expected-news is True unless
    is_production_news is explicitly False. precision/recall are about the *kept* (linked)
    stories being real news; leaks_by_category counts excluded rows that still linked."""
    tp = fp = fn = tn = 0
    leaks: Counter[str] = Counter()
    for linked, is_news, category in rows:
        expected_news = is_news is not False
        if linked and expected_news:
            tp += 1
        elif linked:  # linked an excluded story — a leak
            fp += 1
            leaks[category or "other"] += 1
        elif expected_news:  # dropped a real-news story
            fn += 1
        else:  # correctly dropped an excluded story
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    return NewsValueMetrics(tp, fp, fn, tn, precision, recall, dict(leaks))
