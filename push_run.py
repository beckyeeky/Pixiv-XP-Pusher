"""Single push-run orchestration."""

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime
from typing import Optional

import database as db_module
from config import resolve_model
from database import cache_illust, mark_pushed
from fetcher import ContentFetcher
from filter import ContentFilter
from pixiv_client import PixivClient
from profiler import XPProfiler
from push_stats import PushStats
from tag_classifier import TagClassifier

logger = logging.getLogger(__name__)

MAINTENANCE_COMPLETION_STATE = "runtime.last_maintenance_completion"
MAINTENANCE_BACKGROUND_STATUS_STATE = "runtime.last_maintenance_background_status"
MAINTENANCE_SUCCEEDED = "succeeded"
MAINTENANCE_FAILED = "failed"
MAINTENANCE_TIMEOUT = "timeout"
MAINTENANCE_CANCELLED = "cancelled"
_active_maintenance_task: asyncio.Task | None = None
_latest_maintenance_task: asyncio.Task | None = None


async def _record_maintenance_status(
    state_key: str, status: str, error: Exception | None = None
) -> None:
    payload = {"status": status, "completed_at": datetime.now().isoformat()}
    if error is not None:
        payload["error"] = str(error)
    try:
        await db_module.set_state(state_key, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:
        logger.warning("无法记录 Classification Maintenance 状态: %s", exc)


async def record_maintenance_completion(status: str, error: Exception | None = None) -> None:
    """Persist settled Maintenance Completion after a delivered Daily Slate."""
    await _record_maintenance_status(MAINTENANCE_COMPLETION_STATE, status, error)


async def record_maintenance_background_status(status: str, error: Exception | None = None) -> None:
    """Persist background maintenance status without claiming delivery completion."""
    await _record_maintenance_status(MAINTENANCE_BACKGROUND_STATUS_STATE, status, error)


def get_active_maintenance_task() -> asyncio.Task | None:
    """Return the one background Classification Maintenance task, if active."""
    if _active_maintenance_task is not None and not _active_maintenance_task.done():
        return _active_maintenance_task
    return None


def get_latest_maintenance_task() -> asyncio.Task | None:
    """Return the most recently started maintenance attempt, even if settled."""
    return _latest_maintenance_task


def start_profile_maintenance(classifier: TagClassifier, profile: dict) -> asyncio.Task:
    """Start one maintenance attempt, reusing an active attempt across deliveries."""
    global _active_maintenance_task, _latest_maintenance_task
    active_task = get_active_maintenance_task()
    if active_task is not None:
        logger.info("Classification Maintenance 已在后台运行，复用当前任务")
        return active_task

    task = asyncio.create_task(classifier.maintain_profile_tags(profile))
    _active_maintenance_task = task
    _latest_maintenance_task = task

    def _on_done(completed_task: asyncio.Task) -> None:
        global _active_maintenance_task
        if _active_maintenance_task is completed_task:
            _active_maintenance_task = None
        if completed_task.cancelled():
            return
        try:
            completed_task.result()
        except Exception as exc:
            logger.warning("标签分类维护失败，将在下次运行重试: %s", exc)
            asyncio.create_task(record_maintenance_background_status(MAINTENANCE_FAILED, exc))
        else:
            logger.info("Classification Maintenance 已完成")
            asyncio.create_task(record_maintenance_background_status(MAINTENANCE_SUCCEEDED))

    task.add_done_callback(_on_done)
    return task


def _get_display_tags_max_ip_count(filter_cfg: dict) -> int:
    display_tags_cfg = filter_cfg.get("display_tags", {}) if isinstance(filter_cfg, dict) else {}
    if not isinstance(display_tags_cfg, dict):
        return 2
    return display_tags_cfg.get("max_ip_count", 2)


class PushRun:
    """Runs one complete recommendation, filter, and push cycle."""

    def __init__(
        self,
        config: dict,
        client: PixivClient,
        profiler: XPProfiler,
        notifiers: list,
        stats: PushStats,
        sync_client: Optional[PixivClient] = None,
        historical_days: int = None,
        send_summary: bool = True,
        summary_title: str = "今日精选推送完成",
    ):
        self.config = config
        self.client = client
        self.sync_client = sync_client or client
        self.profiler = profiler
        self.notifiers = notifiers
        self.stats = stats
        self.historical_days = historical_days
        self.send_summary = send_summary
        self.summary_title = summary_title

    async def execute(self) -> PushStats:
        try:
            await self.client.login()
            if self.sync_client and self.sync_client is not self.client:
                await self.sync_client.login()
        except Exception as e:
            logger.warning(f"Token 刷新失败: {e}")

        try:
            profiler_cfg = self.config.get("profiler", {})

            await self.profiler.build_profile(
                user_id=self.config["pixiv"]["user_id"],
                scan_limit=profiler_cfg.get("scan_limit", 500),
                include_private=profiler_cfg.get("include_private", True),
            )

            top_tags = await self.profiler.get_top_tags(profiler_cfg.get("top_n", 20))
            logger.info(f"Top XP Tags: {[t[0] for t in top_tags[:10]]}")

            if self.config.get("test"):
                pass

            xp_profile = await db_module.get_xp_profile()
            fetcher_cfg = self.config.get("fetcher", {})

            following_ids = set()
            pixiv_uid = self.config.get("pixiv", {}).get("user_id", 0)
            if pixiv_uid:
                try:
                    following_ids = await self.sync_client.fetch_following(user_id=pixiv_uid)
                except Exception as e:
                    logger.warning(f"获取关注列表失败: {e}")

            manual_subs = set(fetcher_cfg.get("subscribed_artists") or [])
            all_subs = list(following_ids | manual_subs)
            logger.info(f"有效关注画师数: {len(all_subs)} (API获取: {len(following_ids)}, 手动: {len(manual_subs)})")

            fetcher_date_range = (
                self.historical_days
                if self.historical_days is not None
                else fetcher_cfg.get("date_range_days", 7)
            )
            if self.historical_days is not None:
                logger.info(f"📚 历史补充模式：时间范围调整为 {self.historical_days} 天 (实际使用: {fetcher_date_range})")

            tag_classifier = self._build_tag_classifier(profiler_cfg)
            if tag_classifier and xp_profile:
                self.maintenance_task = start_profile_maintenance(tag_classifier, xp_profile)

            fetcher = ContentFetcher(
                client=self.client,
                sync_client=self.sync_client,
                config=self.config,
                bookmark_threshold=fetcher_cfg.get("bookmark_threshold", {"search": 1000, "subscription": 0}),
                date_range_days=fetcher_date_range,
                subscribed_artists=list(manual_subs),
                discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
                ranking_config=fetcher_cfg.get("ranking"),
                dynamic_threshold_config=fetcher_cfg.get("dynamic_threshold"),
                search_limit=fetcher_cfg.get("search_limit", 50),
                tag_classifier=tag_classifier,
            )

            top_tags = await self.profiler.get_top_tags(profiler_cfg.get("top_n", 20))
            top_tags = await self.profiler.get_top_tags(profiler_cfg.get("top_n", 20))

            all_illusts = await fetcher.fetch_content(
                xp_tags=top_tags,
                total_limit=fetcher_cfg.get("discovery_limit", 200),
            )
            logger.info(f"共获取 {len(all_illusts)} 个候选作品")

            source_counts = Counter(getattr(ill, "source", "unknown") for ill in all_illusts)
            for source, count in source_counts.items():
                self.stats.record_fetch(source, count)

            self.stats.record_filter_start(len(all_illusts))

            filter_cfg = self.config.get("filter", {})
            match_cfg = fetcher_cfg.get("match_score", {})
            if self.historical_days is not None:
                filter_cfg = dict(filter_cfg)
                filter_cfg["min_create_days"] = 0
                logger.info("📚 历史补充模式：min_create_days 临时设为 0")

            embedder = self._build_embedder()
            ai_scorer = self._build_ai_scorer()

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
                r18_mode=filter_cfg.get("r18_mode", False),
                skip_ugoira=filter_cfg.get("skip_ugoira", False),
                content_type=filter_cfg.get("content_type", "all"),
                author_diversity=filter_cfg.get("author_diversity"),
                ip_diversity=filter_cfg.get("ip_diversity"),
                source_boost=filter_cfg.get("source_boost"),
                embedder=embedder,
                ai_scorer=ai_scorer,
                shuffle_factor=filter_cfg.get("shuffle_factor", 0.0),
                exploration_ratio=filter_cfg.get("exploration_ratio", 0.0),
                daily_slate=filter_cfg.get("daily_slate"),
                tag_classifier=tag_classifier,
                display_tags_max_ip_count=_get_display_tags_max_ip_count(filter_cfg),
            )

            pixiv_uid = self.config.get("pixiv", {}).get("user_id", 0)
            filtered = await content_filter.filter(all_illusts, xp_profile=xp_profile, user_id=pixiv_uid)
            logger.info(f"过滤后 {len(filtered)} 个作品")

            self.stats.record_filter_end(len(filtered))

            if hasattr(content_filter, "_last_filter_reasons"):
                for reason, count in content_filter._last_filter_reasons.items():
                    self.stats.record_filter_reason(reason, count)

            self.stats.record_ai_enabled(
                semantic_match=embedder is not None and embedder.enabled,
                scorer=ai_scorer is not None and ai_scorer.enabled,
            )

            await self._push_filtered(filtered)
        except Exception as e:
            logger.error(f"任务执行出错: {e}", exc_info=True)

        await self._write_summary()
        logger.info("=== 推送任务结束 ===")
        return self.stats

    def _build_embedder(self):
        embedder = None
        ai_cfg = self.config.get("ai", {})
        embedding_cfg = ai_cfg.get("embedding", {})
        if embedding_cfg.get("enabled", False):
            try:
                from embedder import Embedder

                model_ref = embedding_cfg.get("model")
                if model_ref in self.config.get("models", {}):
                    embedding_cfg = {
                        **embedding_cfg,
                        **resolve_model(self.config, model_ref, "embedding"),
                    }
                embedder = Embedder(embedding_cfg)
                if embedder.enabled:
                    logger.info(f"已启用 AI 语义匹配 (model={embedder.model})")
            except Exception as e:
                logger.warning(f"Embedder 初始化失败: {e}")
        return embedder

    def _build_ai_scorer(self):
        ai_scorer = None
        ai_cfg = self.config.get("ai", {})
        scorer_cfg = ai_cfg.get("scorer", {})
        if scorer_cfg.get("enabled", False):
            try:
                from ai_scorer import AIScorer

                model_ref = scorer_cfg.get("model")
                if model_ref in self.config.get("models", {}):
                    scorer_cfg = {
                        **scorer_cfg,
                        **resolve_model(self.config, model_ref, "llm"),
                    }
                    ai_scorer = AIScorer(scorer_cfg)
                elif scorer_cfg.get("use_profiler_api", True):
                    profiler_ai_cfg = self.config.get("profiler", {}).get("ai", {})
                    merged_cfg = {
                        "enabled": scorer_cfg.get("enabled", False),
                        "provider": scorer_cfg.get("provider") or profiler_ai_cfg.get("provider", "openai"),
                        "api_key": scorer_cfg.get("api_key") or profiler_ai_cfg.get("api_key", ""),
                        "base_url": scorer_cfg.get("base_url") or profiler_ai_cfg.get("base_url", ""),
                        "model": scorer_cfg.get("model") or profiler_ai_cfg.get("model", "gpt-4o-mini"),
                        "max_candidates": scorer_cfg.get("max_candidates", 50),
                        "score_weight": scorer_cfg.get("score_weight", 0.3),
                    }
                    ai_scorer = AIScorer(merged_cfg)
                else:
                    ai_scorer = AIScorer(scorer_cfg)

                if ai_scorer.enabled:
                    logger.info(f"已启用 AI 精排评分 (model={ai_scorer.model})")
            except Exception as e:
                logger.warning(f"AIScorer 初始化失败: {e}")
        return ai_scorer

    def _build_tag_classifier(self, profiler_cfg: dict):
        try:
            classifier_config = dict(self.config.get("tag_classifier", {}))
            classifier_config["providers"] = self.config.get("providers", {})
            classifier_config["models"] = self.config.get("models", {})
            return TagClassifier(
                classifier_config,
                ip_tags=profiler_cfg.get("ip_tags") or profiler_cfg.get("ip_tags_file"),
            )
        except Exception as e:
            logger.warning(f"TagClassifier 初始化失败，将仅使用 XP 排序: {e}")
            return None

    async def _push_filtered(self, filtered: list) -> None:
        if self.notifiers and filtered:
            try:
                for illust in filtered:
                    await cache_illust(illust.id, illust.tags, illust.user_id, illust.user_name, source=illust.source)

                delivered_ids = set()
                queued_ids = set()
                for notifier in self.notifiers:
                    try:
                        if getattr(type(notifier), "send_with_result", None):
                            result = await notifier.send_with_result(filtered)
                            delivered_ids.update(result.delivered_ids)
                            queued_ids.update(result.queued_ids)
                        else:
                            sent_ids = await notifier.send(filtered)
                            delivered_ids.update(sent_ids)
                    except Exception as e:
                        logger.error(f"推送器 {type(notifier).__name__} 发送失败: {e}")

                if queued_ids:
                    logger.info(f"有 {len(queued_ids)} 个作品已进入发送队列，等待后续投递确认")
                    for pid in queued_ids - delivered_ids:
                        if any(ill.id == pid for ill in filtered):
                            self.stats.record_push_queued()

                if delivered_ids:
                    filtered_map = {ill.id: ill for ill in filtered}
                    for pid in delivered_ids:
                        if pid in filtered_map:
                            illust = filtered_map[pid]
                            source = getattr(illust, "source", "unknown")
                            self.stats.record_push_success(source)
                        else:
                            logger.warning(f"收到未匹配的推送结果 ID: {pid}，跳过统计归因")

                if delivered_ids:
                    filtered_map = {ill.id: ill for ill in filtered}
                    for pid in delivered_ids:
                        if pid in filtered_map:
                            illust = filtered_map[pid]
                            source = getattr(illust, "source", "unknown")
                            await mark_pushed(pid, source)

                            if source in ["xp_search", "subscription", "ranking", "related", "engagement_artists"]:
                                await db_module.update_strategy_stats(source, is_success=False)
                    await db_module.set_state("runtime.last_successful_push_at", datetime.now().isoformat())

                    for notifier in self.notifiers:
                        if hasattr(notifier, "_message_illust_map"):
                            for msg_id, illust_id in notifier._message_illust_map.items():
                                if illust_id in delivered_ids:
                                    await db_module.set_chain_meta(illust_id, chain_depth=0, chain_msg_id=msg_id)

                    logger.info(f"推送完成: {len(delivered_ids)}/{len(filtered)} 个作品成功")

                    unresolved_queued_count = len(queued_ids - delivered_ids)
                    failed_count = len(filtered) - len(delivered_ids) - unresolved_queued_count
                    if failed_count > 0:
                        for _ in range(failed_count):
                            self.stats.record_push_failed()
                elif queued_ids:
                    logger.warning("作品已进入发送队列，但尚未确认任何作品送达")
                else:
                    logger.error("没有任何作品被成功推送")
                    for _ in range(len(filtered)):
                        self.stats.record_push_failed()

            except Exception as e:
                logger.error(f"推送过程出错: {e}")
                for _ in range(len(filtered)):
                    self.stats.record_push_failed()
        elif not filtered:
            logger.info("无新作品可推送")
        else:
            logger.warning("未配置推送器")

    async def _write_summary(self) -> None:
        summary = {
            "finished_at": datetime.now().isoformat(),
            "fetch_count": getattr(self.stats, "fetch_total", 0),
            "filtered_count": getattr(self.stats, "filter_after_count", 0),
            "pushed": getattr(self.stats, "push_success_count", 0),
            "queued": getattr(self.stats, "push_queued_count", 0),
            "failed": getattr(self.stats, "push_failed_count", 0),
        }
        await db_module.set_state("runtime.last_run_summary", json.dumps(summary, ensure_ascii=False))
        logger.info("运行摘要: %s", summary)
        if self.send_summary and self.notifiers and hasattr(self.stats, "format_report"):
            try:
                report = self.stats.format_report()
                if self.summary_title != "今日精选推送完成":
                    report = report.replace("今日精选推送完成", self.summary_title)
                for notifier in self.notifiers:
                    if hasattr(notifier, "send_text"):
                        await notifier.send_text(report)
                        break
            except Exception as e:
                logger.warning(f"发送运行摘要失败: {e}")
