import argparse
import sys
from pathlib import Path
import asyncio
import json

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from database import get_all_strategy_stats, init_db


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
        print(f"{strategy}	{data['success_count']}	{data['total_count']}	{data['success_rate']:.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
