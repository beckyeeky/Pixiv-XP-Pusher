"""Read-only evaluation for semantic vector Exploration retrieval runs."""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class VectorExplorationEvaluation:
    run_count: int
    completed_run_count: int
    candidate_count: int
    selected_count: int
    feedback_count: int
    likes: int
    dislikes: int
    skips: int
    feedback_rate: float
    like_rate: float | None
    mean_signed_rank_movement: float | None
    mean_absolute_rank_movement: float | None
    mean_profile_concentration: float | None
    mean_slate_profile_concentration: float | None
    mean_duplicate_semantic_rate: float | None

    def to_dict(self) -> dict:
        return asdict(self)


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def evaluate_vector_exploration_rows(
    runs: list[dict],
    candidates: list[dict],
) -> VectorExplorationEvaluation:
    selected = [item for item in candidates if item.get("selected")]
    actions = [item.get("action") for item in selected if item.get("action")]
    likes = actions.count("like")
    dislikes = actions.count("dislike")
    skips = actions.count("skip")
    preference_feedback = likes + dislikes
    movements = [
        float(item["final_rank"]) - float(item["retrieval_rank"])
        for item in candidates
        if item.get("final_rank") is not None
    ]
    completed = [run for run in runs if run.get("status") == "completed"]
    profile_concentrations = [
        float(run["profile_concentration"])
        for run in runs
        if run.get("profile_concentration") is not None
    ]
    concentrations = [
        float(run["slate_profile_concentration"])
        for run in completed
        if run.get("slate_profile_concentration") is not None
    ]
    duplicate_rates = [
        float(run["duplicate_semantic_rate"])
        for run in completed
        if run.get("duplicate_semantic_rate") is not None
    ]
    return VectorExplorationEvaluation(
        run_count=len(runs),
        completed_run_count=len(completed),
        candidate_count=len(candidates),
        selected_count=len(selected),
        feedback_count=len(actions),
        likes=likes,
        dislikes=dislikes,
        skips=skips,
        feedback_rate=(len(actions) / len(selected)) if selected else 0.0,
        like_rate=(likes / preference_feedback) if preference_feedback else None,
        mean_signed_rank_movement=_mean(movements),
        mean_absolute_rank_movement=_mean([abs(value) for value in movements]),
        mean_profile_concentration=_mean(profile_concentrations),
        mean_slate_profile_concentration=_mean(concentrations),
        mean_duplicate_semantic_rate=_mean(duplicate_rates),
    )


def load_vector_exploration_evaluation(
    db_path: str | Path,
    *,
    model: str | None = None,
    since: str | None = None,
) -> VectorExplorationEvaluation:
    clauses, params = [], []
    if model:
        clauses.append("model = ?")
        params.append(model)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    try:
        runs = [
            dict(row) for row in connection.execute(
                f"SELECT * FROM exploration_vector_runs {where} ORDER BY created_at",
                params,
            ).fetchall()
        ]
        run_ids = [run["run_id"] for run in runs]
        if not run_ids:
            return evaluate_vector_exploration_rows([], [])
        placeholders = ",".join("?" for _ in run_ids)
        candidates = [
            dict(row) for row in connection.execute(
                f"""
                SELECT evc.*, feedback.action
                FROM exploration_vector_candidates AS evc
                LEFT JOIN feedback ON feedback.illust_id = evc.illust_id
                WHERE evc.run_id IN ({placeholders})
                ORDER BY evc.run_id, evc.retrieval_rank
                """,
                run_ids,
            ).fetchall()
        ]
    finally:
        connection.close()
    return evaluate_vector_exploration_rows(runs, candidates)
