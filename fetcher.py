"""
内容获取模块
双策略：XP搜索 + 画师订阅 + 排行榜
"""
import logging
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional

from pixiv_client import Illust, PixivClient
import database as db
from related_recommender import RelatedRecommender
from utils import expand_search_query

logger = logging.getLogger(__name__)


class ContentFetcher:
    """内容获取器"""
    
    def __init__(
        self,
        client: PixivClient,
        config: Optional[dict] = None,
        bookmark_threshold: dict[str, int] = None,
        date_range_days: int = 7,
        subscribed_artists: Optional[list[int]] = None,
        discovery_rate: float = 0.1,
        ranking_config: Optional[dict] = None,
        mab_limits: Optional[dict] = None,
        sync_client: PixivClient = None,
        dynamic_threshold_config: Optional[dict] = None,  # 动态阈值配置
        search_limit: int = 50,  # 默认搜索数量
        tag_classifier = None,
    ):
        self.client = client  # 主客户端 (搜索、排行榜)
        self.sync_client = sync_client or client  # 同步客户端 (订阅、关注)
        self.config = config or {}
        self.bookmark_threshold = bookmark_threshold or {"search": 1000, "subscription": 0}
        self.date_range_days = date_range_days
        self.subscribed_artists = subscribed_artists or []
        self.discovery_rate = discovery_rate
        self.mab_limits = mab_limits or {"min_quota": 0.2, "max_quota": 0.6}
        
        # 排行榜配置
        self.ranking_config = ranking_config or {}
        self.ranking_enabled = self.ranking_config.get("enabled", False)
        self.ranking_modes = self.ranking_config.get("modes", ["day"])
        self.ranking_limit = self.ranking_config.get("limit", 100)
        
        # 动态阈值配置 (冷门标签保底)
        dt_cfg = dynamic_threshold_config or {}
        dt_cfg = dynamic_threshold_config or {}
        self.dynamic_threshold_min = dt_cfg.get("min", 100)
        self.dynamic_threshold_rate = dt_cfg.get("rate", 0.05)
        
        # 搜索数量限制
        self.search_limit = search_limit
        self.tag_classifier = tag_classifier

        # 缓存 Tag 的最高热度，避免重复查询 (Session Valid)
        self._search_max_bookmarks_cache = {}
        self._cache_lock = asyncio.Lock()  # 保护缓存操作
        
        logger.debug(f"ContentFetcher 初始化: date_range_days={self.date_range_days}")
    
    def _adaptive_threshold(self, base: int, tag_weight: float, is_combination: bool = False) -> int:
        """
        自适应收藏阈值
        
        根据 Tag 权重动态调整阈值：
        - 高权重 Tag（用户热爱）：保持高阈值，确保质量
        - 低权重 Tag（尝试发现）：降低阈值，扩大搜索范围
        - 组合搜索：额外降低阈值（更精准匹配）
        """
        multiplier = max(0.3, tag_weight)
        
        if is_combination:
            multiplier *= 0.5
        
        return max(100, int(base * multiplier))

    def _weighted_sample_items(self, items: list, weights: list[float], k: int) -> list:
        """按权重无放回采样，避免概率和未重算导致抽样偏斜。"""
        if k <= 0 or not items:
            return []
        if len(items) <= k:
            return list(items)

        selected = []
        available = list(range(len(items)))

        for _ in range(min(k, len(items))):
            available_weights = [max(0.0, float(weights[i])) for i in available]
            total_weight = sum(available_weights)

            if total_weight <= 0:
                chosen_idx = random.choice(available)
            else:
                threshold = random.random() * total_weight
                cumulative = 0.0
                chosen_idx = available[-1]
                for idx, weight in zip(available, available_weights):
                    cumulative += weight
                    if threshold <= cumulative:
                        chosen_idx = idx
                        break

            selected.append(items[chosen_idx])
            available.remove(chosen_idx)

        return selected

    @staticmethod
    def _is_identity_pair(
        tag1: str,
        tag2: str,
        tag_classifications: Optional[dict] = None,
    ) -> bool:
        classification1 = (tag_classifications or {}).get(tag1)
        classification2 = (tag_classifications or {}).get(tag2)
        return (
            classification1 is not None
            and classification2 is not None
            and getattr(classification1, "classification", None) == "ip"
            and getattr(classification2, "classification", None) == "ip"
        )

    async def _classify_seed_tags(
        self,
        top_pairs: list[tuple[str, str, float]],
        xp_tags: list[tuple[str, float]],
    ) -> dict:
        if not self.tag_classifier:
            return {}

        tags = []
        for tag1, tag2, _ in top_pairs:
            tags.extend([tag1, tag2])
        tags.extend(tag for tag, _ in xp_tags)

        if not tags:
            return {}

        try:
            return await self.tag_classifier.classify_tags(tags)
        except Exception as e:
            logger.warning(f"种子标签分类失败，回退到原有组合搜索: {e}")
            return {}

    def _select_search_pairs(
        self,
        top_pairs: list[tuple[str, str, float]],
        max_combo_tasks: int,
        tag_classifications: Optional[dict] = None,
    ) -> list[tuple[str, str, float]]:
        """在热门组合中保留主干，并留出一部分名额做加权探索。"""
        if max_combo_tasks <= 0:
            return []

        valid_pairs = []
        seen_pairs = set()

        for t1, t2, weight in top_pairs:
            pair_key = tuple(sorted((t1, t2)))
            if pair_key in seen_pairs:
                continue

            q1 = expand_search_query(t1)
            q2 = expand_search_query(t2)
            if q1 == q2 or t1 in q2 or t2 in q1:
                continue
            if self._is_identity_pair(t1, t2, tag_classifications=tag_classifications):
                continue

            seen_pairs.add(pair_key)
            valid_pairs.append((t1, t2, max(0.0, float(weight))))

        if len(valid_pairs) <= max_combo_tasks:
            return valid_pairs

        discovery_slots = min(
            max_combo_tasks,
            max(0, int(round(max_combo_tasks * self.discovery_rate)))
        )
        stable_slots = max_combo_tasks - discovery_slots

        selected = list(valid_pairs[:stable_slots])
        remaining = valid_pairs[stable_slots:]

        if discovery_slots > 0 and remaining:
            selected.extend(
                self._weighted_sample_items(
                    remaining,
                    [weight for _, _, weight in remaining],
                    discovery_slots,
                )
            )

        if len(selected) < max_combo_tasks:
            selected_pair_keys = {tuple(sorted((t1, t2))) for t1, t2, _ in selected}
            for pair in valid_pairs:
                pair_key = tuple(sorted((pair[0], pair[1])))
                if pair_key in selected_pair_keys:
                    continue
                selected.append(pair)
                selected_pair_keys.add(pair_key)
                if len(selected) >= max_combo_tasks:
                    break

        return selected[:max_combo_tasks]
    def _build_exploration_pairs(
        self,
        xp_tags: list[tuple[str, float]],
        used_pairs: set[tuple[str, str]],
        count: int,
        tag_classifications: Optional[dict] = None,
    ) -> list[tuple[str, str, float]]:
        """从 XP 标签池里补一些随机组合，提升种子多样性。"""
        if count <= 0 or len(xp_tags) < 2:
            return []

        extra_pairs = []
        attempts = 0
        max_attempts = max(10, count * 8)

        while len(extra_pairs) < count and attempts < max_attempts:
            attempts += 1
            sampled_tags = self._weighted_sample(xp_tags, k=2)
            if len(sampled_tags) < 2:
                continue

            t1, t2 = sampled_tags
            pair_key = tuple(sorted((t1, t2)))
            if pair_key in used_pairs:
                continue

            q1 = expand_search_query(t1)
            q2 = expand_search_query(t2)
            if q1 == q2 or t1 in q2 or t2 in q1:
                continue
            if self._is_identity_pair(t1, t2, tag_classifications=tag_classifications):
                continue

            used_pairs.add(pair_key)
            extra_pairs.append((t1, t2, 0.0))

        return extra_pairs    
    async def discover(
        self,
        xp_tags: list[tuple[str, float]],
        limit: int = 50
    ) -> list[Illust]:
        """
        策略A：基于XP的广泛搜索 (并发优化版)
        """
        if not xp_tags:
            logger.warning("无XP标签，跳过搜索")
            return []
        
        all_illusts = []
        
        # 获取高权重组合 (Smart Search)
        top_pairs = await db.get_top_tag_pairs(limit=50)
        if not top_pairs:
            top_pairs = []
        tag_classifications = await self._classify_seed_tags(top_pairs, xp_tags)
        used_pairs = set()

        tasks = []

        # 1. 构建组合搜索任务
        max_combo_tasks = 20 # 限制并发数
        selected_pairs = self._select_search_pairs(
            top_pairs,
            max_combo_tasks,
            tag_classifications=tag_classifications,
        )
        used_pairs.update(tuple(sorted((t1, t2))) for t1, t2, _ in selected_pairs)

        if len(selected_pairs) < max_combo_tasks and self.discovery_rate > 0:
            selected_pairs.extend(
                self._build_exploration_pairs(
                    xp_tags,
                    used_pairs,
                    max_combo_tasks - len(selected_pairs),
                    tag_classifications=tag_classifications,
                )
            )

        for t1, t2, _ in selected_pairs:
            tasks.append(self._search_pair(t1, t2))

        # 执行组合搜索
        # 为了避免瞬间过高并发，我们可以切分 tasks
        # 但 PixivClient 内部有 RateLimiter，所以 gather all 也是安全的，只是会排队。
        # 不过为了更早拿到结果并判断数量，我们还是分批好？
        # 全量 gather 也行，代码最简单。
        
        if tasks:
            logger.info(f"启动 {len(tasks)} 个组合搜索任务...")

            # 使用 Semaphore 控制并发
            sem = asyncio.Semaphore(5)
            async def sem_task(coro):
                async with sem:
                    return await coro
            wrapped_tasks = [sem_task(t) for t in tasks]
            results = await asyncio.gather(*wrapped_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, list):
                    all_illusts.extend(res)
                elif isinstance(res, Exception):
                    logger.error(f"组合搜索任务异常: {res}")

        # 检查数量是否达标
        remaining = limit - len(all_illusts)
        
        # 2. 如果不够，补充单 Tag 搜索
        if remaining > 0:
            fallback_tasks = []
            used_seed_tags = {tag for pair in used_pairs for tag in pair}
            # 尝试多次采样补充
            for _ in range(3):
                tags_to_search = self._weighted_sample(xp_tags, k=1)
                if not tags_to_search: continue
                
                tag = tags_to_search[0]
                if tag in used_seed_tags:
                    continue
                
                fallback_tasks.append(self._search_single(tag, max(10, remaining // 2)))
                
            if fallback_tasks:
                logger.info(f"启动 {len(fallback_tasks)} 个单Tag补充任务...")
                wrapped_fallback = [sem_task(t) for t in fallback_tasks]
                res_list = await asyncio.gather(*wrapped_fallback, return_exceptions=True)
                for res in res_list:
                    if isinstance(res, list):
                        all_illusts.extend(res)

        # 去重
        MAX_PER_ARTIST = 3
        artist_counts = {}
        filtered_illusts = []
        
        for illust in all_illusts:
            artist_id = illust.user_id
            if artist_counts.get(artist_id, 0) < MAX_PER_ARTIST:
                filtered_illusts.append(illust)
                artist_counts[artist_id] = artist_counts.get(artist_id, 0) + 1
        
        logger.info(f"XP搜索获取 {len(filtered_illusts)} 个作品 (原始 {len(all_illusts)})")
        return filtered_illusts[:limit]

    async def _search_pair(self, t1: str, t2: str) -> list[Illust]:
        """单个组合搜索任务"""
        base_threshold = self.bookmark_threshold["search"]
        
        # 并发获取动态阈值
        t1_thresh, t2_thresh = await asyncio.gather(
            self._get_dynamic_threshold(t1, base_threshold),
            self._get_dynamic_threshold(t2, base_threshold)
        )
        
        threshold = int(min(t1_thresh, t2_thresh) * 0.3)  # 组合搜索降低阈值(0.3)，增加命中率
        
        # 获取搜索词
        tags_dict = await db.get_best_search_tags([t1, t2])
        raw_t1 = tags_dict.get(t1, t1)
        raw_t2 = tags_dict.get(t2, t2)
        
        final_q1 = self._build_query(t1, raw_t1)
        final_q2 = self._build_query(t2, raw_t2)
        
        # 从配置读取 content_type
        content_type = self.config.get("filter", {}).get("content_type", "all")
        
        return await self.client.search_illusts(
            tags=[final_q1, final_q2],
            bookmark_threshold=threshold,
            date_range_days=self.date_range_days,
            limit=self.search_limit,
            content_type=content_type
        )

    async def _search_single(self, tag: str, limit: int) -> list[Illust]:
        """单个Tag搜索任务"""
        dynamic = await self._get_dynamic_threshold(tag, self.bookmark_threshold["search"])
        # 如果是单Tag搜索，也给与一定折扣(0.5)，防止动态阈值过高
        threshold = int(min(dynamic, self.bookmark_threshold["search"] * 0.5))
        
        raw_tag = await db.get_best_search_tag(tag)
        final_q = self._build_query(tag, raw_tag)
        
        # 从配置读取 content_type
        content_type = self.config.get("filter", {}).get("content_type", "all")
        
        return await self.client.search_illusts(
            tags=[final_q],
            bookmark_threshold=threshold,
            date_range_days=self.date_range_days,
            limit=limit,
            content_type=content_type
        )

    def _build_query(self, tag: str, raw_tag: str) -> str:
        base_q = expand_search_query(tag)
        if raw_tag != tag and raw_tag not in base_q:
            if "(" in base_q:
                return base_q[:-1] + f" OR {raw_tag})"
            else:
                return f"({base_q} OR {raw_tag})"
        return base_q

    
    async def check_subscriptions(self) -> list[Illust]:
        """
        策略B：检查订阅画师更新 + 关注者新作
        使用 sync_client 进行低风险操作
        """
        all_illusts = []
        seen_ids = set()
        
        # 1. 获取关注者时间轴 (高效) - 使用 sync_client
        try:
            feed_illusts = await self.sync_client.fetch_follow_latest(limit=100)
            for illust in feed_illusts:
                if illust.id not in seen_ids:
                    all_illusts.append(illust)
                    seen_ids.add(illust.id)
        except Exception as e:
            logger.error(f"获取关注时间轴失败: {e}")
            
        # 2. 检查配置中的特定订阅 (补充) - 使用 sync_client
        # 如果订阅列表只有几个，检查一下也无妨；如果是空的则跳过
        if self.subscribed_artists:
            since = datetime.now().astimezone() - timedelta(days=self.date_range_days)
            for artist_id in self.subscribed_artists:
                # 如果刚才的 feed 里已经有了很多该画师的图，或许可以跳过？
                # 简单起见，还是查一下，但限制数量
                try:
                    illusts = await self.sync_client.get_user_illusts(
                        user_id=artist_id,
                        since=since,
                        limit=5
                    )
                    for illust in illusts:
                        if illust.id not in seen_ids:
                            all_illusts.append(illust)
                            seen_ids.add(illust.id)
                except Exception as e:
                    logger.error(f"获取画师 {artist_id} 作品失败: {e}")
        
        logger.info(f"订阅/关注更新获取 {len(all_illusts)} 个作品")
        return all_illusts
    
    async def fetch_ranking(self) -> list[Illust]:
        """
        策略C：排行榜抓取
        
        Returns:
            排行榜作品列表
        """
        if not self.ranking_enabled:
            logger.debug("排行榜功能未启用")
            return []
        
        all_illusts = []
        
        # 从配置读取 content_type
        content_type = self.config.get("filter", {}).get("content_type", "all")
        
        for mode in self.ranking_modes:
            try:
                illusts = await self.client.get_ranking(
                    mode=mode,
                    limit=self.ranking_limit // len(self.ranking_modes),
                    content_type=content_type
                )
                all_illusts.extend(illusts)
                logger.info(f"排行榜 [{mode}] 获取 {len(illusts)} 个作品")
            except Exception as e:
                logger.error(f"获取 {mode} 排行榜失败: {e}")
        
        logger.info(f"排行榜总计获取 {len(all_illusts)} 个作品")
        return all_illusts
    
    def _weighted_sample(
        self,
        weighted_tags: list[tuple[str, float]],
        k: int
    ) -> list[str]:
        """根据权重随机采样Tag"""
        if len(weighted_tags) <= k:
            return [t[0] for t in weighted_tags]

        tags = [t[0] for t in weighted_tags]
        weights = [t[1] for t in weighted_tags]
        return self._weighted_sample_items(tags, weights, k)
    
    async def _get_dynamic_threshold(self, tag: str, base: int) -> int:
        """
        基于热度的动态阈值 (Dynamic Threshold) - Cached
        """
        # 1. Check Cache (使用锁保护读取)
        async with self._cache_lock:
            cached = self._search_max_bookmarks_cache.get(tag)
        
        if cached is not None:
            max_bookmarks = cached
            # logger.debug(f"动态阈值 Cache Hit: {tag} -> {max_bookmarks}")
        else:
            try:
                # 搜索该 Tag 按收藏数降序，获取第一张作为参考
                # 使用与主流程一致的时间范围，避免历史补充模式仍用默认30天
                content_type = self.config.get("filter", {}).get("content_type", "all")
                top_illusts = await self.client.search_illusts(
                    tags=[tag], 
                    limit=1,
                    date_range_days=self.date_range_days,
                    content_type=content_type,
                    # search_illusts 内部默认是 popular_desc，所以取第1个就是 Max Bookmarks 左右
                )
                if top_illusts:
                    max_bookmarks = top_illusts[0].bookmark_count
                else:
                    max_bookmarks = 1000  # Fallback
            except Exception as e:
                logger.debug(f"获取 Tag '{tag}' 热度失败: {e}")
                max_bookmarks = 1000  # Fallback
            
            # Save Cache (使用锁保护写入)
            async with self._cache_lock:
                self._search_max_bookmarks_cache[tag] = max_bookmarks

        # 相对阈值：使用配置的比例和保底值
        relative_threshold = max(self.dynamic_threshold_min, int(max_bookmarks * self.dynamic_threshold_rate))
        
        final_threshold = min(base, relative_threshold)
        # logger.debug(f"动态阈值 '{tag}': Max={max_bookmarks} -> Threshold={final_threshold} (Base={base})")
        
        return final_threshold

    async def select_strategies(self, total_limit: int) -> dict[str, int]:
        """
        MAB 策略调度 (Thompson Sampling)
        
        基于历史反馈动态分配配额：
        - XP搜索 (xp_search)
        - 订阅 (subscription)
        - 排行榜 (ranking)
        - 关联发现 (related)
        - 互动画师发现 (engagement_artists) - 新增
        """
        strategies = ['xp_search', 'subscription', 'ranking', 'related', 'engagement_artists']
        scores = {}
        
        for strategy in strategies:
            s, total = await db.get_strategy_stats(strategy)
            f = total - s
            # Beta分布采样 (alpha=s+1, beta=f+1)
            # 给予一定的先验信心 (alpha+=2, beta+=2) 避免初期剧烈波动
            scores[strategy] = random.betavariate(s + 2, f + 2)
            
        total_score = sum(scores.values())
        ratios = {k: v/total_score for k, v in scores.items()}
        
        # 应用配额限制 (Quota Limits)
        min_q = self.mab_limits.get("min_quota", 0.1)
        max_q = self.mab_limits.get("max_quota", 0.8)
        
        final_quotas = {}
        remaining = total_limit
        
        # 1. 先分配最小保底
        for s in ratios:
            min_count = int(total_limit * min_q)
            final_quotas[s] = min_count
            remaining -= min_count
            
        # 2. 剩下的按比例分配，但不超过 max
        if remaining > 0:
            for s, r in ratios.items():
                if remaining <= 0: break
                
                # 当前已分配
                current = final_quotas[s]
                # 最大允许
                max_allowed = int(total_limit * max_q)
                
                # 目标分配 (按比例应得的总数)
                target = int(total_limit * r)
                
                # 还可以加多少
                can_add = max(0, max_allowed - current)
                needed = max(0, target - current)
                
                actual_add = min(needed, can_add, remaining)
                final_quotas[s] += actual_add
                remaining -= actual_add
                
        # 3. 如果还有剩余 (由于 max 限制导致未分完)，均分给未达上限的
        if remaining > 0:
             # 为了简单，随机分给没满的，或者直接分给第一名
             # 这里简单平铺
             for s in strategies:
                 if remaining <= 0: break
                 max_allowed = int(total_limit * max_q)
                 if final_quotas[s] < max_allowed:
                     add = 1
                     final_quotas[s] += add
                     remaining -= add
        
        logger.info(f"MAB 分配: {final_quotas} (Scores: { {k: f'{v:.2f}' for k, v in scores.items()} })")
        return final_quotas

    async def discover_related(self, xp_tags: list[tuple[str, float]], limit: int = 50) -> list[Illust]:
        """策略D：基于关联作品 (Related Works) 的发现。"""
        recommender = RelatedRecommender(
            client=self.client,
            sync_client=self.sync_client,
            config=self.config,
            bookmark_threshold=self.bookmark_threshold,
        )
        return await recommender.discover_for_strategy(xp_tags, limit=limit)

    async def discover_from_engaged_artists(
        self, 
        xp_tags: list[tuple[str, float]], 
        limit: int = 30
    ) -> list[Illust]:
        """
        策略E：基于互动画师的发现 (Engagement-Based Discovery)
        
        借鉴 X 算法的 Thunder (关注者内容流) + Two-Tower (用户偏好匹配)
        
        1. 从用户点赞历史中，统计互动最多的画师
        2. 获取这些画师的最新作品
        3. 结合 XP Profile 进行匹配度评分
        """
        try:
            # 1. 获取互动最多的画师
            top_artists = await db.get_top_engaged_artists(limit=10)
            if not top_artists:
                logger.debug("无互动画师数据，跳过策略E")
                return []
            
            logger.info(f"策略E: 发现 {len(top_artists)} 个互动画师")
            
            # 2. 并发获取画师最新作品
            from datetime import datetime, timedelta
            since = datetime.now().astimezone() - timedelta(days=self.date_range_days * 2)  # 扩大时间范围
            
            async def fetch_artist_works(artist_id: int, artist_name: str):
                try:
                    illusts = await self.sync_client.get_user_illusts(
                        user_id=artist_id,
                        since=since,
                        limit=5  # 每个画师取 5 个
                    )
                    return illusts
                except Exception as e:
                    logger.debug(f"获取画师 {artist_name}({artist_id}) 作品失败: {e}")
                    return []
            
            tasks = [fetch_artist_works(aid, aname) for aid, aname, _ in top_artists]
            results = await asyncio.gather(*tasks)
            
            # 3. 扁平化并评分
            all_illusts = []
            xp_dict = dict(xp_tags)
            
            for illusts in results:
                for illust in illusts:
                    # 计算匹配分
                    score = 0.0
                    for tag in illust.tags:
                        norm = tag.lower().replace(" ", "_")
                        if norm in xp_dict:
                            score += xp_dict[norm]
                    
                    # 画师好感度加成
                    artist_score = await db.get_artist_score(illust.user_id)
                    score += artist_score * 0.5  # 衰减因子避免过度依赖
                    
                    all_illusts.append((illust, score))
            
            # 4. 排序并返回
            all_illusts.sort(key=lambda x: x[1], reverse=True)
            
            result = [x[0] for x in all_illusts[:limit]]
            logger.info(f"策略E: 获取 {len(result)} 个作品 (来自 {len(top_artists)} 个互动画师)")
            
            return result
            
        except Exception as e:
            logger.error(f"策略E执行失败: {e}")
            return []

    async def fetch_content(self, xp_tags: list[tuple[str, float]], total_limit: int = 200) -> list[Illust]:
        """
        统一内容获取入口 (MAB 调度)
        
        策略:
        - xp_search: XP 画像搜索
        - subscription: 订阅/关注更新
        - ranking: 排行榜
        - related: 关联作品发现
        - engagement_artists: 互动画师发现 (策略E, 新增)
        """
        quotas = await self.select_strategies(total_limit)
        
        tasks = []
        task_names = []  # 记录任务顺序，用于解析结果
        
        # 1. XP 搜索
        if quotas.get('xp_search', 0) > 0:
            tasks.append(self.discover(xp_tags, limit=quotas['xp_search']))
            task_names.append('xp_search')
            
        # 2. 订阅
        tasks.append(self.check_subscriptions())
        task_names.append('subscription')
        
        # 3. 排行榜
        if quotas.get('ranking', 0) > 0 and self.ranking_enabled:
            tasks.append(self.fetch_ranking_with_limit(quotas['ranking']))
            task_names.append('ranking')
            
        # 4. 关联推荐
        if quotas.get('related', 0) > 0:
            tasks.append(self.discover_related(xp_tags, limit=quotas['related']))
            task_names.append('related')
        
        # 5. 互动画师发现 (策略E, 新增)
        if quotas.get('engagement_artists', 0) > 0:
            tasks.append(self.discover_from_engaged_artists(xp_tags, limit=quotas['engagement_artists']))
            task_names.append('engagement_artists')
            
        # 执行获取
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 解析结果并标记来源
        all_illusts = []
        result_counts = {}
        
        for i, result in enumerate(results):
            name = task_names[i]
            if isinstance(result, Exception):
                logger.error(f"策略 {name} 执行异常: {result}")
                result_counts[name] = 0
                continue
            
            illusts = result if isinstance(result, list) else []
            for ill in illusts:
                ill.source = name
            all_illusts.extend(illusts)
            result_counts[name] = len(illusts)
        
        # 日志汇总
        counts_str = ", ".join(f"{k}={v}" for k, v in result_counts.items())
        logger.info(f"策略获取汇总: {counts_str}")
        
        return all_illusts


    async def fetch_ranking_with_limit(self, limit: int) -> list[Illust]:
        """支持自定义 Limit 的排行榜抓取"""
        if not self.ranking_enabled or limit <= 0:
            return []
            
        all_illusts = []
        # 从配置读取 content_type
        content_type = self.config.get("filter", {}).get("content_type", "all")
        
        for mode in self.ranking_modes:
            try:
                # 平均分配 limit
                mode_limit = max(1, limit // len(self.ranking_modes))
                illusts = await self.client.get_ranking(
                    mode=mode, 
                    limit=mode_limit,
                    content_type=content_type
                )
                all_illusts.extend(illusts)
            except Exception as e:
                logger.error(f"获取 {mode} 排行榜失败: {e}")
        return all_illusts


