#!/usr/bin/env python3
"""Generate and shortlist advisory AI reviews for Tag Mapping Candidates."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import database as db
from config import load_config, resolve_tag_mapping_config
from tag_relationship_judge import (
    MERGE_PRINCIPLES_VERSION,
    OpenAICompatibleRelationshipJudge,
    plan_ai_recommendation_staging,
    relationship_evidence,
    relationship_evidence_hash,
)


def relationship_judge_from_config() -> tuple[OpenAICompatibleRelationshipJudge, int]:
    runtime = resolve_tag_mapping_config(load_config(PROJECT_ROOT / "config.yaml"))
    if not runtime.get("enabled"):
        raise ValueError("tag_mapping 尚未启用")
    concurrency = max(1, int(runtime.get("review_concurrency", 3)))
    return OpenAICompatibleRelationshipJudge(runtime), concurrency


async def judge_candidates(
    *,
    limit: int,
    refresh: bool,
    judge=None,
    concurrency: int | None = None,
) -> dict:
    """Review a bounded queue; never accept/reject a candidate here."""

    if int(limit) < 1:
        raise ValueError("judge limit must be at least 1")
    if judge is None:
        judge, configured_concurrency = relationship_judge_from_config()
        concurrency = concurrency or configured_concurrency
    concurrency = max(1, int(concurrency or 1))
    pending = await db.get_tag_mapping_candidates(limit=500)
    selected = [
        candidate for candidate in pending
        if refresh
        or candidate.get("ai_model") != judge.identity
        or candidate.get("ai_principles_version") != MERGE_PRINCIPLES_VERSION
        or candidate.get("ai_evidence_hash") != relationship_evidence_hash(candidate)
    ][:int(limit)]
    semaphore = asyncio.Semaphore(concurrency)

    async def judge_safely(candidate):
        async with semaphore:
            try:
                return candidate, await judge.judge(candidate), None
            except Exception as error:
                return candidate, None, error

    outcomes = await asyncio.gather(*(judge_safely(candidate) for candidate in selected))
    failures = []
    judged = 0
    for candidate, recommendation, error in outcomes:
        if error is not None:
            failures.append({"candidate_id": candidate["id"], "error": str(error)})
            continue
        await db.save_tag_mapping_ai_recommendation(
            candidate["id"], recommendation.as_dict(),
            model=judge.identity,
            principles_version=MERGE_PRINCIPLES_VERSION,
            evidence=relationship_evidence(candidate),
        )
        judged += 1
    return {
        "selected": len(selected),
        "judged": judged,
        "failed": len(failures),
        "failures": failures,
        "model": judge.identity,
        "principles_version": MERGE_PRINCIPLES_VERSION,
    }


async def stage_recommendations(min_confidence: float, *, confirm: bool) -> dict:
    """Preview or mark a safe shortlist; never create Tag Aliases."""

    candidates = await db.get_tag_mapping_candidates(limit=500)
    plan = plan_ai_recommendation_staging(
        candidates, min_confidence=min_confidence,
    )
    by_id = {int(candidate["id"]): candidate for candidate in candidates}
    recommendations = [
        {
            "candidate_id": item.candidate_id,
            "recommendation_id": item.recommendation_id,
            "original_tag": by_id[item.candidate_id]["original_tag"],
            "proposed_normalized_tag": by_id[item.candidate_id]["proposed_normalized_tag"],
            "decision": item.decision,
            "confidence": by_id[item.candidate_id]["ai_confidence"],
            "rationale": by_id[item.candidate_id].get("ai_rationale") or "",
        }
        for item in plan.decisions
    ]
    result = {
        "eligible": len(plan.decisions),
        "blocked": plan.blocked,
        "recommendations": recommendations,
        "min_confidence": min_confidence,
    }
    if not confirm:
        return {**result, "dry_run": True}
    staged = await db.stage_tag_mapping_ai_recommendations(plan.decisions)
    return {**result, "staged": staged}


async def list_candidates(limit: int) -> dict:
    if int(limit) < 1:
        raise ValueError("list limit must be at least 1")
    return {"items": await db.get_tag_mapping_candidates(limit=int(limit))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "流程：judge --limit 20 → list → stage --dry-run → "
            "stage --confirm → 在 Web 标签页面逐条最终接受。"
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)
    judge = commands.add_parser("judge", help="让配置的 LLM 复核有限数量候选")
    judge.add_argument("--limit", type=int, default=20)
    judge.add_argument("--refresh", action="store_true")
    listing = commands.add_parser("list", help="显示候选及最新 AI Recommendation")
    listing.add_argument("--limit", type=int, default=40)
    stage = commands.add_parser("stage", help="预览或暂存安全的高置信建议")
    stage.add_argument("--min-confidence", type=float, default=0.95)
    confirmation = stage.add_mutually_exclusive_group(required=True)
    confirmation.add_argument("--dry-run", action="store_true")
    confirmation.add_argument("--confirm", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db._init_db_sync()
    if args.command == "judge":
        result = asyncio.run(judge_candidates(limit=args.limit, refresh=args.refresh))
    elif args.command == "list":
        result = asyncio.run(list_candidates(args.limit))
    else:
        result = asyncio.run(stage_recommendations(
            args.min_confidence, confirm=args.confirm,
        ))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2)
