import asyncio
import contextvars
import database as db_module
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from config import CONFIG_PATH, get_singleton_provider, load_config, resolve_profiler_ai_config
from database import init_db, cache_illust, get_cached_illust
from pixiv_client import PixivClient
from profiler import XPProfiler
from notifier.telegram import TelegramNotifier
from notifier.onebot import OneBotNotifier
from push_run import (
    MAINTENANCE_CANCELLED,
    MAINTENANCE_FAILED,
    MAINTENANCE_SUCCEEDED,
    MAINTENANCE_TIMEOUT,
    PushRun,
    get_latest_maintenance_task,
    record_maintenance_completion,
)
from push_stats import PushStats, create_stats
from related_recommender import RelatedRecommender
logger = logging.getLogger(__name__)

MAINTENANCE_WAIT_SECONDS = 90


def _get_display_tags_max_ip_count(filter_cfg: dict) -> int:
    display_tags_cfg = filter_cfg.get("display_tags", {}) if isinstance(filter_cfg, dict) else {}
    if not isinstance(display_tags_cfg, dict):
        return 2
    return display_tags_cfg.get("max_ip_count", 2)


def _parse_datetime(value: str | None) -> datetime | None:
    """解析数据库/状态里记录的时间字符串。"""
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        logger.warning("无法解析时间值: %s", value)
        return None


def _split_schedule_crons(schedule_str: str) -> list[str]:
    """兼容单 cron 与旧格式的多 cron 字符串。"""
    candidate = schedule_str.strip()
    if not candidate:
        return []

    try:
        CronTrigger.from_crontab(candidate)
        logger.info(f"识别为单一定时任务: {candidate}")
        return [candidate]
    except ValueError:
        potential_crons = [c.strip() for c in candidate.split(",") if c.strip()]
        valid_crons = []
        for cron_expr in potential_crons:
            try:
                CronTrigger.from_crontab(cron_expr)
                valid_crons.append(cron_expr)
            except ValueError:
                logger.warning(f"忽略无效的 Cron 表达式片段: {cron_expr}")

        if valid_crons:
            logger.info(f"识别为 {len(valid_crons)} 个独立定时任务")
            return valid_crons

        return [candidate]


def _get_min_schedule_interval(cron_list: list[str]) -> timedelta:
    """估算多个 Cron 中最短的触发间隔，失败时回退到 4 小时。"""
    fallback = timedelta(hours=4)
    if not cron_list:
        return fallback

    all_fire_times = []
    now = datetime.now()

    for cron_expr in cron_list:
        try:
            trigger = CronTrigger.from_crontab(cron_expr)
        except ValueError:
            logger.warning("无法为最近运行保护解析 Cron，使用回退间隔: %s", cron_expr)
            return fallback

        previous_fire_time = None
        current_time = now
        for _ in range(8):
            next_fire_time = trigger.get_next_fire_time(previous_fire_time, current_time)
            if next_fire_time is None:
                break
            fire_time = next_fire_time.replace(tzinfo=None) if next_fire_time.tzinfo else next_fire_time
            all_fire_times.append(fire_time)
            previous_fire_time = next_fire_time
            current_time = next_fire_time

    unique_fire_times = sorted(set(all_fire_times))
    deltas = [
        later - earlier
        for earlier, later in zip(unique_fire_times, unique_fire_times[1:])
        if later > earlier
    ]

    return min(deltas) if deltas else fallback


async def _get_last_successful_push_at() -> datetime | None:
    """优先读取运行态状态，缺失时回退到推送历史。"""
    state_value = await db_module.get_state("runtime.last_successful_push_at")
    parsed_state = _parse_datetime(state_value)
    if parsed_state is not None:
        return parsed_state
    return await db_module.get_last_push_at()


def _should_skip_immediate_run(
    last_push_at: datetime | None,
    min_interval: timedelta,
    now: datetime | None = None,
) -> bool:
    """最近成功推送过时，跳过 daemon 模式的 `--now`。"""
    if last_push_at is None:
        return False

    current_time = now or datetime.now()
    return current_time - last_push_at < min_interval


async def retry_async(coro_func, *args, max_retries: int = 3, delay: float = 5.0, backoff: float = 2.0, **kwargs):
    """
    通用异步重试函数
    
    Args:
        coro_func: 要执行的异步函数
        max_retries: 最大重试次数
        delay: 初始延迟秒数
        backoff: 延迟倍增系数
    
    Returns:
        函数返回值，或在所有重试失败后返回 None
    """
    last_error = None
    current_delay = delay
    
    for attempt in range(max_retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                logger.warning(f"操作失败 (尝试 {attempt + 1}/{max_retries + 1}): {e}，{current_delay:.1f}s 后重试...")
                await asyncio.sleep(current_delay)
                current_delay *= backoff
            else:
                logger.error(f"操作最终失败 (已重试 {max_retries} 次): {e}")
    
    return None


# 全局运行锁，防止任务并发
_task_lock = asyncio.Lock()
# 允许最多排队 30 个触发
_queue_limit = asyncio.Semaphore(30)
# 强制模式标记（跳过队列限制）- 使用 ContextVar 避免竞态条件
_force_mode_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar('force_mode', default=False)

# 连锁推送全局去重：跟踪正在处理中的关联推送候选（防止竞态条件导致的重复）
_related_chain_processing = set()
_related_chain_lock = asyncio.Lock()

async def setup_notifiers(config: dict, client: PixivClient, profiler: XPProfiler, sync_client: PixivClient = None):
    """创建并配置推送器（支持多推送渠道）"""
    # sync_client 用于 on_action 回调中的 main_task 调用
    if sync_client is None:
        sync_client = client
    notifier_cfg = config.get("notifier", {})
    # 支持单个 type 字符串或 types 列表
    notifier_types = notifier_cfg.get("types") or [notifier_cfg.get("type", "telegram")]
    if isinstance(notifier_types, str):
        notifier_types = [notifier_types]
    
    # 延迟引用避免因为 notifiers 列表未初始化完成导致的问题
    # 但 on_feedback 需要访问 notifiers 列表
    # 我们可以把 notifiers 定义在外部列表，然后引用它
    notifiers_list = []
    max_pages = notifier_cfg.get("max_pages", 10)
    related_recommender = RelatedRecommender(
        client=client,
        sync_client=sync_client,
        config=config,
        profiler=profiler,
        processing_set=_related_chain_processing,
        processing_lock=_related_chain_lock,
    )

    async def push_related_task(seed_illust, parent_msg_id: int = None, current_depth: int = 1):
        """异步：推送关联作品。"""
        await related_recommender.push_chain(
            seed_illust,
            notifiers_list,
            parent_msg_id=parent_msg_id,
            current_depth=current_depth,
        )

    async def on_feedback(illust_id: int, action: str):
        """反馈回调 (优化版：使用缓存避免 API 调用)"""
        illust = None
        
        # 1. 尝试从缓存获取
        cached = await get_cached_illust(illust_id)
        if cached:
            from pixiv_client import Illust
            from datetime import datetime
            illust = Illust(
                id=cached["id"],
                title="",
                user_id=cached.get("user_id", 0),
                user_name=cached.get("user_name", ""),
                tags=cached.get("tags", []),
                tags_translated=[],
                bookmark_count=0,
                view_count=0,
                page_count=1,
                image_urls=[],
                is_r18=False,
                ai_type=0,
                create_date=datetime.now()
            )
            # 是否需要完整信息（如点赞时不知道画家ID）
            if (action in ("like", "1") and illust.user_id == 0):
                try:
                    full = await client.get_illust_detail(illust_id)
                    if full: illust = full
                except Exception as e:
                    logger.warning(f"补充详情失败: {e}")
        
        # 2. 缓存未命中，回退到 API
        if not illust:
            logger.warning(f"未找到作品缓存: {illust_id}，尝试从 API 获取...")
            try:
                illust = await client.get_illust_detail(illust_id)
                if illust:
                    # 补充写入缓存
                    await cache_illust(illust.id, illust.tags, illust.user_id, illust.user_name)
                    logger.info(f"API 获取成功并已缓存: {illust.title}")
            except Exception as e:
                logger.error(f"API 回退获取失败: {e}")
        
        if not illust:
            logger.error(f"无法获取作品信息: {illust_id}，反馈处理中止")
            return

        # 3. 执行核心反馈逻辑
        feedback_result = await profiler.apply_feedback(
            illust=illust,
            action=action,
            config=config.get("feedback", {})
        )
        
        # 如果是"喜欢"，同步添加到 Pixiv 收藏
        if action in ("like", "1"):
             try:
                 await sync_client.add_bookmark(illust_id)
                 
                 # 更新 MAB 策略反馈 (排除连锁推送 related_chain，但统计 MAB 的 related)
                 from database import get_push_source, update_strategy_stats
                 source = await get_push_source(illust_id)
                 if source and source != 'related_chain':
                     await update_strategy_stats(source, is_success=True)
                     logger.info(f"MAB策略 '{source}' 获得正反馈")
                
                 # === Chain Reaction Logic (Per-Image Depth) ===
                 if "related" in config.get("strategies", ["related"]):
                     max_depth = config.get("feedback", {}).get("max_chain_depth", 3)
                     
                     # 从缓存中获取当前作品的链深度和消息 ID
                     chain_depth = cached.get("chain_depth", 0) if cached else 0
                     chain_msg_id = cached.get("chain_msg_id") if cached else None
                     
                     # Fallback: 从 notifier 的消息映射中查找（用于非连锁推送的原图）
                     if chain_msg_id is None:
                         for n in notifiers_list:
                             if hasattr(n, '_message_illust_map'):
                                 # 反查：illust_id -> message_id
                                 for msg_id, ill_id in n._message_illust_map.items():
                                     if ill_id == illust_id:
                                         chain_msg_id = msg_id
                                         break
                             if chain_msg_id:
                                 break
                     
                     # 如果深度未超限，触发新一层连锁
                     next_depth = chain_depth + 1
                     if next_depth <= max_depth:
                         logger.info(f"🔗 触发连锁 (当前深度={chain_depth}, 下一层={next_depth})")
                         asyncio.create_task(push_related_task(
                             illust, 
                             parent_msg_id=chain_msg_id,
                             current_depth=next_depth
                         ))
                     else:
                         logger.info(f"🔗 作品 {illust_id} 连锁深度已达上限 ({chain_depth}/{max_depth})，跳过")
                     
             except Exception as e:
                 logger.error(f"同步收藏/连锁处理失败: {e}")
        
        logger.info(f"反馈处理完成: illust_id={illust_id}, action={action}")
        return feedback_result
    
    # ... (rest of setup_notifiers) ...

            
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
        
        elif action == "run_task":
             # 手动触发推送任务 (Bot 命令跳过队列)
             logger.info("🤖 收到 Bot 手动推送指令 (跳过队列)")
             # 使用 create_task 异步执行，避免阻塞 Bot 响应
             # force=True 跳过队列限制
             # 返回 task 对象以便调用者可以等待任务完成
             task = asyncio.create_task(
                 main_task(
                     config,
                     client,
                     profiler,
                     notifiers,
                     sync_client,
                     force=True,
                     send_summary=False,
                 )
             )
             return task

        elif action == "run_task_historical":
            # 历史补充模式推送
            days = data.get("days", 180) if isinstance(data, dict) else 180
            logger.info(f"🤖 收到 Bot 历史补充推送指令（近{days}天）(跳过队列)")
            task = asyncio.create_task(main_task(
                config, client, profiler, notifiers, sync_client, 
                force=True,
                historical_days=days,
                send_summary=False,
            ))
            return task

        elif action == "get_status":
            # 获取系统状态
            try:
                # Semaphore 没有直接的方法获取当前占用数
                # 我们通过检查 _task_lock 和返回基本信息
                task_running = _task_lock.locked()
                
                # 尝试估计队列使用量（不够精确但够用）
                # 注意：asyncio.Semaphore 没有公开当前计数器
                import sys
                status_info = {
                    "queue_used": "运行中" if task_running else "空闲",
                    "queue_limit": 30,
                    "task_running": task_running,
                }
                return status_info
            except Exception as e:
                logger.warning(f"获取状态失败: {e}")
                return None
             
        elif action == "update_schedule":
            # 更新调度计划 (支持多个时间)
            schedule_str = str(data)
            logger.info(f"📅 收到调度更新请求: {schedule_str}")
            try:
                # 1. 持久化
                from database import set_state
                await set_state("schedule_cron", schedule_str)
                
                # 2. 如果 scheduler 实例存在，重新调度
                if 'scheduler' in config:
                    sched = config['scheduler']
                    
                    # 移除所有旧的 push_job
                    for job in sched.get_jobs():
                        if job.id.startswith('push_job'):
                            sched.remove_job(job.id)
                    
                    # 添加新的任务
                    cron_list = [c.strip() for c in schedule_str.split(",") if c.strip()]
                    for i, cron_expr in enumerate(cron_list):
                        try:
                            sched.add_job(
                                main_task, 
                                CronTrigger.from_crontab(cron_expr),
                                args=[config, client, profiler, notifiers, sync_client],
                                id=f'push_job_{i}'
                            )
                        except Exception as e:
                            logger.error(f"添加任务失败 ({cron_expr}): {e}")
                    
                    logger.info(f"✅ 调度任务已更新，共 {len(cron_list)} 个时间点")
            except Exception as e:
                logger.error(f"更新调度失败: {e}")
    
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
                on_action=on_action,
                proxy_url=tg_cfg.get("proxy_url"),
                max_pages=max_pages,
                image_quality=tg_cfg.get("image_quality", 85),
                max_image_size=tg_cfg.get("max_image_size", 2000),
                topic_rules=tg_cfg.get("topic_rules"),
                topic_tag_mapping=tg_cfg.get("topic_tag_mapping"),
                # 批量模式配置
                batch_mode=tg_cfg.get("batch_mode", "single"),
                batch_show_title=tg_cfg.get("batch_show_title", True),
                batch_show_artist=tg_cfg.get("batch_show_artist", True),
                batch_show_tags=tg_cfg.get("batch_show_tags", True),
                rich_message=tg_cfg.get("rich_message"),
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
                on_feedback=on_feedback,
                on_action=on_action,
                client=client,
                max_pages=max_pages
            )
            try:
                await ob_notifier.connect()
                notifiers.append(ob_notifier)
                logger.info("已启用 OneBot 推送")
            except Exception as e:
                logger.error(f"OneBot 连接失败: {e}")
    
    if "astrbot" in notifier_types:
        ab_cfg = notifier_cfg.get("astrbot", {})
        if ab_cfg.get("http_url") and ab_cfg.get("unified_msg_origin"):
            from notifier.astrbot import AstrBotNotifier
            ab_notifier = AstrBotNotifier(
                http_url=ab_cfg["http_url"],
                unified_msg_origin=ab_cfg["unified_msg_origin"],
                api_key=ab_cfg.get("api_key"),
                on_feedback=on_feedback,
                on_action=on_action,
                client=client,
                max_pages=max_pages,
                image_quality=ab_cfg.get("image_quality", 85),
                max_image_size=ab_cfg.get("max_image_size", 1500)
            )
            notifiers.append(ab_notifier)
            logger.info("已启用 AstrBot 推送")
    
    # 将创建的 notifiers 填充到 notifiers_list (供 push_related_task 等闭包使用)
    notifiers_list.extend(notifiers)
    
    return notifiers if notifiers else None


async def setup_services(config: dict):
    """初始化全局服务 (DB, Client, Profiler, Notifiers)"""
    await init_db()
    
    # 公共网络配置
    network_cfg = config.get("network", {})
    pixiv_cfg = get_singleton_provider(config, "pixiv") or config.get("pixiv", {})
    proxy_url = config.get("notifier", {}).get("telegram", {}).get("proxy_url")
    
    client_kwargs = {
        "requests_per_minute": network_cfg.get("requests_per_minute", 60),
        "random_delay": tuple(network_cfg.get("random_delay", [1.0, 3.0])),
        "max_concurrency": network_cfg.get("max_concurrency", 5),
        "proxy_url": proxy_url
    }
    
    # 主客户端 (用于搜索、排行榜等高风险操作)
    main_client = PixivClient(
        refresh_token=pixiv_cfg.get("refresh_token"),
        **client_kwargs
    )
    await main_client.login()
    
    # 同步客户端 (用于获取收藏、关注动态等低风险操作)
    sync_token = pixiv_cfg.get("sync_token")
    if sync_token:
        sync_client = PixivClient(
            refresh_token=sync_token,
            **client_kwargs
        )
        await sync_client.login()
        logger.info("✅ 已启用同步专用 Token (sync_token)")
    else:
        sync_client = main_client  # 回退到主客户端
        logger.info("未配置 sync_token，收藏同步将使用主 Token")

    # Init Profiler (使用 sync_client，只读操作)
    profiler_cfg = config.get("profiler", {})
    profiler = XPProfiler(
        client=sync_client,  # 使用同步客户端获取收藏
        stop_words=profiler_cfg.get("stop_words"),
        discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
        time_decay_days=profiler_cfg.get("time_decay_days", 180),
        ai_config=resolve_profiler_ai_config(config),
        saturation_threshold=profiler_cfg.get("saturation_threshold", 0.5),
        # Pass IP discount config
        ip_tags=profiler_cfg.get("ip_tags") or profiler_cfg.get("ip_tags_file"),
        ip_weight_discount=profiler_cfg.get("ip_weight_discount", 1.0)
    )
    
    # Init Notifiers (使用 main_client 用于下载图片等，sync_client 用于 on_action 回调)
    notifiers = await setup_notifiers(config, main_client, profiler, sync_client)
    
    # 返回双客户端
    return main_client, sync_client, profiler, notifiers


async def main_task(
    config: dict,
    client: PixivClient,
    profiler: XPProfiler,
    notifiers: list,
    sync_client: PixivClient = None,
    force: bool = False,
    historical_days: int = None,
    send_summary: bool = True,
    summary_title: str = "今日精选推送完成",
) -> PushStats:
    """
    执行一次完整的推送任务 (依赖外部服务)
    
    Args:
        client: 主客户端 (用于搜索、排行榜、下载)
        sync_client: 同步客户端 (用于获取关注动态，可选)
        force: 是否强制跳过队列限制
        historical_days: 历史补充模式的天数 (覆盖配置中的 date_range_days)
    
    Returns:
        PushStats: 推送任务的统计数据
    """
    # 如果未传入 sync_client，使用 main_client
    if sync_client is None:
        sync_client = client
        
    # 队列深度限制：最多排队 30 个触发（强制模式跳过）
    _acquired = False
    if not force:
        try:
            await asyncio.wait_for(_queue_limit.acquire(), timeout=0.001)
            _acquired = True
        except asyncio.TimeoutError:
            logger.warning("⏳ 推送触发过于频繁，队列已满(30)，已拒绝本次触发")
            return PushStats()
        except Exception as e:
            logger.warning(f"⏳ 队列入队失败: {e}")
            return PushStats()
    else:
        logger.info("🚀 强制模式：跳过队列限制")
    
    try:
        if _task_lock.locked():
            logger.info("⏳ 推送任务正在运行中，本次触发已入队等待")
        
        # 创建统计对象（尽早创建，确保所有路径都有）
        stats = create_stats()
        
        async with _task_lock:
            logger.info("=== 开始推送任务 ===")
            await db_module.set_state("runtime.last_run_started_at", datetime.now().isoformat())

        runner = PushRun(
            config=config,
            client=client,
            profiler=profiler,
            notifiers=notifiers,
            sync_client=sync_client,
            stats=stats,
            historical_days=historical_days,
            send_summary=send_summary,
            summary_title=summary_title,
        )
        return await runner.execute()
    finally:
        # 使用标志位确保只在成功 acquire 后才 release，避免竞态条件
        if _acquired:
            try:
                _queue_limit.release()
            except Exception:
                pass


async def run_once(config: dict, force: bool = False) -> PushStats:
    """立即执行一次"""
    main_client, sync_client, profiler, notifiers = await setup_services(config)
    
    # 即使是 Run Once，如果用于测试，可能也需要 Feedback?
    # 但 cli --once 通常是脚本调用，跑完即走。
    # 这里我们还是启动监听 (如果是 Test 模式也许不需要?)
    # 如果是 --test, 我们不启动监听? 
    # 如果用户想测试反馈，OneBot/TG 需要跑。
    # 但 script ends immediately. Feedback needs loop.
    # 所以 --once 真的就是 "Fire and Forget".
    
    try:
        stats = await main_task(config, main_client, profiler, notifiers, sync_client, force=force)
        if stats.push_success_count > 0:
            await _complete_once_maintenance()
        return stats
    finally:
        await main_client.close()
        # 如果 sync_client 是独立实例，也需要关闭
        if sync_client is not main_client:
            await sync_client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'): 
                try: 
                    await n.close() 
                except: 
                    pass


async def _complete_once_maintenance() -> None:
    """Bound one-shot maintenance so shared clients remain usable until it settles."""
    task = get_latest_maintenance_task()
    if task is None:
        return

    done, _ = await asyncio.wait({task}, timeout=MAINTENANCE_WAIT_SECONDS)
    if not done:
        logger.warning("Classification Maintenance 在 %s 秒内未完成，已停止本次尝试", MAINTENANCE_WAIT_SECONDS)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await record_maintenance_completion(MAINTENANCE_TIMEOUT)
        return

    if task.cancelled():
        await record_maintenance_completion(MAINTENANCE_CANCELLED)
        return
    try:
        task.result()
    except Exception as exc:
        await record_maintenance_completion(MAINTENANCE_FAILED, exc)
    else:
        await record_maintenance_completion(MAINTENANCE_SUCCEEDED)

async def daily_report_task(config: dict, notifiers: list, profiler=None):
    """每日维护任务：生成日报 + 数据清理 + AI 标签刷新
    
    设计原则：
    - 每个步骤独立 try/except，即使某一步失败，其他步骤仍可继续
    - 网络相关操作（AI、发送）使用 retry_async 自动重试
    """
    logger.info("📊 开始执行每日维护任务...")
    
    maintenance_summary = []
    lines = ["📊 **每日 XP 日报**\n"]
    
    # ========== 1. 生成日报 (Top Tags + MAB Stats) ==========
    try:
        from database import get_top_xp_tags, get_all_strategy_stats
        
        top_tags = await get_top_xp_tags(10)
        stats = await get_all_strategy_stats()
        
        if top_tags:
            lines.append("🎯 **Top 10 XP 标签**")
            for i, (tag, weight) in enumerate(top_tags[:10], 1):
                lines.append(f"  {i}. `{tag}` ({weight:.1f})")
            lines.append("")
        
        if stats:
            lines.append("📈 **MAB 策略表现**")
            strategy_names = {"search": "XP搜索", "xp_search": "XP搜索", "subscription": "订阅", "ranking": "排行榜"}
            for strategy, data in stats.items():
                name = strategy_names.get(strategy, strategy)
                rate_pct = data["rate"] * 100
                lines.append(f"  • {name}: {data['success']}/{data['total']} ({rate_pct:.1f}%)")
    except Exception as e:
        logger.error(f"生成日报统计失败: {e}")
        maintenance_summary.append(f"⚠️ 日报统计失败: {e}")
    
    # ========== 2. 同步屏蔽标签到 XP 画像 ==========
    try:
        from database import sync_blocked_tags_to_xp
        blocked_removed = await sync_blocked_tags_to_xp()
        if blocked_removed > 0:
            maintenance_summary.append(f"🚫 从画像中移除 {blocked_removed} 个已屏蔽标签")
            logger.info(f"已从 XP 画像中移除 {blocked_removed} 个屏蔽标签")
    except Exception as e:
        logger.error(f"同步屏蔽标签失败: {e}")
        maintenance_summary.append(f"⚠️ 同步屏蔽标签失败: {e}")
    
    # ========== 3. AI 标签增量处理 (带重试) ==========
    if profiler and hasattr(profiler, 'ai_processor') and profiler.ai_processor.enabled:
        try:
            from database import get_uncached_tags
            uncached_tags = await get_uncached_tags(limit=200)
            if uncached_tags:
                logger.info(f"发现 {len(uncached_tags)} 个未处理标签，启动 AI 清洗...")
                
                async def _ai_process():
                    return await profiler.ai_processor.process_tags(uncached_tags)
                
                result = await retry_async(_ai_process, max_retries=3, delay=10.0)
                if result:
                    valid_tags, mapping = result
                    maintenance_summary.append(f"🤖 AI 清洗 {len(uncached_tags)} 个标签 → {len(valid_tags)} 个有效")
                    logger.info(f"AI 清洗完成: {len(valid_tags)}/{len(uncached_tags)} 有效")
                else:
                    maintenance_summary.append(f"⚠️ AI 清洗失败 (已重试)")
        except Exception as e:
            logger.error(f"AI 清洗失败: {e}")
            maintenance_summary.append(f"⚠️ AI 清洗失败: {e}")
    
    # ========== 4. 清理旧推送历史 ==========
    try:
        from database import cleanup_old_sent_history
        old_removed = await cleanup_old_sent_history(days=30)
        if old_removed > 0:
            maintenance_summary.append(f"🗑️ 清理 {old_removed} 条过期推送记录")
            logger.info(f"已清理 {old_removed} 条 30 天前的推送历史")
    except Exception as e:
        logger.error(f"清理推送历史失败: {e}")
        maintenance_summary.append(f"⚠️ 清理推送历史失败: {e}")
    
    # ========== 5. 清理旧作品缓存 ==========
    try:
        from database import cleanup_old_illust_cache
        cache_removed = await cleanup_old_illust_cache(days=60)
        if cache_removed > 0:
            maintenance_summary.append(f"🗑️ 清理 {cache_removed} 条过期作品缓存")
            logger.info(f"已清理 {cache_removed} 条 60 天前的作品缓存")
    except Exception as e:
        logger.error(f"清理作品缓存失败: {e}")
        maintenance_summary.append(f"⚠️ 清理作品缓存失败: {e}")

    # ========== 6. 清理过期作品向量 ==========
    try:
        from database import cleanup_old_embeddings
        embeddings_removed = await cleanup_old_embeddings(days=90)
        if embeddings_removed > 0:
            maintenance_summary.append(f"🗑️ 清理 {embeddings_removed} 条过期作品向量")
            logger.info(f"已清理 {embeddings_removed} 条 90 天前的作品向量")
    except Exception as e:
        logger.error(f"清理作品向量失败: {e}")
        maintenance_summary.append(f"⚠️ 清理作品向量失败: {e}")

    # ========== 7. 添加维护摘要到日报 ==========
    if maintenance_summary:
        lines.append("")
        lines.append("🛠️ **维护记录**")
        for item in maintenance_summary:
            lines.append(f"  {item}")
    
    report_msg = "\n".join(lines)
    
    # ========== 8. 发送日报 (带重试) ==========
    async def _send_report():
        for n in notifiers:
            if hasattr(n, 'send_text'):
                await n.send_text(report_msg)
                return True
        return False
    
    result = await retry_async(_send_report, max_retries=5, delay=30.0, backoff=2.0)
    if not result:
        logger.error("发送日报最终失败")
    
    logger.info("✅ 每日维护任务完成")


async def run_scheduler(config: dict, run_immediately: bool = False):
    """启动调度器 (Daemon Mode)"""
    main_client, sync_client, profiler, notifiers = await setup_services(config)
    scheduler_cfg = config.get("scheduler", {})
    db_cron = await db_module.get_state("schedule_cron")
    config_cron = scheduler_cfg.get("cron", "0 20 * * *")
    schedule_str = db_cron if db_cron else config_cron
    cron_list = _split_schedule_crons(schedule_str)
    min_interval = _get_min_schedule_interval(cron_list)

    if db_cron and db_cron != config_cron:
        logger.warning(
            "检测到数据库中的 schedule_cron (%s) 与 config.yaml 中的 scheduler.cron (%s) 不一致；当前仍使用数据库值，请按需在 Telegram 菜单或配置文件中统一。",
            db_cron,
            config_cron,
        )
    
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
        last_push_at = await _get_last_successful_push_at()
        if _should_skip_immediate_run(last_push_at, min_interval):
            logger.info(
                "⏭️ 跳过 `--now` 立即推送：最近一次成功推送时间为 %s，距离当前不足最短调度间隔 %s",
                last_push_at.isoformat(timespec="seconds") if last_push_at else "unknown",
                min_interval,
            )
        else:
            logger.info("🚀 正在立即执行首次任务...")
            # Run main_task as a background task so it doesn't block scheduler start.
            asyncio.create_task(main_task(config, main_client, profiler, notifiers, sync_client))

    scheduler = AsyncIOScheduler()
    coalesce = scheduler_cfg.get("coalesce", True)
    
    # 将 scheduler 注入到 config 中以便 callback 访问
    config['scheduler'] = scheduler
    
    for i, cron_expr in enumerate(cron_list):
        try:
            scheduler.add_job(
                main_task, 
                CronTrigger.from_crontab(cron_expr),
                args=[config, main_client, profiler, notifiers, sync_client],
                id=f'push_job_{i}',
                coalesce=coalesce,
                misfire_grace_time=3600
            )
            logger.info(f"已添加定时任务 #{i+1}: {cron_expr}")
        except Exception as e:
            logger.error(f"添加定时任务失败 ({cron_expr}): {e}")
    
    # 每日维护任务 (日报 + 清理)
    daily_cron = scheduler_cfg.get("daily_report_cron", "0 0 * * *")  # 默认每天00:00
    try:
        scheduler.add_job(
            daily_report_task,
            CronTrigger.from_crontab(daily_cron),
            args=[config, notifiers, profiler],  # 传入 profiler 以支持 AI 清洗
            id='daily_report_job',
            coalesce=True,
            misfire_grace_time=3600
        )
        logger.info(f"已添加每日维护任务: {daily_cron}")
    except Exception as e:
        logger.error(f"添加每日维护任务失败: {e}")
    
    scheduler.start()
    logger.info(f"调度器已启动，共 {len(cron_list)} 个推送任务 + 1 个每日维护任务")
    
    try:
        while True:
            await asyncio.sleep(1800)  # 每 30 分钟检查一次
            
            # Telegram 连接健康检查
            for n in notifiers:
                if isinstance(n, TelegramNotifier):
                    need_restart = False
                    
                    # 检查 _app 是否存在且 updater 是否在运行
                    if not n._app or not n._app.updater or not n._app.updater.running:
                        logger.warning("Telegram updater 未运行，需要重启...")
                        need_restart = True
                    else:
                        # updater 在运行，检查实际连接
                        try:
                            await n._app.bot.get_me()
                            logger.debug("Telegram 连接健康检查通过")
                        except Exception as e:
                            logger.warning(f"Telegram 健康检查失败: {e}，需要重启轮询...")
                            need_restart = True
                    
                    if need_restart:
                        try:
                            await n.stop_polling()
                            await asyncio.sleep(5)
                            await n.start_polling()
                            logger.info("✅ Telegram 轮询已重启")
                        except Exception as restart_err:
                            logger.error(f"重启 Telegram 轮询失败: {restart_err}")
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
    finally:
        await main_client.close()
        # 如果 sync_client 是独立实例，也需要关闭
        if sync_client is not main_client:
            await sync_client.close()
        for n in (notifiers or []):
            if hasattr(n, 'close'): 
                try:
                    await n.close()
                except:
                    pass
