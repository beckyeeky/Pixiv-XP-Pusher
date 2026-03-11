
import argparse
import asyncio
import contextvars
import logging
import os
import sys
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Ensure project root in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, CONFIG_PATH
from database import init_db, cache_illust, get_cached_illust_tags, get_cached_illust, mark_pushed
from pixiv_client import PixivClient
from profiler import XPProfiler
from fetcher import ContentFetcher
from filter import ContentFilter
from notifier.telegram import TelegramNotifier
from notifier.onebot import OneBotNotifier
from utils import setup_logging
from push_stats import PushStats, create_stats, set_current_stats
from singleton import check_single_instance

logger = logging.getLogger(__name__)


from task_manager import run_once, run_scheduler, _force_mode_ctx

def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(description="Pixiv-XP-Pusher")
    parser.add_argument("--once", action="store_true", help="立即执行一次并退出")
    parser.add_argument("--now", action="store_true", help="启动时立即执行一次，然后保持后台运行（调度模式）")
    parser.add_argument("--reset-xp", action="store_true", help="重置 XP 数据")
    parser.add_argument("--test", action="store_true", help="快速测试模式")
    parser.add_argument("--force", action="store_true", help="强制触发，跳过队列限制")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="配置文件路径")
    args = parser.parse_args()
    
    # 全局标记强制模式
    _force_mode_ctx.set(args.force)
    
    setup_logging()
    
    # 单实例锁检查（防止重复启动）
    check_single_instance()
    
    if args.reset_xp:
        from database import reset_xp_data, init_db
        logger.info("正在清除 XP 数据...")
        asyncio.run(init_db())
        asyncio.run(reset_xp_data())
        logger.info("✅ XP 数据已清除。")
        return
    
    config = load_config()
    
    # 测试模式 override
    if args.test:
        logger.info("🔧 启用测试模式：参数最小化")
        config.setdefault("profiler", {})["scan_limit"] = 10
        config["profiler"]["discovery_rate"] = 0
        config.setdefault("fetcher", {})["bookmark_threshold"] = {"search": 0, "subscription": 0}
        config["fetcher"]["discovery_limit"] = 1
        config["fetcher"]["ranking"] = {"modes": ["day"], "limit": 1}
        # Force once for test
        args.once = True
    
    if args.once:
        asyncio.run(run_once(config, force=args.force))
    else:
        # If --now is set, run_scheduler will handle immediate run
        asyncio.run(run_scheduler(config, run_immediately=args.now))


if __name__ == "__main__":
    main()
