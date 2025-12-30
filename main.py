
import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# Ensure project root in path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from config import load_config, CONFIG_PATH
from database import init_db, cache_illust, get_cached_illust_tags, mark_pushed
from pixiv_client import PixivClient
from profiler import XPProfiler
from fetcher import ContentFetcher
from filter import ContentFilter
from notifier.telegram import TelegramNotifier
from notifier.onebot import OneBotNotifier
from utils import setup_logging

logger = logging.getLogger(__name__)


async def setup_notifiers(config: dict, client: PixivClient, profiler: XPProfiler):
    """创建并配置推送器（支持多推送渠道）"""
    notifier_cfg = config.get("notifier", {})
    # 支持单个 type 字符串或 types 列表
    notifier_types = notifier_cfg.get("types") or [notifier_cfg.get("type", "telegram")]
    if isinstance(notifier_types, str):
        notifier_types = [notifier_types]
    
    async def on_feedback(illust_id: int, action: str):
        """反馈回调"""
        # 从缓存获取作品tags
        cached_tags = await get_cached_illust_tags(illust_id)
        if cached_tags:
            # 创建简化的Illust对象
            from pixiv_client import Illust
            from datetime import datetime
            illust = Illust(
                id=illust_id,
                title="",
                user_id=0,
                user_name="",
                tags=cached_tags,
                bookmark_count=0,
                view_count=0,
                page_count=1,
                image_urls=[],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now()
            )
            await profiler.apply_feedback(
                illust=illust,
                action=action,
                config=config.get("feedback", {})
            )
            
            # 如果是"喜欢"，同步添加到 Pixiv 收藏
            if action in ("like", "1"):
                 try:
                     await client.add_bookmark(illust_id)
                 except Exception as e:
                     logger.error(f"同步收藏失败: {e}")
            
            logger.info(f"反馈处理完成: illust_id={illust_id}, action={action}")
        else:
            logger.warning(f"未找到作品缓存: {illust_id}")
            
    async def on_action(action: str, data: any):
        """通用动作回调"""
        if action == "retry_ai":
            error_id = int(data)
            logger.info(f"收到重试请求: error_id={error_id}")
            
            try:
                from database import get_ai_error, update_ai_error_status
                import json
                
                # 1. 获取错误记录
                error_record = await get_ai_error(error_id)
                if not error_record:
                    logger.error("错误记录不存在")
                    return
                
                if error_record["status"] == "resolved":
                    logger.info("该错误已修复")
                    return

                tags = json.loads(error_record["tags_content"])
                
                # 2. 重新尝试 AI 处理
                logger.info(f"正在重试 AI 处理 {len(tags)} 个标签...")
                valid, mapping = await profiler.ai_processor.process_tags(tags)
                
                await update_ai_error_status(error_id, "resolved")
                
                # 通知用户（使用第一个可用的 notifier）
                msg = f"✅ 修复成功！\n已验证 AI 配置可用。\n({len(tags)} 个标签已正确处理)"
                for n in notifiers:
                    if hasattr(n, 'send_text'):
                        await n.send_text(msg)
                        break
                
            except Exception as e:
                logger.error(f"重试失败: {e}")
    
    notifiers = []
    
    if "telegram" in notifier_types:
        tg_cfg = notifier_cfg.get("telegram", {})
        # 支持旧配置 chat_id 或新配置 chat_ids
        chat_ids = tg_cfg.get("chat_ids") or tg_cfg.get("chat_id")
        if tg_cfg.get("bot_token") and chat_ids:
            notifiers.append(TelegramNotifier(
                bot_token=tg_cfg["bot_token"],
                chat_ids=chat_ids,
                client=client,
                multi_page_mode=notifier_cfg.get("multi_page_mode", "cover_link"),
                allowed_users=tg_cfg.get("allowed_users"),
                thread_id=tg_cfg.get("thread_id"),
                on_feedback=on_feedback,
                on_action=on_action
            ))
            logger.info("已启用 Telegram 推送")
    
    if "onebot" in notifier_types:
        ob_cfg = notifier_cfg.get("onebot", {})
        if ob_cfg.get("ws_url"):
            ob_notifier = OneBotNotifier(
                ws_url=ob_cfg["ws_url"],
                private_id=ob_cfg.get("private_id"),
                group_id=ob_cfg.get("group_id"),
                push_to_private=ob_cfg.get("push_to_private", True),
                push_to_group=ob_cfg.get("push_to_group", False),
                master_id=ob_cfg.get("master_id"),
                on_feedback=on_feedback
            )
            try:
                await ob_notifier.connect()
                notifiers.append(ob_notifier)
                logger.info("已启用 OneBot 推送")
            except Exception as e:
                logger.error(f"OneBot 连接失败: {e}")
    
    return notifiers if notifiers else None


async def setup_services(config: dict):
    """初始化全局服务 (DB, Client, Profiler, Notifiers)"""
    await init_db()
    
    # Init Client
    network_cfg = config.get("network", {})
    client = PixivClient(
        refresh_token=config["pixiv"].get("refresh_token"),
        requests_per_minute=network_cfg.get("requests_per_minute", 60),
        random_delay=tuple(network_cfg.get("random_delay", [1.0, 3.0])),
        max_concurrency=network_cfg.get("max_concurrency", 5)
    )
    await client.login()

    # Init Profiler
    profiler_cfg = config.get("profiler", {})
    profiler = XPProfiler(
        client=client,
        stop_words=profiler_cfg.get("stop_words"),
        discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
        ai_config=profiler_cfg.get("ai")
    )
    
    # Init Notifiers
    notifiers = await setup_notifiers(config, client, profiler)
    
    return client, profiler, notifiers


async def main_task(config: dict, client: PixivClient, profiler: XPProfiler, notifiers: list):
    """
    执行一次完整的推送任务 (依赖外部服务)
    """
    logger.info("=== 开始推送任务 ===")
    
    try:
        # 1. 构建/更新 XP 画像
        profiler_cfg = config.get("profiler", {})
        
        await profiler.build_profile(
            user_id=config["pixiv"]["user_id"],
            scan_limit=profiler_cfg.get("scan_limit", 500),
            include_private=profiler_cfg.get("include_private", True)
        )
        
        top_tags = await profiler.get_top_tags(profiler_cfg.get("top_n", 20))
        logger.info(f"Top XP Tags: {[t[0] for t in top_tags[:10]]}")
        
        if config.get("test"): # Test mode skip heavy DB load if possible, but we need it for xp_profile
             pass
             
        # 获取完整的 XP Profile 用于匹配度计算
        import database as db_module
        xp_profile = await db_module.get_xp_profile()
        
        # 2. 获取内容
        fetcher_cfg = config.get("fetcher", {})
        
        # 1.5 获取关注列表（用于加权和订阅检查）
        following_ids = set()
        pixiv_uid = config.get("pixiv", {}).get("user_id", 0)
        if pixiv_uid:
            try:
                following_ids = await client.fetch_following(user_id=pixiv_uid)
            except Exception as e:
                logger.warning(f"获取关注列表失败: {e}")
        
        manual_subs = set(fetcher_cfg.get("subscribed_artists") or [])
        all_subs = list(following_ids | manual_subs)
        logger.info(f"有效关注画师数: {len(all_subs)} (API获取: {len(following_ids)}, 手动: {len(manual_subs)})")

        fetcher = ContentFetcher(
            client=client,
            bookmark_threshold=fetcher_cfg.get("bookmark_threshold", {"search": 1000, "subscription": 0}),
            date_range_days=fetcher_cfg.get("date_range_days", 7),
            subscribed_artists=list(manual_subs),
            discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
            ranking_config=fetcher_cfg.get("ranking")
        )
        
        # 执行 Discovery (Search + Ranking + Subs)
        top_tags = await profiler.get_top_tags(profiler_cfg.get("top_n", 20)) # Re-get is cheap
        
        # 执行 Discovery (Search + Ranking + Subs)
        search_results = await fetcher.discover(
             xp_tags=top_tags,
             limit=fetcher_cfg.get("discovery_limit", 200)
        )

        # Check Subs
        subscription_results = await fetcher.check_subscriptions()
        
        # Ranking
        ranking_results = await fetcher.fetch_ranking()
        
        all_illusts = search_results + subscription_results + ranking_results
        logger.info(f"共获取 {len(all_illusts)} 个候选作品 (搜索:{len(search_results)}, 订阅:{len(subscription_results)}, 排行榜:{len(ranking_results)})")
        
        # 3. 过滤
        filter_cfg = config.get("filter", {})
        match_cfg = fetcher_cfg.get("match_score", {})
        content_filter = ContentFilter(
            blacklist_tags=filter_cfg.get("blacklist_tags"),
            daily_limit=filter_cfg.get("daily_limit", 20),
            exclude_ai=filter_cfg.get("exclude_ai", True),
            min_match_score=match_cfg.get("min_threshold", 0.0),
            match_weight=match_cfg.get("weight_in_sort", 0.5),
            max_per_artist=filter_cfg.get("max_per_artist", 3),
            subscribed_artists=all_subs,
            artist_boost=filter_cfg.get("artist_boost", 0.3),
            min_create_days=filter_cfg.get("min_create_days", 0),
            r18_mode=filter_cfg.get("r18_mode", False)
        )
        
        filtered = await content_filter.filter(all_illusts, xp_profile=xp_profile)
        logger.info(f"过滤后 {len(filtered)} 个作品")
        
        # 4. 推送
        if notifiers and filtered:
            try:
                # 缓存作品信息
                for illust in filtered:
                    await cache_illust(illust.id, illust.tags)
                
                all_sent_ids = set()
                for notifier in notifiers:
                    try:
                        sent_ids = await notifier.send(filtered)
                        all_sent_ids.update(sent_ids)
                    except Exception as e:
                        logger.error(f"推送器 {type(notifier).__name__} 发送失败: {e}")
                
                if all_sent_ids:
                    # 记录推送历史
                    filtered_map = {ill.id: ill for ill in filtered}
                    for pid in all_sent_ids:
                        if pid in filtered_map:
                            illust = filtered_map[pid]
                            source = "subscription" if illust.user_id in manual_subs else "search"
                            await mark_pushed(pid, source)
                            
                    logger.info(f"推送完成: {len(all_sent_ids)}/{len(filtered)} 个作品成功")
                else:
                    logger.error("没有任何作品被成功推送")
                    
                # 5. AI 错误报警
                ai_errors = profiler.ai_processor.occurred_errors
                if ai_errors:
                    err_count = len(ai_errors)
                    err_id = ai_errors[0]
                    msg = f"⚠️ 警告：本次任务有 {err_count} 批 Tag AI 优化失败。\n已自动记录并降级处理。"
                    buttons = [("🔄 重试修复", f"retry_ai:{err_id}")]
                    logger.warning(f"AI 优化失败 {err_count} 次，发送警告")
                    
                    for notifier in notifiers:
                        if hasattr(notifier, 'send_text'):
                            try:
                                await notifier.send_text(msg, buttons)
                            except:
                                pass
            except Exception as e:
                logger.error(f"推送过程出错: {e}")
        elif not filtered:
             logger.info("无新作品可推送")
        else:
            logger.warning("未配置推送器")
        
    except Exception as e:
        logger.error(f"任务执行出错: {e}", exc_info=True)
    
    logger.info("=== 推送任务结束 ===")


async def run_once(config: dict):
    """立即执行一次"""
    client, profiler, notifiers = await setup_services(config)
    
    # 即使是 Run Once，如果用于测试，可能也需要 Feedback?
    # 但 cli --once 通常是脚本调用，跑完即走。
    # 这里我们还是启动监听 (如果是 Test 模式也许不需要?)
    # 如果是 --test, 我们不启动监听? 
    # 如果用户想测试反馈，OneBot/TG 需要跑。
    # 但 script ends immediately. Feedback needs loop.
    # 所以 --once 真的就是 "Fire and Forget".
    
    try:
        await main_task(config, client, profiler, notifiers)
    finally:
        await client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'): 
                try: 
                    await n.close() 
                except: 
                    pass


async def run_scheduler(config: dict, run_immediately: bool = False):
    """启动调度器 (Daemon Mode)"""
    client, profiler, notifiers = await setup_services(config)
    
    # Start Listeners (Background)
    if notifiers:
        for n in notifiers:
            if isinstance(n, TelegramNotifier):
                 # TelegramNotifier.start_polling is async but handles its own background tasks (updater.start_polling)
                 await n.start_polling()
            elif isinstance(n, OneBotNotifier):
                 # OneBot loop needs to be scheduled
                 asyncio.create_task(n.start_listening())
    
    if run_immediately:
        logger.info("🚀 正在立即执行首次任务...")
        # Run main_task as a background task so it doesn't block scheduler start?
        # Or await it? Since it's "Now", usually await is fine, or create task to allow listener to process concurrently?
        # If we await, listener logic (OneBot) runs in background task ok.
        # BUT if main_task crashes, we still want scheduler.
        asyncio.create_task(main_task(config, client, profiler, notifiers))

    scheduler = AsyncIOScheduler()
    scheduler_cfg = config.get("scheduler", {})
    cron_expr = scheduler_cfg.get("cron", "0 12 * * *")
    coalesce = scheduler_cfg.get("coalesce", True)
    
    parts = cron_expr.split()
    trigger = CronTrigger(
        minute=parts[0],
        hour=parts[1],
        day=parts[2] if parts[2] != "*" else None,
        month=parts[3] if parts[3] != "*" else None,
        day_of_week=parts[4] if parts[4] != "*" else None
    )
    
    scheduler.add_job(
        lambda: asyncio.create_task(main_task(config, client, profiler, notifiers)),
        trigger=trigger,
        coalesce=coalesce,
        misfire_grace_time=3600
    )
    
    scheduler.start()
    logger.info(f"调度器已启动，cron: {cron_expr}")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
    finally:
        await client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'): 
                try:
                    await n.close()
                except:
                    pass


def main():
    """CLI 入口"""
    parser = argparse.ArgumentParser(description="Pixiv-XP-Pusher")
    parser.add_argument("--once", action="store_true", help="立即执行一次并退出")
    parser.add_argument("--now", action="store_true", help="启动时立即执行一次，然后保持后台运行（调度模式）")
    parser.add_argument("--reset-xp", action="store_true", help="重置 XP 数据")
    parser.add_argument("--test", action="store_true", help="快速测试模式")
    parser.add_argument("--config", type=str, default=str(CONFIG_PATH), help="配置文件路径")
    args = parser.parse_args()
    
    setup_logging()
    
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
        asyncio.run(run_once(config))
    else:
        # If --now is set, run_scheduler will handle immediate run
        asyncio.run(run_scheduler(config, run_immediately=args.now))


if __name__ == "__main__":
    main()
