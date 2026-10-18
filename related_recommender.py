"""Related recommendation orchestration.

This module keeps scheduled related discovery and feedback-driven chain
recommendations behind one interface while preserving the existing behavior.
"""

import asyncio
import logging
from typing import Optional

import database as db
from filter import ContentFilter
from pixiv_client import Illust, PixivClient
from tag_classifier import TagClassifier

logger = logging.getLogger(__name__)

# One round may try at most two historical seeds and one related read per seed.
MAX_SEEDS_PER_ROUND = 2


def _is_rate_limit_error(error: Exception) -> bool:
    """Treat HTTP 429/Too Many Requests as a throttle, never an immediate retry."""
    for attribute in ("status", "status_code", "http_status"):
        if getattr(error, attribute, None) == 429:
            return True
    response = getattr(error, "response", None)
    for attribute in ("status", "status_code"):
        if getattr(response, attribute, None) == 429:
            return True
    message = str(error).lower()
    return "429" in message or "too many requests" in message or "rate limit" in message


def _get_display_tags_max_ip_count(filter_cfg: dict) -> int:
    display_tags_cfg = filter_cfg.get("display_tags", {}) if isinstance(filter_cfg, dict) else {}
    if not isinstance(display_tags_cfg, dict):
        return 2
    return display_tags_cfg.get("max_ip_count", 2)


def _confirmed_deliveries(sent_map) -> dict[int, int | None]:
    """仅接受作品 ID 与非布尔正整数 Telegram 消息号的映射。"""
    if not isinstance(sent_map, dict):
        if sent_map is not None:
            logger.warning(
                f"🔗 推送器返回了无法识别的投递结果 {type(sent_map).__name__}，视为未确认投递"
            )
        return {}

    confirmed: dict[int, int | None] = {}
    for illust_id, message_id in sent_map.items():
        if not isinstance(illust_id, int) or isinstance(illust_id, bool):
            logger.warning(f"🔗 忽略无法识别的投递记录 ID: {illust_id!r}")
            continue
        if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0:
            confirmed[illust_id] = message_id
        else:
            logger.warning(f"🔗 作品 {illust_id} 消息 ID 无效，不确认投递")
            continue
    return confirmed


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

    async def _finish_seed_claim(self, illust_id: int, claim_token: str, outcome: str) -> bool:
        """Release one seed lease; a storage error must not crash the strategy."""
        try:
            return await db.finish_related_seed(illust_id, claim_token, outcome)
        except Exception as e:
            logger.warning(f"关联策略: 种子租约结算失败 ({outcome})，交由租约过期回收: {e}")
            return False

    async def discover_for_strategy(self, xp_tags: list[tuple[str, float]], limit: int = 50) -> list[Illust]:
        """Discover related works from a rotating pool of historical positive seeds.

        Seed acquisition is guarded by durable leases and cooldowns. A success is
        only a successful *candidate* fetch, never a delivery confirmation.
        """
        if limit <= 0 or not xp_tags:
            return []

        top_tags = xp_tags[:20]
        if not top_tags:
            return []

        try:
            claims = await db.claim_related_seeds(max_seeds=MAX_SEEDS_PER_ROUND)
        except Exception as e:
            # No fallback to the old random path: it would bypass cooldowns.
            logger.warning(f"关联策略: 历史种子租约获取失败，本轮不请求关联作品: {e}")
            return []

        if not claims:
            logger.info("关联策略: 历史种子池当前无可轮换种子")
            return []

        round_seed_ids = {int(claim["illust_id"]) for claim in claims}
        unfinished = {int(claim["illust_id"]): claim for claim in claims}
        scored_candidates: list[tuple[Illust, float]] = []
        seen_ids: set[int] = set()
        xp_dict = dict(xp_tags)
        bookmark_threshold_related = self.bookmark_threshold.get("related", 0)

        try:
            for claim in claims:
                seed_illust_id = int(claim["illust_id"])
                claim_token = claim["claim_token"]

                try:
                    # Hard budget: one page and at most one underlying related
                    # request per seed. A 429 must bubble up, never look empty.
                    # Token refresh is separate from this artwork read budget.
                    raw_related = await self.client.get_related_illusts(
                        seed_illust_id,
                        limit=limit * 2,
                        single_page=True,
                        no_retry=True,
                    )
                except Exception as e:
                    unfinished.pop(seed_illust_id, None)
                    if _is_rate_limit_error(e):
                        await self._finish_seed_claim(seed_illust_id, claim_token, "throttled")
                        logger.warning("关联策略: 关联接口限流，本轮停止并进入短冷却")
                        break
                    await self._finish_seed_claim(seed_illust_id, claim_token, "error")
                    logger.warning(f"关联策略: 获取关联作品失败，跳过该历史种子: {e}")
                    continue

                unfinished.pop(seed_illust_id, None)
                if not raw_related:
                    await self._finish_seed_claim(seed_illust_id, claim_token, "empty")
                    logger.info("关联策略: 历史种子暂无关联作品，进入退避冷却")
                    continue

                if not await self._finish_seed_claim(seed_illust_id, claim_token, "success"):
                    logger.warning("关联策略: 种子租约已被新请求接管，丢弃本轮候选")
                    continue

                try:
                    for illust in raw_related:
                        if illust.id in round_seed_ids or illust.id in seen_ids:
                            continue
                        seen_ids.add(illust.id)

                        if (
                            bookmark_threshold_related > 0
                            and illust.bookmark_count < bookmark_threshold_related
                        ):
                            logger.debug(
                                f"关联策略: 候选收藏数 {illust.bookmark_count} < 阈值 "
                                f"{bookmark_threshold_related}，跳过"
                            )
                            continue

                        score = 0.0
                        for tag in illust.tags:
                            norm = tag.lower().replace(" ", "_")
                            if norm in xp_dict:
                                score += xp_dict[norm]

                        score += await db.get_artist_score(illust.user_id)
                        scored_candidates.append((illust, score))
                except Exception as e:
                    logger.error(f"关联策略: 候选打分失败，本轮提前结束: {e}")
                    break

                if len(scored_candidates) >= limit:
                    break
        finally:
            for remaining in unfinished.values():
                await self._finish_seed_claim(
                    int(remaining["illust_id"]), remaining["claim_token"], "unused"
                )

        scored_candidates.sort(key=lambda x: x[1], reverse=True)
        logger.info(
            f"关联策略: 使用 {len(claims) - len(unfinished)} 个历史种子，"
            f"候选 {len(scored_candidates)} 个"
        )
        return [illust for illust, _ in scored_candidates[:limit]]

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
        """推送连锁候选并按确认结果落库。

        只有 notifier 确认送达的作品才写入推送历史与永久曝光表；未确认的候选
        仍可缓存为连锁候选（chain_msg_id 记 None），从而保持可再次推荐。
        """
        if not top_results:
            logger.info("🔗 连锁推荐结果为空，跳过推送")
            return

        requested_ids = [ill.id for ill in top_results]
        confirmed_msg_ids: dict[int, int | None] = {}

        for notifier in notifiers:
            if not hasattr(notifier, "push_illusts"):
                continue

            try:
                sent_map = await notifier.push_illusts(
                    top_results,
                    message_prefix=message_prefix,
                    reply_to_message_id=parent_msg_id,
                )
            except Exception as e:
                logger.error(
                    f"🔗 推送器 {type(notifier).__name__} 连锁推送异常，该推送器结果视为未确认: {e}"
                )
                continue

            confirmed = _confirmed_deliveries(sent_map)
            for illust_id in requested_ids:
                if illust_id not in confirmed:
                    continue
                # 多推送器场景下保留第一个有效消息 ID，副作用只落库一次。
                if illust_id in confirmed_msg_ids and confirmed_msg_ids[illust_id] is not None:
                    continue
                confirmed_msg_ids[illust_id] = confirmed[illust_id]

        unconfirmed_ids = [illust_id for illust_id in requested_ids if illust_id not in confirmed_msg_ids]
        if unconfirmed_ids:
            logger.warning(
                f"🔗 连锁推送未确认 {len(unconfirmed_ids)}/{len(requested_ids)} 个作品，"
                f"不计入推送历史/曝光: {unconfirmed_ids}"
            )

        for ill in top_results:
            is_confirmed = ill.id in confirmed_msg_ids
            await db.cache_illust(
                illust_id=ill.id,
                tags=ill.tags,
                user_id=ill.user_id,
                user_name=ill.user_name,
                source="related_chain",
                chain_depth=current_depth,
                chain_parent_id=seed_illust.id,
                chain_msg_id=confirmed_msg_ids.get(ill.id) if is_confirmed else None,
            )
            if is_confirmed:
                await db.mark_pushed(ill.id, "related_chain")
