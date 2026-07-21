#!/usr/bin/env python3
"""Calibrate work-level semantic_weight from cached feedback without writing state."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config import load_config, resolve_model
from embedding_calibration import evaluate_embedding_weights
from embedding_calibration_store import load_calibration_dataset


def _embedding_settings(config: dict) -> tuple[str, float]:
    settings = dict((config.get("ai") or {}).get("embedding") or {})
    model_ref = str(settings.get("model") or "").strip()
    if model_ref in (config.get("models") or {}):
        settings.update(resolve_model(config, model_ref, "embedding"))
    model = str(settings.get("model") or "").strip()
    if not model:
        raise ValueError("ai.embedding.model 未配置")
    return model, float(settings.get("semantic_weight", 0.3))


def _pixiv_user_id(config: dict) -> int | None:
    for provider in (config.get("providers") or {}).values():
        if isinstance(provider, dict) and provider.get("type") == "pixiv":
            try:
                value = int(provider.get("user_id") or 0)
            except (TypeError, ValueError):
                value = 0
            return value or None
    return None


def _weights(value: str) -> list[float]:
    try:
        result = [float(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("weights 必须是逗号分隔的数字") from exc
    if not result or any(item < 0.0 or item > 1.0 for item in result):
        raise argparse.ArgumentTypeError("weights 必须包含 0 到 1 之间的数字")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读比较作品级 Embedding semantic_weight，不修改配置或数据库。"
    )
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.yaml")
    parser.add_argument("--database", type=Path, default=PROJECT_ROOT / "data" / "pixiv_xp.db")
    parser.add_argument("--user-id", type=int, help="覆盖 Pixiv Provider 中的 user_id")
    parser.add_argument("--weights", type=_weights, default=_weights("0,0.1,0.2,0.3,0.4,0.5"))
    parser.add_argument("--min-samples", type=int, default=20)
    parser.add_argument("--min-per-class", type=int, default=5)
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser


def _payload(args: argparse.Namespace) -> dict:
    config = load_config(args.config)
    model, current_weight = _embedding_settings(config)
    dataset = load_calibration_dataset(
        args.database,
        embedding_model=model,
        user_id=args.user_id if args.user_id is not None else _pixiv_user_id(config),
        classification_ttl_days=int((config.get("tag_classifier") or {}).get("ttl_days", 30)),
    )
    report = evaluate_embedding_weights(
        dataset.samples,
        args.weights,
        current_weight=current_weight,
        total_feedback=dataset.total_feedback,
        missing=dataset.missing,
        min_samples=args.min_samples,
        min_per_class=args.min_per_class,
    )
    report["dataset"] = {
        "database": str(args.database.expanduser().resolve()),
        "user_id": dataset.user_id,
        "embedding_model": dataset.embedding_model,
        "profile_hash": dataset.profile_hash,
        "cached_profile_hash": dataset.cached_profile_hash,
        "read_only": True,
    }
    report["feedback"] = {
        "like": dataset.stored_like,
        "dislike": dataset.stored_dislike,
        "follow": dataset.stored_follow,
        "first_at": dataset.first_feedback_at,
        "latest_at": dataset.latest_feedback_at,
    }
    return report


def _format_number(value: float | None, digits: int = 3) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def print_human(report: dict) -> None:
    counts = report["sample_counts"]
    dataset = report["dataset"]
    print("作品级 Embedding 权重离线校准（只读）")
    print(f"模型: {dataset['embedding_model']}  用户: {dataset['user_id'] or '自动'}")
    feedback = report.get("feedback") or {}
    if feedback:
        print(
            f"存储反馈: like={feedback['like']}  dislike={feedback['dislike']}  "
            f"follow={feedback['follow']}（不参与校准）"
        )
        print(f"反馈时间: {feedback['first_at'] or '—'} ~ {feedback['latest_at'] or '—'}")
    print(
        f"反馈: {counts['feedback']}  可用: {counts['eligible']} "
        f"({report['coverage']:.1%})  like: {counts['like']}  dislike: {counts['dislike']}"
    )
    if report["missing_reasons"]:
        reasons = ", ".join(f"{key}={value}" for key, value in sorted(report["missing_reasons"].items()))
        print(f"缺失: {reasons}")
    print()
    print("weight   AUC    分离度   平均排名移动")
    for item in report["evaluations"]:
        marker = "*" if item["weight"] == report["current_weight"] else " "
        print(
            f"{marker}{item['weight']:>5.2f}  {_format_number(item['auc']):>5}  "
            f"{_format_number(item['score_separation']):>7}  "
            f"{_format_number(item['mean_rank_movement'], 2):>12}"
        )
    print("\n* 当前配置")
    if report["sufficient"]:
        print(f"建议候选 semantic_weight: {report['recommended_weight']:.2f}")
        print("此命令不会修改 config.yaml；请人工复核报告后再决定是否调整。")
    else:
        print("暂不建议修改 semantic_weight：")
        for reason in report["reasons"]:
            print(f"- {reason}")
        print("先积累 like/dislike，并让正常推荐流程生成与当前画像、模型一致的缓存向量。")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = _payload(args)
    except (FileNotFoundError, sqlite3.Error, ValueError) as exc:
        if args.json:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"无法校准: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_human(report)
    return 0 if report["sufficient"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
