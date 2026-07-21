#!/usr/bin/env python3
"""Evaluate semantic vector Exploration retrieval without writing state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import database
from exploration_vector_evaluation import load_vector_exploration_evaluation


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="只读评估 semantic vector Exploration 候选来源。"
    )
    parser.add_argument("--db", type=Path, default=database.DB_PATH)
    parser.add_argument("--model")
    parser.add_argument("--since", help="只统计此 ISO 时间之后开始的运行")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = load_vector_exploration_evaluation(
            args.db, model=args.model, since=args.since,
        ).to_dict()
    except (OSError, sqlite3.Error) as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    print(
        f"运行: {report['completed_run_count']}/{report['run_count']} completed | "
        f"候选: {report['candidate_count']} | 入选: {report['selected_count']}"
    )
    print(
        f"反馈: {report['feedback_count']} "
        f"(like={report['likes']}, dislike={report['dislikes']}, skip={report['skips']}) | "
        f"反馈覆盖率: {report['feedback_rate']:.3f}"
    )
    for label, key in (
        ("Like rate", "like_rate"),
        ("平均排名移动（带符号）", "mean_signed_rank_movement"),
        ("平均绝对排名移动", "mean_absolute_rank_movement"),
        ("平均 Preference Profile 集中度", "mean_profile_concentration"),
        ("平均 Slate 画像支持集中度", "mean_slate_profile_concentration"),
        ("平均重复语义率", "mean_duplicate_semantic_rate"),
    ):
        value = report[key]
        print(f"{label}: {'N/A' if value is None else f'{value:.4f}'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
