"""The fit score is arithmetic over verified verdicts. The model never produces a number."""

from collections.abc import Iterable

STATUS_VALUE = {"met": 1.0, "partial": 0.5, "missing": 0.0}
KIND_WEIGHT = {"must": 2, "nice": 1}


def fit_score(verdicts: Iterable[tuple[str, str]]) -> int | None:
    """0-100 from (kind, status) pairs: must-haves weigh double, a partial match counts half.
    None when there is nothing to score."""
    total = earned = 0.0
    for kind, status in verdicts:
        weight = KIND_WEIGHT[kind]
        total += weight
        earned += weight * STATUS_VALUE[status]
    if total == 0:
        return None
    return round(100 * earned / total)
