"""
内容过滤模块
去重、黑名单、质量过滤、匹配度评分
"""
import logging
from dataclasses import dataclass
from typing import Optional

from pixiv_client import Illust
import database as db
from tag_categories import is_identity_category, is_seed_category
from daily_slate import (
    DailySlateComposer,
    DailySlateResult,
    PreferenceContributions,
    calculate_preference_contributions,
)
from embedder import profile_embedding_hash
from tag_mapping import TagIdentityResolver
from utils import normalize_tag

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ContentFilterResult:
    selected: tuple[Illust, ...]
    ranked: tuple[Illust, ...]
    daily_slate: DailySlateResult | None = None


async def ensure_current_profile_embedding(
    embedder,
    xp_profile: dict[str, float],
    user_id: int,
) -> list[float] | None:
    """Return the current profile vector, creating its normal cache entry if needed."""
    if not embedder or not embedder.enabled or not xp_profile or user_id <= 0:
        return None

    profile_hash = profile_embedding_hash(xp_profile)
    user_embedding = await db.get_current_user_embedding(
        user_id,
        embedder.model,
        profile_hash,
    )
    if user_embedding:
        logger.debug("使用缓存的用户 Embedding")
        return user_embedding

    top_tags = [
        tag
        for tag, _weight in sorted(
            xp_profile.items(),
            key=lambda item: item[1],
            reverse=True,
        )[:15]
    ]
    user_embedding = await embedder.embed_tags(top_tags)
    if user_embedding:
        await db.save_user_embedding(
            user_id,
            user_embedding,
            embedder.model,
            profile_hash,
        )
        logger.info("已更新用户画像 Embedding")
    return user_embedding


def calculate_match_score(
    illust: Illust,
    xp_profile: dict[str, float],
    negative_profile: dict[str, float] = None,
    tag_classifications: Optional[dict] = None,
    tag_resolver: Optional[TagIdentityResolver] = None,
) -> float:
    """
    计算作品与 XP 画像的匹配度（改进版）

    算法:
    1. 多个 feature 命中采用递减贡献，避免 tag 堆叠
    2. 非 feature 偏好保持原有累加方式
    3. 按最高权重归一化
    4. 使用对数平滑匹配数量影响
    5. 负向画像惩罚（匹配到不喜欢的 Tag 时扣分）

    Returns:
        0.0 ~ 1.0 归一化分数
    """
    return calculate_tag_match_score(
        illust.tags,
        xp_profile,
        negative_profile,
        tag_classifications=tag_classifications,
        tag_resolver=tag_resolver,
    )


def calculate_tag_match_score(
    tags: list[str],
    xp_profile: dict[str, float],
    negative_profile: dict[str, float] = None,
    tag_classifications: Optional[dict] = None,
    tag_resolver: Optional[TagIdentityResolver] = None,
) -> float:
    """Calculate the production Tag score from cached work tags only."""

    contributions = calculate_preference_contributions(
        tags,
        xp_profile,
        negative_profile,
        classifications=tag_classifications,
        resolver=tag_resolver,
    )
    return contributions.match_score

class ContentFilter:
    """内容过滤器"""
    
    def __init__(
        self,
        blacklist_tags: Optional[list[str]] = None,
        daily_limit: int = 20,
        exclude_ai: bool = True,
        min_match_score: float = 0.0,
        match_weight: float = 0.5,
        max_per_artist: int = 3,
        subscribed_artists: Optional[list[int]] = None,  # 关注的画师 ID
        artist_boost: float = 0.3,  # 关注画师的匹配度加成
        min_create_days: int = 0,  # 过滤 N 天前的老图 (0=不过滤)
        r18_mode: bool | str = False,  # 涩涩模式：只推送 R-18 (支持 bool 或 str: safe, mixed, r18_only)
        # === 新增：借鉴 X 算法的增强选项 ===
        author_diversity: Optional[dict] = None,  # 画师多样性衰减配置
        source_boost: Optional[dict] = None,  # 来源加成配置
        embedder = None,  # 可选的 Embedder 实例 (用于语义匹配)
        ai_scorer = None,  # 可选的 AIScorer 实例 (用于 LLM 精排)
        # 多样性增强
        shuffle_factor: float = 0.0,  # 随机打散因子 (0-0.5)
        exploration_ratio: float = 0.0,  # 探索比例 (0-0.5)
        skip_ugoira: bool = False,  # 跳过动图
        content_type: str = "all",  # 内容类型过滤: "all", "illust", "manga"
        tag_classifier = None,
        display_tags_max_ip_count: int = 2,
        ip_diversity: Optional[dict] = None,
        daily_slate: Optional[dict] = None,
    ):
        self._config_blacklist_raw = [t for t in (blacklist_tags or []) if t]
        self._config_blacklist_tags = {normalize_tag(t) for t in self._config_blacklist_raw}
        self._db_blacklist_tags: set[str] = set()
        self.blacklist_tags = set(self._config_blacklist_tags)
        self.blocked_artist_ids: set[int] = set()
        self.daily_limit = daily_limit
        self.exclude_ai = exclude_ai
        self.min_match_score = min_match_score
        self.match_weight = match_weight
        self.max_per_artist = max_per_artist
        self.subscribed_artists = set(subscribed_artists or [])
        self.artist_boost = artist_boost
        self.min_create_days = min_create_days
        self.r18_mode = r18_mode
        self.skip_ugoira = skip_ugoira
        self.content_type = content_type.lower()  # 统一小写
        self.tag_classifier = tag_classifier
        self.tag_resolver = TagIdentityResolver()
        self.display_tags_max_ip_count = self._normalize_max_ip_count(display_tags_max_ip_count)
        
        # 画师多样性衰减 (借鉴 X 算法 AuthorDiversityScorer)
        # 公式: multiplier(position) = (1.0 - floor) × decay^position + floor
        diversity_cfg = author_diversity or {}
        self.diversity_enabled = diversity_cfg.get("enabled", False)
        self.diversity_decay = diversity_cfg.get("decay_factor", 0.5)
        self.diversity_floor = diversity_cfg.get("floor", 0.1)

        ip_diversity_cfg = ip_diversity or {}
        self.ip_diversity_enabled = ip_diversity_cfg.get("enabled", False)
        self.ip_diversity_decay = ip_diversity_cfg.get("decay_factor", 0.6)
        self.ip_diversity_floor = ip_diversity_cfg.get("floor", 0.1)
        self.daily_slate = DailySlateComposer(daily_slate)
        
        # 来源加成 (借鉴 X 算法 OON Scorer)
        self.source_boost = source_boost or {
            "xp_search": 1.0,
            "subscription": 1.1,
            "ranking": 0.9,
            "related": 1.15
        }
        
        # AI Embedding 语义匹配 (可选)
        self.embedder = embedder
        
        # AI Scorer LLM 精排 (可选)
        self.ai_scorer = ai_scorer
        
        # 多样性增强
        self.shuffle_factor = min(0.5, max(0.0, shuffle_factor))  # 限制在 0-0.5
        self.exploration_ratio = min(0.5, max(0.0, exploration_ratio))
        
        # 硬性过滤Tag
        self.blacklist_tags.update({"r-18g", "guro", "gore"})

    async def load_db_blocklist(self) -> None:
        """加载数据库中的屏蔽标签和屏蔽画师。"""
        blocked_tags = await db.get_blocked_tags()
        blocked_artists = await db.get_blocked_artists()
        try:
            aliases = await db.get_accepted_tag_aliases("equivalent")
        except Exception as exc:
            logger.warning("加载已审核 Tag Alias 失败，使用确定性归一化: %s", exc)
            aliases = {}
        self.tag_resolver = TagIdentityResolver(aliases)
        self._config_blacklist_tags = {
            self.tag_resolver.resolve(tag) for tag in self._config_blacklist_raw
        }

        self._db_blacklist_tags = {
            self.tag_resolver.resolve(tag)
            for tag in blocked_tags
            if tag
        }
        self.blocked_artist_ids = {
            artist_id
            for artist_id, _ in blocked_artists
        }
        self.blacklist_tags = set(self._config_blacklist_tags)
        self.blacklist_tags.update(self._db_blacklist_tags)
        self.blacklist_tags.update({"r-18g", "guro", "gore"})
    
    async def filter(
        self,
        illusts: list[Illust],
        xp_profile: Optional[dict[str, float]] = None,
        user_id: int = 0  # 用于 Embedding 缓存
    ) -> list[Illust]:
        result = await self.filter_with_result(illusts, xp_profile, user_id)
        return list(result.selected)

    async def filter_with_result(
        self,
        illusts: list[Illust],
        xp_profile: Optional[dict[str, float]] = None,
        user_id: int = 0,
    ) -> ContentFilterResult:
        """
        过滤管道
        
        1. 去重（已推送）
        2. 时间过滤（老图片）
        3. 硬性过滤（R-18G、AI）
        4. 黑名单Tag
        5. 匹配度过滤 + 画师权重加成 + 语义匹配(可选)
        6. 综合排序
        7. 多样性控制
        8. 每日上限
        """
        from datetime import datetime, timedelta
        
        if not illusts:
            return ContentFilterResult((), ())

        try:
            await self.load_db_blocklist()
        except Exception as e:
            logger.warning(f"加载数据库屏蔽列表失败: {e}")
        
        # 计算时间阈值
        time_threshold = None
        if self.min_create_days > 0:
            time_threshold = datetime.now(illusts[0].create_date.tzinfo if illusts else None) - timedelta(days=self.min_create_days)
        
        # 批量预加载已推送 ID (性能优化: O(n) -> O(1) 数据库查询)
        all_ids = [illust.id for illust in illusts]
        pushed_ids = await db.get_pushed_ids_batch(all_ids)

        # 预加载临时静音标签 (/mute)
        muted_tags = set()
        try:
            muted_rows = await db.get_muted_tags(active_only=True)
            muted_tags = {row[0].lower().strip() for row in muted_rows}
        except Exception as e:
            logger.warning(f"预加载静音标签失败: {e}")
        
        result = []
        filtered_by_time = 0
        
        # 过滤原因统计
        reason_stats = {
            "pushed": 0,
            "time": 0,
            "artist": 0,
            "blacklist": 0,
            "muted": 0,
            "ai": 0,
            "r18": 0,
            "ugoira": 0,
        }
        
        for illust in illusts:
            # 1. 去重 (使用预加载的集合)
            if illust.id in pushed_ids:
                reason_stats["pushed"] += 1
                continue
            
            # 2. 时间过滤
            if time_threshold and illust.create_date < time_threshold:
                filtered_by_time += 1
                reason_stats["time"] += 1
                continue

            if illust.user_id and illust.user_id in self.blocked_artist_ids:
                reason_stats["artist"] += 1
                continue
            
            # 3. R-18G 排除
            if self._has_blacklisted_tag(illust):
                reason_stats["blacklist"] += 1
                continue

            # 3.5 临时静音标签过滤 (/mute)
            try:
                if muted_tags:
                    from utils import normalize_tag
                    muted_hit = any(
                        self.tag_resolver.resolve(t) in muted_tags
                        for t in (illust.tags or [])
                    )
                    if muted_hit:
                        reason_stats["muted"] += 1
                        continue
            except Exception as e:
                logger.warning(f"静音标签检查失败: {e}")
            
            # 4. AI 生成排除（增强版）
            if self.exclude_ai:
                # 4.1 Pixiv 官方标记
                if illust.ai_type == 2:
                    reason_stats["ai"] += 1
                    continue
                # 4.2 标签关键词检测
                if self._is_ai_by_tags(illust):
                    reason_stats["ai"] += 1
                    continue
            
            # 4.1 涩涩模式 (R-18 Mode Control)
            # 支持 bool (旧配置) 和 str (新配置: safe, mixed, r18_only)
            mode_str = str(self.r18_mode).lower()
            
            # R18 判定增强：标签显式 + 作品 x_restrict + 保险检查
            has_r18_tag = any(t.lower().replace(" ", "") in ("r-18", "r18") for t in (illust.tags or []))
            is_r18 = bool(getattr(illust, "is_r18", False) or getattr(illust, "x_restrict", 0) == 1 or has_r18_tag)
            
            if mode_str in ("true", "r18_only", "pure"):
                # 纯 18+ 模式：只允许 R-18
                if not is_r18:
                    reason_stats["r18"] += 1
                    continue
            elif mode_str in ("safe", "18-", "clean"):
                # 净网模式：禁止 R-18
                if is_r18:
                    reason_stats["r18"] += 1
                    continue
            else:
                # 默认/mixed/neutral：不因 R-18 属性过滤，全凭匹配度
                pass
            
            # 4.2 动图过滤
            if self.skip_ugoira and getattr(illust, 'type', 'illust') == 'ugoira':
                reason_stats["ugoira"] += 1
                continue
            
            result.append(illust)
        
        if filtered_by_time > 0:
            logger.debug(f"过滤 {filtered_by_time} 个超过 {self.min_create_days} 天的老图")
        
        # 去重（同批次内）
        seen_ids = set()
        unique_result = []
        for illust in result:
            if illust.id not in seen_ids:
                seen_ids.add(illust.id)
                unique_result.append(illust)

        tag_classifications = await self._classify_tags_for_illusts(unique_result)
        
        # 5. 计算匹配度并过滤 + 画师权重加成 + 负向画像惩罚 + 语义匹配(可选)
        negative_profile = await db.get_negative_profile()  # 加载负向画像
        
        # 准备语义匹配 (如果启用)
        user_embedding = None
        illust_embeddings_cache = {}
        
        if self.embedder and self.embedder.enabled and xp_profile and user_id > 0:
            try:
                user_embedding = await ensure_current_profile_embedding(
                    self.embedder,
                    xp_profile,
                    user_id,
                )
                
                # 批量获取作品 Embedding 缓存
                illust_ids = [ill.id for ill in unique_result]
                illust_embeddings_cache = await db.get_illust_embeddings_batch(illust_ids)
                logger.debug(f"Embedding 缓存命中: {len(illust_embeddings_cache)}/{len(illust_ids)}")
                
            except Exception as e:
                logger.warning(f"语义匹配初始化失败: {e}")
                user_embedding = None
        
        scored_result = []
        uncached_embeddings = []  # 待计算的作品 Embedding
        contributions_by_id: dict[int, PreferenceContributions] = {}
        
        for illust in unique_result:
            if xp_profile:
                contributions = calculate_preference_contributions(
                    illust.tags,
                    xp_profile,
                    negative_profile,
                    classifications=tag_classifications,
                    resolver=self.tag_resolver,
                )
                score = contributions.match_score
                contributions_by_id[illust.id] = contributions

                # 画师权重加成：关注画师的作品额外加成
                if illust.user_id in self.subscribed_artists:
                    score = min(score + self.artist_boost, 1.0)

                if score < self.min_match_score:
                    continue
            else:
                score = 0.0
                contributions_by_id[illust.id] = PreferenceContributions()
                # 无 XP 时，关注画师也给予基础分
                if illust.user_id in self.subscribed_artists:
                    score = self.artist_boost
            
            # 语义匹配加成 (可选)
            semantic_score = 0.0
            if user_embedding and self.embedder:
                illust_emb = illust_embeddings_cache.get(illust.id)
                if illust_emb:
                    # 使用缓存
                    similarity = self.embedder.cosine_similarity(user_embedding, illust_emb)
                    semantic_score = self.embedder.normalize_similarity(similarity)
                else:
                    # 记录需要计算的作品
                    uncached_embeddings.append((illust, score))
                    continue  # 跳过，后面批量处理
                
                # 加权组合: (1-semantic_weight)*tag_score + semantic_weight*semantic_score
                semantic_weight = self.embedder.semantic_weight
                score = (1 - semantic_weight) * score + semantic_weight * semantic_score
            
            # 来源加成 (借鉴 X 算法 OON Scorer)
            source = getattr(illust, 'source', 'xp_search')
            source_multiplier = self.source_boost.get(source, 1.0)
            score *= source_multiplier
            
            scored_result.append((illust, score))
        
        # 批量计算未缓存的作品 Embedding
        if uncached_embeddings and user_embedding and self.embedder:
            try:
                texts = [", ".join(ill.tags[:10]) for ill, _ in uncached_embeddings]
                embeddings = await self.embedder.embed_batch(texts)
                
                to_save = []
                for i, (illust, tag_score) in enumerate(uncached_embeddings):
                    emb = embeddings[i]
                    if emb:
                        to_save.append((illust.id, emb, self.embedder.model))
                        similarity = self.embedder.cosine_similarity(user_embedding, emb)
                        semantic_score = self.embedder.normalize_similarity(similarity)
                        semantic_weight = self.embedder.semantic_weight
                        score = (1 - semantic_weight) * tag_score + semantic_weight * semantic_score
                    else:
                        score = tag_score
                    
                    # 来源加成
                    source = getattr(illust, 'source', 'xp_search')
                    source_multiplier = self.source_boost.get(source, 1.0)
                    score *= source_multiplier
                    
                    scored_result.append((illust, score))
                
                # 保存新计算的 Embedding
                if to_save:
                    await db.save_illust_embeddings_batch(to_save)
                    logger.info(f"已缓存 {len(to_save)} 个作品 Embedding")
                    
            except Exception as e:
                logger.error(f"批量 Embedding 计算失败: {e}")
                # Fallback: 只用 Tag 分数
                for illust, tag_score in uncached_embeddings:
                    source = getattr(illust, 'source', 'xp_search')
                    source_multiplier = self.source_boost.get(source, 1.0)
                    scored_result.append((illust, tag_score * source_multiplier))
        
        # 6. 综合排序：match_score * weight + normalized_bookmark * (1-weight) + 随机打散
        if scored_result:
            import random
            max_bookmark = max(item[0].bookmark_count for item in scored_result) or 1
            
            def sort_key(item):
                illust, score = item
                normalized_bookmark = illust.bookmark_count / max_bookmark
                base_score = score * self.match_weight + normalized_bookmark * (1 - self.match_weight)
                # 随机打散：添加随机噪声使每次排序结果不同
                if self.shuffle_factor > 0:
                    noise = random.uniform(-self.shuffle_factor, self.shuffle_factor)
                    return base_score + noise
                return base_score
            
            scored_result.sort(key=sort_key, reverse=True)
        
        # 构建 illust -> score 的映射
        score_map = {item[0].id: item[1] for item in scored_result}
        sorted_illusts = [item[0] for item in scored_result]
        
        # 优化标签展示顺序：feature-first，Identity 数量受限，AI 判定的 Identity 靠后
        if xp_profile:
            await self.apply_display_tags(sorted_illusts, xp_profile, tag_classifications=tag_classifications)
        
        # 6.1 AI 精排 (可选) - 使用 LLM 对候选作品进行二次评分
        if self.ai_scorer and self.ai_scorer.enabled and xp_profile:
            try:
                candidates_for_ai = sorted_illusts[:self.ai_scorer.max_candidates]
                if len(candidates_for_ai) > 5:  # 至少需要一定数量才有意义
                    # 获取近期反馈
                    recent_likes = await db.get_recent_liked_tags(limit=5)
                    recent_dislikes = await db.get_recent_disliked_tags(limit=5)
                    
                    ai_scores = await self.ai_scorer.score_candidates(
                        candidates_for_ai,
                        xp_profile,
                        recent_likes,
                        recent_dislikes
                    )
                    
                    if ai_scores:
                        # 混合 AI 分数和基础分数
                        score_map = self.ai_scorer.blend_scores(score_map, ai_scores)
                        # 重新排序
                        scored_result = [(ill, score_map.get(ill.id, 0)) for ill in sorted_illusts]
                        scored_result.sort(key=lambda x: x[1], reverse=True)
                        sorted_illusts = [item[0] for item in scored_result]
                        logger.info("AI 精排已应用")
            except Exception as e:
                logger.warning(f"AI 精排失败: {e}")
        
        # 7. 多样性控制：Identity / 画师多样性衰减 + 硬性限制
        if self.ip_diversity_enabled:
            identity_position = {}
            for illust, score in scored_result:
                primary_identity = self._get_primary_identity_tag(illust, tag_classifications, xp_profile)
                if not primary_identity:
                    continue

                pos = identity_position.get(primary_identity, 0)
                multiplier = (1.0 - self.ip_diversity_floor) * (self.ip_diversity_decay ** pos) + self.ip_diversity_floor
                score_map[illust.id] = score * multiplier
                identity_position[primary_identity] = pos + 1

            scored_result = [(ill, score_map[ill.id]) for ill, _ in scored_result]
            scored_result.sort(key=lambda x: x[1], reverse=True)
            sorted_illusts = [item[0] for item in scored_result]

        # 借鉴 X 算法 AuthorDiversityScorer: 同一画师后续作品分数递减
        if self.diversity_enabled:
            # 应用画师多样性衰减
            artist_position = {}  # 记录每个画师已出现的位置
            for illust, score in scored_result:
                pos = artist_position.get(illust.user_id, 0)
                # 衰减公式: (1.0 - floor) × decay^position + floor
                multiplier = (1.0 - self.diversity_floor) * (self.diversity_decay ** pos) + self.diversity_floor
                new_score = score * multiplier
                score_map[illust.id] = new_score
                artist_position[illust.user_id] = pos + 1
            
            # 重新排序
            scored_result = [(ill, score_map[ill.id]) for ill, _ in scored_result]
            scored_result.sort(key=lambda x: x[1], reverse=True)
            sorted_illusts = [item[0] for item in scored_result]
        
        # 硬性限制每个画师的作品数
        artist_count = {}
        diverse_result = []
        for illust in sorted_illusts:
            count = artist_count.get(illust.user_id, 0)
            if count < self.max_per_artist:
                # 将匹配度附加到对象上（动态属性）
                illust.match_score = score_map.get(illust.id, 0.0)
                diverse_result.append(illust)
                artist_count[illust.user_id] = count + 1
        
        # Daily Slate applies Motive Mix and Identity Caps after ranking.
        slate_result = None
        if self.daily_slate.enabled:
            slate_result = self.daily_slate.compose(
                diverse_result,
                self.daily_limit,
                tag_classifications,
                xp_profile or {},
                contributions_by_id,
            )
            final_result = list(slate_result.selected)
        # 7. 探索比例：只从常规 Top N 之外挑 feature 候选，避免把原本就会入选的强图改名成探索
        elif self.exploration_ratio > 0 and len(diverse_result) > self.daily_limit:
            import random
            explore_count = int(self.daily_limit * self.exploration_ratio)
            main_count = self.daily_limit - explore_count

            ordinary_result = diverse_result[:self.daily_limit]
            main_result = ordinary_result[:main_count]
            candidate_pool = diverse_result[self.daily_limit:self.daily_limit + (self.daily_limit * 2)]
            feature_pool = [
                illust
                for illust in candidate_pool
                if contributions_by_id.get(
                    illust.id,
                    PreferenceContributions(),
                ).feature_match_count > 0
                and contributions_by_id.get(
                    illust.id,
                    PreferenceContributions(),
                ).feature
                >= contributions_by_id.get(
                    illust.id,
                    PreferenceContributions(),
                ).identity
            ]

            if len(feature_pool) >= explore_count:
                explore_picks = random.sample(feature_pool, explore_count)
            else:
                explore_picks = list(feature_pool)

            restore_count = explore_count - len(explore_picks)
            restore_picks = ordinary_result[main_count:main_count + restore_count]
            final_result = main_result + explore_picks + restore_picks

            if explore_picks:
                random.shuffle(final_result)
            logger.info(f"探索推荐: 混入 {len(explore_picks)} 个潜力作品")
        else:
            # 8. 每日上限
            final_result = diverse_result[:self.daily_limit]

        # 记录匹配度日志
        if xp_profile and scored_result:
            top_3 = scored_result[:3]
            log_items = [f"{i[0].title[:10]}(score={i[1]:.2f})" for i in top_3]
            logger.info(f"匹配度 Top3: {', '.join(log_items)}")
        
        # 输出过滤原因统计
        try:
            logger.info(
                "过滤原因统计: "
                f"pushed={reason_stats['pushed']} | "
                f"time={reason_stats['time']} | "
                f"artist={reason_stats['artist']} | "
                f"blacklist={reason_stats['blacklist']} | "
                f"muted={reason_stats['muted']} | "
                f"ai={reason_stats['ai']} | "
                f"r18={reason_stats['r18']} | "
                f"ugoira={reason_stats['ugoira']}"
            )
            # 保存到实例属性供外部获取
            self._last_filter_reasons = reason_stats
        except Exception:
            pass
        
        logger.info(f"过滤后剩余 {len(final_result)} 个作品 (涉及 {len(artist_count)} 个画师)")
        return ContentFilterResult(
            selected=tuple(final_result),
            ranked=tuple(diverse_result),
            daily_slate=slate_result,
        )

    async def _classify_tags_for_illusts(self, illusts: list[Illust]) -> dict:
        if not self.tag_classifier or not illusts:
            return {}

        normalized_tags: list[str] = []
        for illust in illusts:
            for tag in illust.tags or []:
                normalized = self.tag_resolver.resolve(tag)
                if normalized:
                    normalized_tags.append(normalized)

        if not normalized_tags:
            return {}

        try:
            return await self.tag_classifier.classify_tags(normalized_tags)
        except Exception as e:
            logger.warning(f"标签分类失败，回退为 XP 权重排序: {e}")
            return {}

    async def apply_display_tags(
        self,
        illusts: list[Illust],
        xp_profile: dict[str, float],
        tag_classifications: Optional[dict] = None,
    ) -> None:
        """构建用于消息展示的标签顺序。"""
        await self._apply_display_tags(illusts, xp_profile, tag_classifications=tag_classifications)

    async def _apply_display_tags(
        self,
        illusts: list[Illust],
        xp_profile: dict[str, float],
        tag_classifications: Optional[dict] = None,
    ) -> None:
        """构建用于消息展示的标签顺序。"""
        per_illust_tags: dict[int, list[tuple[str, str, float]]] = {}

        for illust in illusts:
            tag_rows: list[tuple[str, str, float]] = []
            for tag in illust.tags:
                normalized_tag = self.tag_resolver.resolve(tag)
                tag_lower = tag.lower()
                if normalized_tag in self.blacklist_tags or tag_lower in self.blacklist_tags:
                    continue

                score = xp_profile.get(normalized_tag, xp_profile.get(tag_lower, 0.0))
                tag_rows.append((tag, normalized_tag, score))

            per_illust_tags[illust.id] = tag_rows

        classifications = tag_classifications or {}
        if not classifications and self.tag_classifier:
            classifications = await self._classify_tags_for_illusts(illusts)

        for illust in illusts:
            feature_tags: list[tuple[str, float]] = []
            identity_tags: list[tuple[str, float, int]] = []
            for tag, normalized_tag, score in per_illust_tags.get(illust.id, []):
                classification = classifications.get(normalized_tag)
                if classification and is_identity_category(classification):
                    source_rank = 1 if classification.source == "ai" else 0
                    identity_tags.append((tag, score, source_rank))
                elif classification is None or is_seed_category(classification):
                    feature_tags.append((tag, score))

            feature_tags.sort(key=lambda item: item[1], reverse=True)
            identity_tags.sort(key=lambda item: (item[2], -item[1]))

            illust.display_tags = [
                *(tag for tag, _ in feature_tags),
                *(tag for tag, _, _ in identity_tags[:self.display_tags_max_ip_count]),
            ]

    def _get_primary_identity_tag(
        self,
        illust: Illust,
        tag_classifications: Optional[dict] = None,
        xp_profile: Optional[dict[str, float]] = None,
    ) -> Optional[str]:
        primary_identity = None
        primary_identity_score = float("-inf")

        for tag in illust.tags or []:
            normalized_tag = self.tag_resolver.resolve(tag)
            classification = (tag_classifications or {}).get(normalized_tag)
            if not is_identity_category(classification):
                continue

            tag_score = 0.0
            if xp_profile:
                tag_score = xp_profile.get(normalized_tag, xp_profile.get(tag.lower(), 0.0))

            if primary_identity is None or tag_score > primary_identity_score:
                primary_identity = normalized_tag
                primary_identity_score = tag_score

        return primary_identity

    @staticmethod
    def _normalize_max_ip_count(value) -> int:
        try:
            if isinstance(value, bool):
                raise ValueError
            return max(0, int(value))
        except (TypeError, ValueError):
            return 2
    
    def check_illust(self, illust: Illust) -> bool:
        """检查单个作品是否满足基本过滤条件 (Blacklist, AI, R18, Time)"""
        # 1. 基础有效性
        if not illust.id: return False
        
        # 2. 时间过滤 (如果配置)
        if self.min_create_days > 0:
            from datetime import datetime, timedelta
            time_threshold = datetime.now(illust.create_date.tzinfo) - timedelta(days=self.min_create_days)
            if illust.create_date < time_threshold:
                return False

        # 3. R-18G / Blacklist
        if self._has_blacklisted_tag(illust):
            return False
            
        # 4. AI 排除
        if self.exclude_ai:
            # 4.1 Pixiv 官方标记 (纯AI)
            if illust.ai_type == 2:
                return False
            # 4.2 标签关键词检测 (增强检测)
            if self._is_ai_by_tags(illust):
                return False
            
        # 5. R-18 Mode
        mode_str = str(self.r18_mode).lower()
        if mode_str in ("true", "r18_only", "pure"):
            if not illust.is_r18: return False
        elif mode_str in ("safe", "18-", "clean"):
            if illust.is_r18: return False
            
        # 6. Ugoira Mode
        if self.skip_ugoira and getattr(illust, 'type', 'illust') == 'ugoira':
            return False
        
        # 7. 内容类型过滤 (illust/manga)
        if self.content_type != 'all':
            illust_type = getattr(illust, 'type', 'illust')
            if self.content_type == 'illust' and illust_type != 'illust':
                return False
            if self.content_type == 'manga' and illust_type != 'manga':
                return False
            
        return True
    
    def _is_ai_by_tags(self, illust: Illust) -> bool:
        """通过标签检测 AI 作品（增强检测）"""
        # AI 相关关键词（多语言）
        ai_keywords = [
            # 英文
            "ai", "stable diffusion", "midjourney", "novelai", "dall-e", 
            "ai generated", "ai art", "ai illustration", "ai drawing",
            "generated by ai", "ai创作", "ai绘画",
            # 中文
            "人工智能", "ai生成", "ai绘图", "ai创作", "ai画", "ai作品",
            "stable diffusion", "midjourney", "novelai",
            # 日文
            "ai", "ai生成", "aiイラスト", "ai絵", "ai作品", "stable diffusion",
            "midjourney", "novelai", "ai描いた", "aiが生成"
        ]
        
        # 检查每个标签
        for tag in illust.tags:
            tag_lower = tag.lower()
            for keyword in ai_keywords:
                if keyword.lower() in tag_lower:
                    logger.debug(f"AI 检测: 作品 {illust.id} 标签 '{tag}' 包含 AI 关键词 '{keyword}'")
                    return True
        
        return False

    def _has_blacklisted_tag(self, illust: Illust) -> bool:
        """检查是否包含黑名单Tag"""
        for tag in illust.tags:
            if self.tag_resolver.resolve(tag) in self.blacklist_tags:
                return True
        return False
    
    async def add_to_blacklist(self, tag: str):
        """动态添加黑名单Tag"""
        self.blacklist_tags.add(tag.lower())
        logger.info(f"Tag '{tag}' 已加入黑名单")
