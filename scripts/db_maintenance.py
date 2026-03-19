import argparse
import sys
import asyncio
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.append(str(PROJECT_ROOT))

from database import DB_PATH, cleanup_old_records, get_db_overview, init_db


def backup_database(target: Path | None = None) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB_PATH, target)
    return target


async def main():
    parser = argparse.ArgumentParser(description="数据库维护工具")
    parser.add_argument("action", choices=["overview", "backup", "cleanup"])
    parser.add_argument("--days", type=int, default=180, help="cleanup 保留天数")
    parser.add_argument("--output", type=str, default="", help="backup 输出文件路径")
    args = parser.parse_args()

    await init_db()

    if args.action == "overview":
        print(await get_db_overview())
        return

    if args.action == "backup":
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = Path(args.output) if args.output else DB_PATH.with_name(f"pixiv_xp_{stamp}.db")
        print(f"backup -> {backup_database(output)}")
        return

    if args.action == "cleanup":
        await cleanup_old_records(days=args.days)
        print(await get_db_overview())


if __name__ == "__main__":
    asyncio.run(main())
