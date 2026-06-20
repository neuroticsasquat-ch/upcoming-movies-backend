"""Pure scoring functions for the linking/clustering accuracy baseline. No I/O — the
runner feeds these (predicted, expected) pairs and predicted clusters."""

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


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
