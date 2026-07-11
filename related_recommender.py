"""Related recommendation orchestration.

This module keeps scheduled related discovery and feedback-driven chain
recommendations behind one interface while preserving the existing behavior.
"""

import asyncio
import logging
import random
from typing import Optional

import database as db
from filter import ContentFilter
from pixiv_client import Illust, PixivClient
from tag_classifier import TagClassifier

logger = logging.getLogger(__name__)


def _get_display_tags_max_ip_count(filter_cfg: dict) -> int:
    display_tags_cfg = filter_cfg.get("display_tags", {}) if isinstance(filter_cfg, dict) else {}
    if not isinstance(display_tags_cfg, dict):
        return 2
    return display_tags_cfg.get("max_ip_count", 2)


class RelatedRecommender:
    """Owns both scheduled and feedback-chain related recommendations."""

    def __init__(
        self,
        client: PixivClient,
        config: Optional[dict] = None,
        profiler=None,
        sync_client: Optional[PixivClient] = None,
        bookmark_threshold: Optional[dict[str, int]] = None,
        processing_set: Optional[set[int]] = None,
        processing_lock: Optional[asyncio.Lock] = None,
    ):
        self.client = client
        self.sync_client = sync_client or client
        self.config = config or {}
        self.profiler = profiler
        fetcher_cfg = self.config.get("fetcher", {}) if isinstance(self.config, dict) else {}
        self.bookmark_threshold = bookmark_threshold or fetcher_cfg.get("bookmark_threshold") or {}
        self.processing_set = processing_set if processing_set is not None else set()
        self.processing_lock = processing_lock or asyncio.Lock()

    async def discover_for_strategy(self, xp_tags: list[tuple[str, float]], limit: int = 50) -> list[Illust]:
        """Discover related works for the scheduled MAB related strategy."""
        if not xp_tags:
            return []

        top_tags = xp_tags[:20]
        if not top_tags:
            return []

        tags, weights = zip(*top_tags)
        seed_tag = random.choices(tags, weights=weights, k=1)[0]

        try:
            liked_ids = await db.get_liked_illusts()
            if not liked_ids:
                return []

            seed_illust_id = random.choice(list(liked_ids))
            logger.info(f"关联策略: 选中种子作品 {seed_illust_id} (Tag: {seed_tag})")
        except Exception as e:
            logger.warning(f"关联策略选种失败: {e}")
            return []

        try:
            raw_related = await self.client.get_related_illusts(seed_illust_id, limit=limit * 2)
        except Exception as e:
            logger.error(f"获取关联作品失败: {e}")
            return []

        scored_candidates = []
        xp_dict = dict(xp_tags)
        bookmark_threshold_related = self.bookmark_threshold.get("related", 0)

        for illust in raw_related:
            if bookmark_threshold_related > 0 and illust.bookmark_count < bookmark_threshold_related:
                logger.debug(
                    f"关联策略: 作品 {illust.id} 收藏数 {illust.bookmark_count} < 阈值 {bookmark_threshold_related}，跳过"
                )
                continue

            score = 0.0
            for tag in illust.tags:
                norm = tag.lower().replace(" ", "_")
                if norm in xp_dict:
                    score += xp_dict[norm]

            artist_score = await db.get_artist_score(illust.user_id)
            score += artist_score
            scored_candidates.append((illust, score))

        scored_candidates.sort(key=lambda x: x[1], reverse=True)

        details = [f"{ill.id}({sc:.1f})" for ill, sc in scored_candidates[:5]]
        logger.info(f"关联推荐结果: {details}...")

        return [x[0] for x in scored_candidates[:limit]]

    async def push_chain(
        self,
        seed_illust: Illust,
        notifiers: list,
        parent_msg_id: int = None,
        current_depth: int = 1,
    ) -> None:
        """Push feedback-triggered related recommendations."""
        try:
            logger.info(f"🔗 触发连锁反应 (深度={current_depth}): 正在获取 {seed_illust.id} 的关联作品...")
            typing_task = self._start_typing_indicator(notifiers)
            try:
                related = await self.client.get_related_illusts(seed_illust.id, limit=20)
                if not related:
                    logger.info(f"🔗 作品 {seed_illust.id} 无关联推荐，连锁结束")
                    return

                content_filter = self._build_chain_filter()
                xp_profile = await db.get_xp_profile()
                filtered = await self.discover_for_chain(seed_illust, related, content_filter, xp_profile)

                push_limit = self.config.get("feedback", {}).get("related_push_limit", 1)
                top_results = [x[0] for x in filtered[:push_limit]]

                if top_results:
                    await content_filter.apply_display_tags(top_results, xp_profile)

                    source_title = getattr(seed_illust, "title", f"#{seed_illust.id}")
                    message_prefix = f"🔗 连锁推荐 (源自: {source_title})"

                    logger.info(f"🔗 连锁推送: {len(top_results)} 个关联作品")
                    await self._push_chain_results(
                        top_results,
                        notifiers,
                        message_prefix=message_prefix,
                        parent_msg_id=parent_msg_id,
                        current_depth=current_depth,
                        seed_illust=seed_illust,
                    )
                else:
                    logger.info("🔗 关联作品过滤后为空")
            finally:
                if typing_task:
                    typing_task.cancel()
        except Exception as e:
            logger.error(f"连锁推送失败: {e}")

    async def discover_for_chain(
        self,
        seed_illust: Illust,
        related: list[Illust],
        content_filter: ContentFilter,
        xp_profile: Optional[dict[str, float]] = None,
    ) -> list[tuple[Illust, float]]:
        """Filter and score feedback-chain related candidates."""
        filtered = []
        seen_ids = set()
        if xp_profile is None:
            xp_profile = await db.get_xp_profile()

        bt_cfg = self.bookmark_threshold or {}
        bookmark_threshold_related = bt_cfg.get("related", 0) if isinstance(bt_cfg, dict) else 0

        for ill in related:
            if bookmark_threshold_related > 0 and ill.bookmark_count < bookmark_threshold_related:
                logger.debug(f"🔗 作品 {ill.id} 收藏数 {ill.bookmark_count} < 阈值 {bookmark_threshold_related}，跳过")
                continue

            try:
                if ill.id and seed_illust.id and int(ill.id) == int(seed_illust.id):
                    continue
            except (ValueError, TypeError) as e:
                logger.warning(f"ID 类型转换失败: ill.id={ill.id}, seed_illust.id={seed_illust.id}: {e}")
                continue

            if ill.id in seen_ids:
                continue
            seen_ids.add(ill.id)

            async with self.processing_lock:
                if ill.id in self.processing_set:
                    logger.debug(f"🔗 作品 {ill.id} 正在被其他关联推送任务处理，跳过")
                    continue
                self.processing_set.add(ill.id)

            try:
                if await db.is_pushed(ill.id):
                    logger.debug(f"🔗 作品 {ill.id} 已推送过，跳过推荐")
                    continue
                if not content_filter.check_illust(ill):
                    continue
                if self.profiler and ill.user_id in getattr(self.profiler, "_blocked_artist_ids", set()):
                    continue

                score = 0
                for tag in ill.tags:
                    norm = tag.lower().replace(" ", "_")
                    if norm in xp_profile:
                        score += xp_profile[norm]

                artist_score = await db.get_artist_score(ill.user_id)
                score += artist_score

                filtered.append((ill, score))
            finally:
                async with self.processing_lock:
                    self.processing_set.discard(ill.id)

        filtered.sort(key=lambda x: x[1], reverse=True)
        return filtered

    def _build_chain_filter(self) -> ContentFilter:
        filter_cfg = self.config.get("filter", {})
        profiler_cfg = self.config.get("profiler", {})
        tag_classifier = None
        try:
            classifier_config = dict(self.config.get("tag_classifier", {}))
            classifier_config["providers"] = self.config.get("providers", {})
            classifier_config["models"] = self.config.get("models", {})
            tag_classifier = TagClassifier(
                classifier_config,
                ip_tags=profiler_cfg.get("ip_tags") or profiler_cfg.get("ip_tags_file"),
            )
        except Exception as e:
            logger.warning(f"TagClassifier 初始化失败，连锁推送将仅使用 XP 排序: {e}")

        stop_words = list(getattr(self.profiler, "stop_words", [])) if self.profiler else []
        return ContentFilter(
            blacklist_tags=stop_words,
            exclude_ai=filter_cfg.get("exclude_ai", True),
            r18_mode=filter_cfg.get("r18_mode", False),
            min_create_days=filter_cfg.get("min_create_days", 0),
            skip_ugoira=filter_cfg.get("skip_ugoira", False),
            content_type=filter_cfg.get("content_type", "all"),
            tag_classifier=tag_classifier,
            display_tags_max_ip_count=_get_display_tags_max_ip_count(filter_cfg),
        )

    def _start_typing_indicator(self, notifiers: list):
        if not notifiers:
            return None

        for notifier in notifiers:
            if not hasattr(notifier, "_keep_typing") or not getattr(notifier, "chat_ids", None):
                continue
            try:
                chat_id = int(notifier.chat_ids[0]) if notifier.chat_ids else None
                if chat_id:
                    return asyncio.create_task(notifier._keep_typing(chat_id))
            except (ValueError, TypeError) as e:
                logger.warning(f"无效的 chat_id: {notifier.chat_ids[0] if notifier.chat_ids else None}: {e}")
            break
        return None

    async def _push_chain_results(
        self,
        top_results: list[Illust],
        notifiers: list,
        message_prefix: str,
        parent_msg_id: int,
        current_depth: int,
        seed_illust: Illust,
    ) -> None:
        for notifier in notifiers:
            if not hasattr(notifier, "push_illusts"):
                continue

            sent_map = await notifier.push_illusts(
                top_results,
                message_prefix=message_prefix,
                reply_to_message_id=parent_msg_id,
            )

            for ill in top_results:
                msg_id = sent_map.get(ill.id)
                await db.cache_illust(
                    illust_id=ill.id,
                    tags=ill.tags,
                    user_id=ill.user_id,
                    user_name=ill.user_name,
                    source="related_chain",
                    chain_depth=current_depth,
                    chain_parent_id=seed_illust.id,
                    chain_msg_id=msg_id,
                )
                await db.mark_pushed(ill.id, "related_chain")
