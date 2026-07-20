#!/usr/bin/env python3
"""Generate and shortlist advisory AI reviews for Tag Mapping Candidates."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
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
    RelationshipJudgeResponseError,
    plan_high_confidence_ai_actions,
    plan_ai_recommendation_staging,
    relationship_evidence,
    relationship_evidence_hash,
)
from tag_mapping import would_create_alias_cycle
from utils import normalize_tag


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
    pending = db.collapse_tag_mapping_candidate_groups(
        db.get_tag_mapping_candidates_sync(limit=500)
    )
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
            failure = {"candidate_id": candidate["id"], "error": str(error)}
            if isinstance(error, RelationshipJudgeResponseError):
                failure["finish_reason"] = error.finish_reason
                failure["response_excerpt"] = error.response_excerpt
            failures.append(failure)
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


def _build_recommendation_plan(min_confidence: float, *, follow_ai: bool = False):
    candidates = db.get_tag_mapping_candidates_sync(limit=500)
    planner = plan_high_confidence_ai_actions if follow_ai else plan_ai_recommendation_staging
    plan = planner(
        candidates, min_confidence=min_confidence,
    )
    by_id = {int(candidate["id"]): candidate for candidate in candidates}
    if follow_ai:
        aliases = {
            normalize_tag(original): normalize_tag(target)
            for original, target in db.get_tag_aliases_sync().items()
        }
        decisions = []
        blocked = Counter(plan.blocked)
        for item in plan.decisions:
            if item.decision != "accept_equivalent":
                decisions.append(item)
                continue
            candidate = by_id[item.candidate_id]
            canonical = candidate.get("ai_canonical_tag")
            alias_original = next(
                tag for tag in (
                    candidate["original_tag"],
                    candidate["proposed_normalized_tag"],
                )
                if tag != canonical
            )
            original = normalize_tag(alias_original)
            target = normalize_tag(canonical or "")
            existing_target = aliases.get(original)
            if existing_target and existing_target != target:
                blocked["alias_conflict"] += 1
                continue
            if (
                existing_target != target
                and aliases.get(target) != original
                and would_create_alias_cycle(aliases, original, target)
            ):
                blocked["alias_cycle"] += 1
                continue
            decisions.append(item)
            if not existing_target and aliases.get(target) != original:
                aliases[original] = target
        plan = type(plan)(tuple(decisions), dict(blocked))
    recommendations = [
        {
            "candidate_id": item.candidate_id,
            "recommendation_id": item.recommendation_id,
            "original_tag": by_id[item.candidate_id]["original_tag"],
            "proposed_normalized_tag": by_id[item.candidate_id]["proposed_normalized_tag"],
            "decision": item.decision,
            "confidence": by_id[item.candidate_id]["ai_confidence"],
            "rationale": by_id[item.candidate_id].get("ai_rationale") or "",
            "alias_original": (
                next(
                    tag for tag in (
                        by_id[item.candidate_id]["original_tag"],
                        by_id[item.candidate_id]["proposed_normalized_tag"],
                    )
                    if tag != by_id[item.candidate_id].get("ai_canonical_tag")
                )
                if item.decision == "accept_equivalent" and follow_ai else
                by_id[item.candidate_id]["original_tag"]
                if item.decision == "accept_equivalent" else None
            ),
            "normalized_tag": (
                by_id[item.candidate_id].get("ai_canonical_tag")
                if item.decision == "accept_equivalent" and follow_ai else
                by_id[item.candidate_id]["proposed_normalized_tag"]
                if item.decision == "accept_equivalent" else None
            ),
        }
        for item in plan.decisions
    ]
    result = {
        "eligible": len(plan.decisions),
        "blocked": plan.blocked,
        "recommendations": recommendations,
        "min_confidence": min_confidence,
    }
    return plan, result


async def stage_recommendations(min_confidence: float, *, confirm: bool) -> dict:
    """Preview or mark a safe shortlist; never create Tag Aliases."""

    plan, result = _build_recommendation_plan(min_confidence)
    if not confirm:
        return {**result, "dry_run": True}
    staged = db.stage_tag_mapping_ai_recommendations_sync(plan.decisions)
    return {**result, "staged": staged}


async def apply_recommendations(
    min_confidence: float, *, confirm: bool, follow_ai: bool = False,
) -> dict:
    """Preview or atomically apply the safe AI plan without per-item Web review."""

    plan, result = _build_recommendation_plan(
        min_confidence, follow_ai=follow_ai,
    )
    if not confirm:
        return {**result, "dry_run": True}
    applied = db.apply_tag_mapping_ai_batch_sync(plan.decisions)
    return {**result, "confirmed": True, **applied}


async def list_candidates(limit: int) -> dict:
    if int(limit) < 1:
        raise ValueError("list limit must be at least 1")
    return {"items": db.get_tag_mapping_candidates_sync(limit=int(limit))}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "逐条审核：judge → stage --dry-run → WebUI；"
            "跳过逐条审核：apply --dry-run → apply --confirm。"
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
    apply = commands.add_parser(
        "apply", help="按安全计划预览或直接应用 AI Recommendation",
    )
    apply.add_argument("--min-confidence", type=float, default=0.95)
    apply.add_argument(
        "--follow-ai", action="store_true",
        help="高置信时按 AI canonical 方向接受等价项，并拒绝 related/distinct 候选",
    )
    apply_confirmation = apply.add_mutually_exclusive_group(required=True)
    apply_confirmation.add_argument("--dry-run", action="store_true")
    apply_confirmation.add_argument("--confirm", action="store_true")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    db._init_db_sync()
    if args.command == "judge":
        result = asyncio.run(judge_candidates(limit=args.limit, refresh=args.refresh))
    elif args.command == "list":
        result = asyncio.run(list_candidates(args.limit))
    elif args.command == "stage":
        result = asyncio.run(stage_recommendations(
            args.min_confidence, confirm=args.confirm,
        ))
    else:
        result = asyncio.run(apply_recommendations(
            args.min_confidence, confirm=args.confirm, follow_ai=args.follow_ai,
        ))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(2)
