import argparse
import sys
from pathlib import Path
import asyncio
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from database import get_all_strategy_stats, init_db


def normalize_strategy_data(data: dict) -> tuple[int, int, float]:
    """兼容不同版本的统计字段，并统一返回百分比成功率。"""
    success = int(data.get("success_count", data.get("success", 0)) or 0)
    total = int(data.get("total_count", data.get("total", 0)) or 0)

    if "success_rate" in data:
        rate_percent = float(data["success_rate"] or 0.0)
    elif "rate" in data:
        rate_percent = float(data["rate"] or 0.0) * 100
    else:
        rate_percent = (success / total * 100) if total > 0 else 0.0

    return success, total, rate_percent


async def main():
    parser = argparse.ArgumentParser(description="推荐质量基线评估")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    await init_db()
    stats = await get_all_strategy_stats()
    if args.json:
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return

    print("strategy	success	total	rate")
    for strategy, data in sorted(stats.items()):
        success, total, rate_percent = normalize_strategy_data(data)
        print(f"{strategy}	{success}	{total}	{rate_percent:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
