"""Pure work-level Embedding weight calibration from labeled score pairs."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Iterable, Mapping, Sequence


@dataclass(frozen=True)
class CalibrationSample:
    illust_id: int
    action: str
    tag_score: float
    semantic_score: float


def normalized_cosine(left: Sequence[float], right: Sequence[float]) -> float:
    """Return production-compatible cosine similarity mapped to 0..1."""

    if not left or len(left) != len(right):
        raise ValueError("Embedding 向量为空或维度不一致")
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = sqrt(sum(value * value for value in left))
    right_norm = sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        raise ValueError("Embedding 向量不能是零向量")
    cosine = max(-1.0, min(1.0, dot / (left_norm * right_norm)))
    return (cosine + 1.0) / 2.0


def blend_score(tag_score: float, semantic_score: float, weight: float) -> float:
    if not 0.0 <= weight <= 1.0:
        raise ValueError("semantic_weight 必须位于 0 到 1 之间")
    return (1.0 - weight) * float(tag_score) + weight * float(semantic_score)


def _pairwise_auc(samples: Sequence[CalibrationSample], scores: Mapping[int, float]) -> float | None:
    positives = [scores[item.illust_id] for item in samples if item.action == "like"]
    negatives = [scores[item.illust_id] for item in samples if item.action == "dislike"]
    if not positives or not negatives:
        return None
    wins = 0.0
    for positive in positives:
        for negative in negatives:
            if positive > negative:
                wins += 1.0
            elif positive == negative:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def _rank_positions(samples: Sequence[CalibrationSample], scores: Mapping[int, float]) -> dict[int, int]:
    ordered = sorted(samples, key=lambda item: (-scores[item.illust_id], item.illust_id))
    return {item.illust_id: index + 1 for index, item in enumerate(ordered)}


def evaluate_embedding_weights(
    samples: Iterable[CalibrationSample],
    candidate_weights: Iterable[float],
    *,
    current_weight: float,
    total_feedback: int | None = None,
    missing: Mapping[str, int] | None = None,
    min_samples: int = 20,
    min_per_class: int = 5,
) -> dict:
    """Compare candidate weights without mutating configuration or runtime state."""

    items = tuple(samples)
    ids = [item.illust_id for item in items]
    if len(ids) != len(set(ids)):
        raise ValueError("Calibration Sample 的 illust_id 必须唯一")
    if any(item.action not in {"like", "dislike"} for item in items):
        raise ValueError("Calibration Sample 只接受 like 或 dislike")
    if min_samples < 1 or min_per_class < 1:
        raise ValueError("最小样本数必须大于零")

    weights = sorted({round(float(weight), 10) for weight in candidate_weights} | {0.0, round(float(current_weight), 10)})
    if any(weight < 0.0 or weight > 1.0 for weight in weights):
        raise ValueError("候选 semantic_weight 必须位于 0 到 1 之间")

    positive_count = sum(item.action == "like" for item in items)
    negative_count = sum(item.action == "dislike" for item in items)
    total = max(len(items), int(total_feedback if total_feedback is not None else len(items)))
    missing_counts = {str(key): int(value) for key, value in (missing or {}).items() if int(value) > 0}

    reasons: list[str] = []
    if len(items) < min_samples:
        reasons.append(f"可用样本 {len(items)} 少于最低要求 {min_samples}")
    if positive_count < min_per_class:
        reasons.append(f"like 样本 {positive_count} 少于最低要求 {min_per_class}")
    if negative_count < min_per_class:
        reasons.append(f"dislike 样本 {negative_count} 少于最低要求 {min_per_class}")

    baseline_scores = {item.illust_id: item.tag_score for item in items}
    baseline_ranks = _rank_positions(items, baseline_scores) if items else {}
    evaluations = []
    for weight in weights:
        scores = {
            item.illust_id: blend_score(item.tag_score, item.semantic_score, weight)
            for item in items
        }
        positives = [scores[item.illust_id] for item in items if item.action == "like"]
        negatives = [scores[item.illust_id] for item in items if item.action == "dislike"]
        ranks = _rank_positions(items, scores) if items else {}
        rank_movement = (
            sum(abs(ranks[item_id] - baseline_ranks[item_id]) for item_id in ranks) / len(ranks)
            if ranks else 0.0
        )
        positive_mean = sum(positives) / len(positives) if positives else None
        negative_mean = sum(negatives) / len(negatives) if negatives else None
        evaluations.append({
            "weight": weight,
            "auc": _pairwise_auc(items, scores),
            "positive_mean": positive_mean,
            "negative_mean": negative_mean,
            "score_separation": (
                positive_mean - negative_mean
                if positive_mean is not None and negative_mean is not None else None
            ),
            "mean_rank_movement": rank_movement,
        })

    sufficient = not reasons
    recommended_weight = None
    if sufficient:
        best = max(
            evaluations,
            key=lambda item: (
                round(float(item["auc"]), 12),
                round(float(item["score_separation"]), 12),
                -abs(float(item["weight"]) - float(current_weight)),
                -float(item["weight"]),
            ),
        )
        recommended_weight = best["weight"]

    return {
        "sufficient": sufficient,
        "reasons": reasons,
        "current_weight": float(current_weight),
        "recommended_weight": recommended_weight,
        "sample_counts": {
            "feedback": total,
            "eligible": len(items),
            "like": positive_count,
            "dislike": negative_count,
            "missing": max(0, total - len(items)),
        },
        "coverage": (len(items) / total) if total else 0.0,
        "missing_reasons": missing_counts,
        "evaluations": evaluations,
    }
