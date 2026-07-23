#!/usr/bin/env python3
"""Run search-first tag classification in shadow mode without touching the DB."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from search_grounded_judge import (  # noqa: E402
    BraveLLMContextClient,
    DeepSeekFlashClassifier,
    MonthlyQuotaUsageLedger,
    SearchCredentialPool,
    SearchGroundedJudge,
    SearchPoolConfig,
    TavilySearchClient,
    run_shadow_evaluation,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSONL: tag, optional translation, expected_classification")
    parser.add_argument("--report", type=Path, required=True, help="Where to write the JSON shadow report")
    parser.add_argument("--brave-key-env", action="append", required=True, metavar="NAME")
    parser.add_argument("--tavily-key-env", action="append", required=True, metavar="NAME")
    parser.add_argument("--deepseek-key-env", default="DEEPSEEK_API_KEY", metavar="NAME")
    parser.add_argument("--brave-free-search-limit", type=int, default=1000)
    parser.add_argument("--tavily-free-search-limit", type=int, default=500,
                        help="Advanced Search consumes two Tavily credits per request")
    parser.add_argument("--quota-state-path", type=Path,
                        default=Path("data/search_judge_quota_usage.json"),
                        help="Local monthly redacted usage ledger; contains no API keys")
    parser.add_argument("--concurrency", type=int, default=3)
    return parser.parse_args()


def _read_items(path: Path) -> list[dict]:
    items = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"输入第 {line_number} 行不是 JSON") from exc
        if not isinstance(item, dict) or not str(item.get("tag") or "").strip():
            raise ValueError(f"输入第 {line_number} 行缺少 tag")
        items.append(item)
    if not items:
        raise ValueError("输入文件没有可测试的 tag")
    return items


def _build_pools(
    env_names: list[str], provider: str, request_limit: int, *, initial_usage: dict[str, int],
) -> SearchCredentialPool:
    pools = []
    for index, env_name in enumerate(env_names, start=1):
        api_key = os.environ.get(env_name, "").strip()
        if not api_key:
            raise ValueError(f"环境变量 {env_name} 未设置")
        pools.append(SearchPoolConfig(f"{provider}-{index}", api_key, request_limit=request_limit))
    return SearchCredentialPool(pools, initial_requests_used=initial_usage)


async def _run(args: argparse.Namespace) -> dict:
    ledger = MonthlyQuotaUsageLedger(args.quota_state_path)
    initial_usage = ledger.initial_usage()
    brave_pool = _build_pools(
        args.brave_key_env, "brave", args.brave_free_search_limit, initial_usage=initial_usage,
    )
    tavily_pool = _build_pools(
        args.tavily_key_env, "tavily", args.tavily_free_search_limit, initial_usage=initial_usage,
    )
    try:
        deepseek_key = os.environ.get(args.deepseek_key_env, "").strip()
        if not deepseek_key:
            raise ValueError(f"环境变量 {args.deepseek_key_env} 未设置")
        judge = SearchGroundedJudge(
            BraveLLMContextClient(brave_pool),
            TavilySearchClient(tavily_pool),
            DeepSeekFlashClassifier(deepseek_key),
        )
        return await run_shadow_evaluation(
            _read_items(args.input), judge,
            pool_statuses=lambda: brave_pool.status() + tavily_pool.status(),
            concurrency=args.concurrency,
        )
    finally:
        ledger.save(brave_pool.status() + tavily_pool.status())


def main() -> int:
    args = _parse_args()
    try:
        report = asyncio.run(_run(args))
    except (OSError, ValueError) as exc:
        print(f"shadow judge failed: {exc}", file=sys.stderr)
        return 2
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in (
        "total", "matched", "unresolved", "agreement_rate", "unresolved_rate",
    )} | {"priority_metrics": report["priority_metrics"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
