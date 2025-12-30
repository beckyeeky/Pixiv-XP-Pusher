"""
内容获取模块
双策略：XP搜索 + 画师订阅 + 排行榜
"""
import logging
import random
from datetime import datetime, timedelta
from typing import Optional

from pixiv_client import Illust, PixivClient
import database as db
from utils import expand_search_query

logger = logging.getLogger(__name__)


class ContentFetcher:
    """内容获取器"""
    
    def __init__(
        self,
        client: PixivClient,
        bookmark_threshold: dict[str, int] = None,
        date_range_days: int = 7,
        subscribed_artists: Optional[list[int]] = None,
        discovery_rate: float = 0.1,
        ranking_config: Optional[dict] = None
    ):
        self.client = client
        self.bookmark_threshold = bookmark_threshold or {"search": 1000, "subscription": 0}
        self.date_range_days = date_range_days
        self.subscribed_artists = subscribed_artists or []
        self.discovery_rate = discovery_rate
        
        # 排行榜配置
        self.ranking_config = ranking_config or {}
        self.ranking_enabled = self.ranking_config.get("enabled", False)
        self.ranking_modes = self.ranking_config.get("modes", ["day"])
        self.ranking_limit = self.ranking_config.get("limit", 100)
    
    def _adaptive_threshold(self, base: int, tag_weight: float, is_combination: bool = False) -> int:
        """
        自适应收藏阈值
        
        根据 Tag 权重动态调整阈值：
        - 高权重 Tag（用户热爱）：保持高阈值，确保质量
        - 低权重 Tag（尝试发现）：降低阈值，扩大搜索范围
        - 组合搜索：额外降低阈值（更精准匹配）
        
        Args:
            base: 基础阈值
            tag_weight: Tag 的 XP 权重 (0-1 归一化后)
            is_combination: 是否是组合搜索
        
        Returns:
            调整后的阈值
        """
        # 权重越低，阈值越低
        # weight=1.0 -> multiplier=1.0 (保持原阈值)
        # weight=0.5 -> multiplier=0.5
        # weight=0.2 -> multiplier=0.3 (最低 30%)
        multiplier = max(0.3, tag_weight)
        
        # 组合搜索额外降低 50%
        if is_combination:
            multiplier *= 0.5
        
        return max(100, int(base * multiplier))  # 最低 100
    
    async def discover(
        self,
        xp_tags: list[tuple[str, float]],
        limit: int = 50
    ) -> list[Illust]:
        """
        策略A：基于XP的广泛搜索
        
        Args:
            xp_tags: [(tag, weight), ...] 排序后的XP标签
            limit: 获取数量
        """
        if not xp_tags:
            logger.warning("无XP标签，跳过搜索")
            return []
        
        all_illusts = []
        
        # 策略 1: 搜索高权重组合 (Smart Search)
        top_pairs = await db.get_top_tag_pairs(limit=50)
        used_tags = set()
        
        for t1, t2, _ in top_pairs:
            if len(all_illusts) >= limit * 0.6:  # 60% 配额给组合搜索
                break
                
            pair_key = tuple(sorted([t1, t2]))
            if pair_key in used_tags:
                continue
            used_tags.add(pair_key)
            
            # 检查同义词冗余
            q1 = expand_search_query(t1)
            q2 = expand_search_query(t2)
            
            # 如果一个是另一个的扩展结果（如同义词），或者是包含关系
            # 例如: t1="明日方舟", t2="Arknights" -> q1==q2 -> Skip
            # 例如: t1="loli", q2="(loli OR ...)" -> Skip
            if q1 == q2 or t1 in q2 or t2 in q1:
                logger.debug(f"跳过冗余组合: {t1} + {t2}")
                continue
            
            # 降低组合搜索的阈值，因为匹配更精确
            threshold = self.bookmark_threshold["search"] // 2
            
            # 1. 尝试从数据库获取该标准化标签对应的最高频原始标签
            raw_t1 = await db.get_best_search_tag(t1)
            raw_t2 = await db.get_best_search_tag(t2)
            
            # 2. 结合预设字典进行扩展
            # 如果原始标签和标准标签不一样，且不在预设字典里，手动拼 OR
            base_q1 = expand_search_query(t1)
            final_q1 = base_q1
            if raw_t1 != t1 and raw_t1 not in base_q1:
                 # 简单处理：把最高频原始词拼进去
                 if "(" in base_q1:
                     final_q1 = base_q1[:-1] + f" OR {raw_t1})"
                 else:
                     final_q1 = f"({base_q1} OR {raw_t1})"

            base_q2 = expand_search_query(t2)
            final_q2 = base_q2
            if raw_t2 != t2 and raw_t2 not in base_q2:
                 if "(" in base_q2:
                     final_q2 = base_q2[:-1] + f" OR {raw_t2})"
                 else:
                     final_q2 = f"({base_q2} OR {raw_t2})"

            illusts = await self.client.search_illusts(
                tags=[final_q1, final_q2],
                bookmark_threshold=threshold,
                date_range_days=self.date_range_days,
                limit=30
            )
            all_illusts.extend(illusts)
            logger.info(f"组合搜索 '{t1.split(' ')[0]} + {t2.split(' ')[0]}' 获取 {len(illusts)} 个作品")

        # 策略 2: 随机组合高权重Tag (Fallback)
        remaining_limit = limit - len(all_illusts)
        if remaining_limit > 0:
            for _ in range(3):
                tags_to_search = self._weighted_sample(xp_tags, k=1) # 降级为单Tag搜索更稳
                if not tags_to_search:
                    continue
                
                tag = tags_to_search[0]
                if tag in [t for pair in used_tags for t in pair]: # 避免重复
                    continue
                
                # 同样的扩展逻辑
                raw_tag = await db.get_best_search_tag(tag)
                base_q = expand_search_query(tag)
                final_q = base_q
                if raw_tag != tag and raw_tag not in base_q:
                     if "(" in base_q:
                         final_q = base_q[:-1] + f" OR {raw_tag})"
                     else:
                         final_q = f"({base_q} OR {raw_tag})"

                illusts = await self.client.search_illusts(
                    tags=[final_q],
                    bookmark_threshold=self.bookmark_threshold["search"],
                    date_range_days=self.date_range_days,
                    limit=remaining_limit // 2
                )
                all_illusts.extend(illusts)
        
        # 探索模式：混入随机热门
        if random.random() < self.discovery_rate:
             # 这里可以添加探索逻辑...
             pass
        
        logger.info(f"XP搜索获取 {len(all_illusts)} 个作品")
        return all_illusts
    
    async def check_subscriptions(self) -> list[Illust]:
        """
        策略B：检查订阅画师更新 + 关注者新作
        """
        all_illusts = []
        seen_ids = set()
        
        # 1. 获取关注者时间轴 (高效)
        try:
            feed_illusts = await self.client.fetch_follow_latest(limit=100)
            for illust in feed_illusts:
                if illust.id not in seen_ids:
                    all_illusts.append(illust)
                    seen_ids.add(illust.id)
        except Exception as e:
            logger.error(f"获取关注时间轴失败: {e}")
            
        # 2. 检查配置中的特定订阅 (补充)
        # 如果订阅列表只有几个，检查一下也无妨；如果是空的则跳过
        if self.subscribed_artists:
            since = datetime.now().astimezone() - timedelta(days=self.date_range_days)
            for artist_id in self.subscribed_artists:
                # 如果刚才的 feed 里已经有了很多该画师的图，或许可以跳过？
                # 简单起见，还是查一下，但限制数量
                try:
                    illusts = await self.client.get_user_illusts(
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
        
        for mode in self.ranking_modes:
            try:
                illusts = await self.client.get_ranking(
                    mode=mode,
                    limit=self.ranking_limit // len(self.ranking_modes)
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
        
        # 使用权重作为选择概率
        total = sum(weights)
        probs = [w / total for w in weights]
        
        selected = []
        available = list(range(len(tags)))
        
        for _ in range(k):
            if not available:
                break
            
            r = random.random()
            cumsum = 0
            for i in available:
                cumsum += probs[i]
                if r <= cumsum:
                    selected.append(tags[i])
                    available.remove(i)
                    break
        
        return selected
    
    def _adaptive_threshold(self, tags: list[str], base: int) -> int:
        """
        智能阈值：根据Tag热度调整
        - 冷门Tag：降低阈值
        - 热门Tag：保持或提高阈值
        
        TODO: 可以通过API查询Tag作品数来判断热度
        目前使用简化策略：多Tag组合降低阈值
        """
        # 组合Tag越多，匹配难度越大，降低阈值
        multiplier = 1.0 - (len(tags) - 1) * 0.2
        return max(100, int(base * multiplier))
