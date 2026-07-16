"""
XP 画像构建模块
分析收藏Tag，构建用户XP权重
"""
import logging
import math
import itertools
import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Union

from pixiv_client import Illust, PixivClient
import database as db
from tag_mapping import TagIdentityResolver



logger = logging.getLogger(__name__)


# 默认 IP 列表 (作为 Fallback)
DEFAULT_IP_TAGS = {
    # 游戏
    "blue_archive", "honkai_star_rail", "nikke", "arknights",
    "arknights_endfield", "zenless_zone_zero", "wuthering_waves",
    "honkai_impact_3rd", "uma_musume", "uma_musume_pretty_derby",
    "genshin_impact", "starrail", "zenless_zone_zero", "zzz",
    "azur_lane", "fate_grand_order", "fgo", "princess_connect",
    "priconne", "re_dive", "idolmaster", "idolmaster_cinderella_girls",
    "idolmaster_shiny_colors", "idolmaster_million_live",
    "bang_dream", "bandori", "lovelive", "lovelive_sunshine",
    "lovelive_nijigasaki", "lovelive_superstar", "project_sekai",
    "proseka", "vocaloid", "touhou", "kantai_collection", "kancolle",
    # 动画
    "spy_x_family", "chainsaw_man", "jujutsu_kaisen", "kimetsu_no_yaiba",
    "attack_on_titan", "shingeki_no_kyojin", "one_piece", "naruto",
    "pokemon", "digimon", "dragon_ball", "evangelion", "eva",
    "sword_art_online", "sao", "re_zero", "re_kara_hajimeru_isekai_seikatsu",
    "mushoku_tensei", "overlord", "slime", "tensei_shitara_slime_datta_ken",
    # 通用
    "original", "copyright", "game", "anime", "manga", "comic",
}

class XPProfiler:
    """XP画像构建器"""
    
    def __init__(
        self,
        client: PixivClient,
        stop_words: Optional[list[str]] = None,
        discovery_rate: float = 0.1,
        time_decay_days: int = 180,
        saturation_threshold: float = 0.5,
        # 新增参数
        ip_tags: Optional[Union[list, str]] = None,
        ip_weight_discount: float = 1.0,
        boost_tags: Optional[dict] = None  # {tag: multiplier}
    ):
        self.client = client
        self.stop_words = set(stop_words or [])
        self.discovery_rate = discovery_rate
        self.time_decay_days = time_decay_days
        self.saturation_threshold = saturation_threshold  # 高频 Tag 饱和度阈值
        self._blocked_artist_ids: set[int] = set()  # 初始化，由 load_blacklist 填充
        
        # IP 标签配置
        self.ip_weight_discount = ip_weight_discount
        self.ip_tags = set()
        
        self.tag_resolver = TagIdentityResolver()
        
        # 手动加权配置
        self.boost_tags = boost_tags or {}
        
        # 加载 IP 标签
        if ip_tags:
            if isinstance(ip_tags, str):
                # 文件路径
                p = Path(ip_tags)
                if p.exists():
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            tags = json.load(f)
                            # 归一化：去掉 Danbooru 的 : 命名空间分隔符，处理连续下划线
                            # 例如: honkai:_star_rail -> honkai_star_rail
                            self.ip_tags = set(t.replace(":", "").replace("__", "_") for t in tags)
                            logger.info(f"已从文件加载 {len(self.ip_tags)} 个 IP 标签")
                    except Exception as e:
                        logger.error(f"加载 IP 标签文件失败: {e}")
                else:
                    logger.warning(f"IP 标签文件不存在: {ip_tags}")
            else:
                # 列表 - 去掉 : 命名空间分隔符，处理连续下划线
                self.ip_tags = set(t.replace(":", "").replace("__", "_") for t in ip_tags)
        else:
            # 默认使用内置列表 (如果没配，但 discount < 1.0 时也许有用？或者干脆不用)
            # 为了兼容性，如果用户没配 ip_tags 但配了 discount，我们用默认列表
            if self.ip_weight_discount < 1.0:
                 self.ip_tags = DEFAULT_IP_TAGS
                 logger.info(f"使用内置 IP 标签列表 ({len(self.ip_tags)} 个)")

        if self.ip_tags and self.ip_weight_discount < 1.0:
            logger.info(f"🎮 IP 降权已启用: {len(self.ip_tags)} 个标签 ×{self.ip_weight_discount}")
        
        # 添加默认停用词（归一化为小写）
        # Pixiv 常见无意义标签
        default_stop_words = [
            # 通用描述
            "original", "オリジナル", "manga", "漫画", "pixiv",
            "illustration", "イラスト", "練習", "practice",
            "落書き", "doodle", "sketch", "スケッチ",
            "drawing", "art", "artwork", "fanart", "ファンアート",
            "digital", "デジタル", "アナログ", "analog",
            
            # 分级标签
            "R-18", "R-18G", "R18", "NSFW", "SFW", "safe",
            
            # 数字/编号类
            "1000users入り", "500users入り", "100users入り", "50users入り",
            "5000users入り", "10000users入り", "users入り",
            "1000bookmarks", "500bookmarks", "100bookmarks",
            
            # 活动/比赛标签
            "コンテスト", "contest", "企画", "project",
            "お題", "リクエスト", "request", "commission",
            "落書き集", "まとめ", "詰め合わせ", "log",
            
            # 平台标签
            "twitter", "fanbox", "patreon", "skeb",
            "pixivfanbox", "fantia",
            
            # 通用形容词
            "cute", "kawaii", "かわいい", "可愛い",
            "beautiful", "綺麗", "pretty", "sexy",
            "cool", "かっこいい", "カッコイイ",
            
            # 其他无意义
            "girls", "girl", "boy", "boys", "woman", "man",
            "female", "male", "solo", "1girl", "1boy",
            "2girls", "2boys", "multiple_girls", "multiple_boys",
            "背景", "background", "風景", "landscape",
            "創作", "オリキャラ", "original_character", "oc",
            "うちの子", "看板娘", "版権", "二次創作",
            "仕事絵", "お仕事", "work",
        ]
        for word in default_stop_words:
            self.stop_words.add(word.lower().replace(" ", "_"))
            
    async def load_blacklist(self):
        """从数据库加载黑名单 (仅包括用户手动屏蔽的)"""
        try:
            accepted_aliases = await db.get_accepted_tag_aliases("equivalent")
            self.tag_resolver = TagIdentityResolver(accepted_aliases)
            # 1. 仅加载手动屏蔽的标签
            # 用户明确要求：没确认就不屏蔽，因此不加载 high-dislike counts
            blocked_tags = await db.get_blocked_tags()
            for tag in blocked_tags:
                self.stop_words.add(self._normalize_tag(tag))
            
            # 2. 加载屏蔽的画师 ID
            blocked_artists = await db.get_blocked_artists()
            self._blocked_artist_ids = {artist_id for artist_id, _ in blocked_artists}
            
            logger.info(f"已加载黑名单: {len(blocked_tags)} 个手动屏蔽Tag + {len(blocked_artists)} 个屏蔽画师")
        except Exception as e:
            logger.error(f"加载黑名单失败: {e}")
            self._blocked_artist_ids = set()

            logger.error(f"加载黑名单失败: {e}")
            self._blocked_artist_ids = set()

    async def build_profile(
        self,
        user_id: int,
        scan_limit: int = 500,
        include_private: bool = False
    ) -> dict[str, float]:
        """
        扫描收藏，构建XP权重字典
        
        Args:
            user_id: 目标用户ID
            scan_limit: 扫描收藏数量
            include_private: 包含私密收藏
        
        Returns:
            {tag: weight} 权重字典
        """
        # 获取收藏
        await self.load_blacklist()  # 确保加载最新黑名单
        
        # 1. 加载本地缓存 ID
        cached_rows = await db.get_xp_bookmarks(user_id)
        cached_ids = {row['illust_id'] for row in cached_rows}
        
        # 2. 检查同步状态
        sync_key = f"sync_completed_{user_id}"
        is_completed = await db.get_state(sync_key) == "true"
        
        # 定义 Cursor Key 生成器
        def get_cursor_key(is_private):
            suffix = "private" if is_private else "public"
            return f"resume_cursor_{user_id}_{suffix}"

        # 通用获取逻辑封装
        async def fetch_segment(is_private):
            cursor_key = get_cursor_key(is_private)
            saved_cursor = await db.get_state(cursor_key)
            
            # 策略判断
            if is_completed:
                mode = "update" # 增量更新
                stop_ids = cached_ids
                skip_ids = None
                start_url = None
                do_tail_resume = False
            elif saved_cursor:
                mode = "jump"   # 高效跳转
                stop_ids = cached_ids
                skip_ids = None
                start_url = None
                do_tail_resume = True
            else:
                mode = "slow"   # 慢速扫描 (首次或丢失游标)
                stop_ids = None
                skip_ids = cached_ids
                start_url = None
                do_tail_resume = False
                
            desc = "私密" if is_private else "公开"
            logger.info(f"[{desc}] 模式: {mode}, 缓存: {len(cached_ids)}")

            # 回调工厂: 区分 Head 更新还是 Tail 更新
            def make_callback(update_cursor_key=None):
                async def _cb(items, next_url):
                    await db.save_xp_bookmarks(user_id, items)
                    if update_cursor_key and next_url:
                        await db.set_state(update_cursor_key, next_url)
                return _cb

            fetched = []
            
            # 1. 头部扫描 (填补最新的 Gap)
            # 即使在 update 模式，也是跑这个。
            # 如果是 jump 模式，这里负责只抓最新的，不要覆盖 saved_cursor
            logger.info(f"[{desc}] 正在扫描头部...")
            head_items = await self.client.get_bookmarks(
                user_id,
                limit=scan_limit,
                private=is_private,
                stop_ids=stop_ids,
                skip_ids=skip_ids,
                start_url=start_url,
                on_batch=make_callback(update_cursor_key=cursor_key if mode == "slow" else None)
            )
            fetched.extend(head_items)
            
            # 2. 尾部跳转 (仅 Jump 模式)
            if do_tail_resume:
                logger.info(f"[{desc}] ⚡ 触发高效断点续传，直接跳转到: {saved_cursor[:60]}...")
                # 这里必须从 saved_cursor 开始，并且要更新 cursor_key (推进进度)
                # 依然传入 stop_ids=cached_ids，万一 хво巴也接上了呢
                tail_items = await self.client.get_bookmarks(
                    user_id,
                    limit=scan_limit, # 剩余额度？暂不精确控制
                    private=is_private,
                    start_url=saved_cursor,
                    stop_ids=cached_ids,
                    on_batch=make_callback(update_cursor_key=cursor_key)
                )
                fetched.extend(tail_items)
                
            return fetched

        # 4. 执行获取
        bookmarks = await fetch_segment(False)
        
        if include_private and self.client._logged_in:
            private_bookmarks = await fetch_segment(True)
            bookmarks = bookmarks + private_bookmarks
        
        # 5. 标记同步完成
        if not is_completed:
            await db.set_state(sync_key, "true")
            logger.info("✅ 全量同步完成，标记为 [已完成]")
            # 清理游标？可选。留着也没事，下次 is_completed=True 会忽略它。
        
        # 6. 重新构建全量列表
        cached_rows = await db.get_xp_bookmarks(user_id)
        
        analyzed_illusts = []
        for row in cached_rows:
            # 数据库里存的时间可能是字符串，需转换
            cdate = row['illust_create_date']
            if isinstance(cdate, str):
                try:
                    cdate = datetime.fromisoformat(cdate)
                except:
                    cdate = datetime.now()
            
            # 预处理标签：去除 users入り 后缀等
            raw_tags = json.loads(row['tags'])
            cleaned_tags = [self._normalize_tag(t) for t in raw_tags]
            # 过滤空标签并去重（保持顺序）
            cleaned_tags = list(dict.fromkeys(t for t in cleaned_tags if t))
            
            analyzed_illusts.append(Illust(
                id=row['illust_id'],
                title="Cached",
                user_id=user_id,
                user_name="",
                tags=cleaned_tags,
                tags_translated=[],
                bookmark_count=0,
                view_count=0,
                page_count=1,
                image_urls=[],
                is_r18=False,
                ai_type=0,
                create_date=cdate
            ))
            
        bookmarks = analyzed_illusts
        logger.info(f"XP分析数据源: {len(bookmarks)} 个收藏作品 (含本地历史)")
        
        # 统计Tag出现次数和时间 (存储 illust_id 用于正确计算 DF)
        # 支持权重系数 (liked items = 0.5x，因为 apply_feedback 已给过 1.0x)
        tag_occurrences: dict[str, list[tuple[int, datetime, float]]] = defaultdict(list)
        
        # 获取已点赞的作品ID (避免双倍计分)
        liked_ids = await db.get_liked_illusts()
        
        for illust in bookmarks:
            # 已点赞的作品给 0.5x 权重 (与反馈的 1.0 合计 = 1.5x)
            weight_mult = 0.5 if illust.id in liked_ids else 1.0
            
            for tag in illust.tags:
                normalized = self._normalize_tag(tag)
                if normalized and normalized not in self.stop_words:
                    tag_occurrences[normalized].append((illust.id, illust.create_date, weight_mult))
        
        # 计算权重
        total_docs = len(bookmarks)
        profile = {}
        tag_df = {}  # 用于 PMI 计算
        
        # 先计算所有 Tag 的 DF 并检测饱和度
        saturated_tags = []
        for tag, occurrences in tag_occurrences.items():
            unique_illusts = set(item[0] for item in occurrences)
            df = len(unique_illusts)
            tag_df[tag] = df
            
            # 饱和度检测：高频 Tag 自动加入停用词
            saturation = df / total_docs if total_docs > 0 else 0
            if saturation > self.saturation_threshold:
                saturated_tags.append((tag, saturation))
                self.stop_words.add(tag)
        
        if saturated_tags:
            logger.info(f"🎯 饱和度检测：{len(saturated_tags)} 个高频 Tag 自动加入停用词")
            for tag, sat in saturated_tags[:5]:  # 只显示前5个
                logger.info(f"   - {tag}: {sat:.1%}")
        
        for tag, occurrences in tag_occurrences.items():
            if tag in self.stop_words:
                continue  # 跳过饱和 Tag
                
            unique_illusts = set(item[0] for item in occurrences)
            dates = [item[1] for item in occurrences]
            weights = [item[2] for item in occurrences]  # 权重系数
            
            weight = self._calculate_weight(
                term_frequency=len(occurrences),
                document_frequency=len(unique_illusts),
                total_documents=total_docs,
                occurrence_dates=dates,
                weight_multipliers=weights  # 传入权重系数
            )
            profile[tag] = weight
        
        # 计算Tag组合权重 (Co-occurrence)
        pair_counts = Counter()
        for illust in bookmarks:
            # 获取该作品所有有效的 normalized tags
            valid_tags = []
            for tag in illust.tags:
                norm = self._normalize_tag(tag)
                if norm and norm not in self.stop_words:
                    valid_tags.append(norm)
            
            # 统计组合 (只统计高频Tag的组合以减少噪音)
            if len(valid_tags) >= 2:
                # 排序以保证 (A, B) 和 (B, A) 视为同一个组合
                valid_tags.sort()
                # 生成所有两两组合
                for t1, t2 in itertools.combinations(valid_tags, 2):
                    # 仅当两个Tag都在Profile中有一定权重时才统计（例如 Top 50）
                    # 这里简化为：所有组合都统计，但后续根据频率筛选
                    pair_counts[(t1, t2)] += 1
        
        # 保存热门组合 (使用 PMI 优化权重)
        pairs_to_save = []
        for (t1, t2), count in pair_counts.most_common(100):  # 扩大候选池
            # 计算 PMI = log(P(t1,t2) / (P(t1) * P(t2)))
            p_t1 = tag_df.get(t1, 1) / total_docs if total_docs > 0 else 0
            p_t2 = tag_df.get(t2, 1) / total_docs if total_docs > 0 else 0
            p_joint = count / total_docs if total_docs > 0 else 0
            
            # 防止除零，使用平滑
            pmi = math.log(p_joint / (p_t1 * p_t2 + 1e-10) + 1e-10)
            
            # 结合 PMI 和原权重，PMI 为负表示反相关，过滤掉
            if pmi > 0:
                weight = pmi * (profile.get(t1, 0) + profile.get(t2, 0))
                pairs_to_save.append((t1, t2, weight))
        
        # 只保留 Top 50
        pairs_to_save = sorted(pairs_to_save, key=lambda x: x[2], reverse=True)[:50]
            
        await db.update_xp_tag_pairs(pairs_to_save)
        
        # ============ 冷启动处理：收藏少时注入热门 Tag 弱先验 ============
        cold_start_threshold = 50  # 收藏少于此数时触发冷启动
        if len(bookmarks) < cold_start_threshold:
            logger.info(f"🧊 检测到冷启动场景 (收藏: {len(bookmarks)} < {cold_start_threshold})")
            try:
                popular_tags = await db.get_popular_tags(20)
                injected_count = 0
                for tag, freq in popular_tags:
                    normalized_tag = self._normalize_tag(tag)
                    if normalized_tag and normalized_tag not in profile and normalized_tag not in self.stop_words:
                        # 弱先验权重：频率 * 0.1（不会压过真实收藏）
                        prior_weight = freq * 0.1
                        profile[normalized_tag] = prior_weight
                        injected_count += 1
                if injected_count > 0:
                    logger.info(f"   注入 {injected_count} 个热门 Tag 作为弱先验")
            except Exception as e:
                logger.warning(f"冷启动注入失败: {e}")
        
        # IP 标签降权处理
        discounted_count = 0
        
        # IP 降权（Tag Alias 已经在统一 resolver seam 中应用）
        for tag in list(profile.keys()):
            if tag in self.ip_tags:
                # 检查是否已有 Boost (包含在 boost_tags 中)
                if tag in (self.boost_tags or {}):
                    continue # 交给后面的 boost 逻辑处理
                
                old_weight = profile[tag]
                profile[tag] = old_weight * self.ip_weight_discount
                discounted_count += 1
        
        if discounted_count > 0:
            logger.info(f"🎮 IP 标签降权完成: {discounted_count} 个标签 ×{self.ip_weight_discount}")
        
        # 手动加权处理
        if self.boost_tags:
            boosted_count = 0
            # 计算平均权重作为基准
            if profile:
                avg_weight = sum(profile.values()) / len(profile)
            else:
                avg_weight = 1.0
                
            for tag, multiplier in self.boost_tags.items():
                # 确保只处理有效的、非停用词的 Tag
                if tag in self.stop_words:
                    continue
                    
                if tag in profile:
                    # 现有 Tag：乘法加成
                    profile[tag] *= multiplier
                    boosted_count += 1
                else:
                    # 新 Tag (从未收藏过)：注入初始分 (0.5倍平均分 * 倍率)
                    # 这是一个很有用的功能，允许用户强行推从未见过的东西
                    profile[tag] = avg_weight * 0.5 * multiplier
                    boosted_count += 1
            
            if boosted_count > 0:
                logger.info(f"🚀 手动加权生效: {boosted_count} 个标签")

        # 保存到数据库 (现有代码)
        await db.update_xp_profile(profile)
        
        logger.info(f"构建XP画像完成，共 {len(profile)} 个Tag，{len(pairs_to_save)} 个热门组合")
        return profile
    
    def _normalize_tag(self, tag: str) -> str:
        """Resolve a tag through the single accepted identity seam."""
        return self.tag_resolver.resolve(tag)
    
    def _calculate_weight(
        self,
        term_frequency: int,
        document_frequency: int,
        total_documents: int,
        occurrence_dates: list[datetime],
        weight_multipliers: list[float] = None
    ) -> float:
        """
        权重计算（优化后的 TF-IDF + 时间衰减）
        
        weight = weighted_TF × IDF
        - weighted_TF = Σ(time_decay × weight_mult)
        - IDF = log(N / (df + 1)) + 1  (带平滑的标准IDF)
        """
        now = datetime.now(occurrence_dates[0].tzinfo if occurrence_dates else None)
        
        # 0. 高频饱和度过滤 (如果超过阈值如 50%，视为无意义停用词)
        df_ratio = document_frequency / total_documents if total_documents > 0 else 0
        if df_ratio > self.saturation_threshold:
            return 0.0

        # 计算带时间衰减的 TF (含权重系数)
        weighted_tf = 0
        for i, date in enumerate(occurrence_dates):
            days_ago = (now - date).days
            # 确保 days_ago 不为负
            days_ago = max(0, days_ago)
            decay = math.exp(-days_ago / self.time_decay_days)
            # 应用权重系数 (liked items = 0.5x)
            weighted_tf += decay * weight_multipliers[i]
        
        # 1. 对 TF 应用对数抑制 (防止数量堆积导致的线性无限增长)
        # log10(1 + 10) = 1.04
        # log10(1 + 100) = 2.0
        # log10(1 + 500) = 2.7
        # 即使有 1000 个收藏，权重也只比 100 个多 35%，而不是 1000%
        if weighted_tf > 0:
            weighted_tf = math.log10(1 + weighted_tf)
        
        # 2. 标准 IDF (带平滑防止除零)
        idf = math.log(total_documents / (document_frequency + 1)) + 1
        
        return weighted_tf * idf
    
    async def get_top_tags(self, n: int = 20) -> list[tuple[str, float]]:
        """获取权重最高的N个Tag"""
        profile = await db.get_xp_profile()
        sorted_tags = sorted(profile.items(), key=lambda x: x[1], reverse=True)
        return sorted_tags[:n]
    
    async def apply_feedback(self, illust: Illust, action: str, config: dict) -> dict:
        """
        应用用户反馈调整权重
        
        Args:
            illust: 作品对象
            action: 'like' | 'dislike'
            config: 反馈配置
        """
        accepted_aliases = await db.get_accepted_tag_aliases("equivalent")
        self.tag_resolver = TagIdentityResolver(accepted_aliases)

        like_boost = config.get("like_boost", 0.5)
        dislike_penalty = config.get("dislike_penalty", 0.3)
        dislike_threshold = config.get("dislike_threshold", 3)
        
        # 获取当前画像用于分级惩罚
        profile = await db.get_xp_profile()
        max_weight = max(profile.values()) if profile else 1
        
        disliked_tags: list[str] = []
        auto_blocked_artists: list[dict] = []
        
        for tag in illust.tags:
            normalized = self._normalize_tag(tag)
            if not normalized or normalized in self.stop_words:
                continue
            
            if action == "like":
                await db.adjust_tag_weight(normalized, like_boost)
                logger.debug(f"Tag '{normalized}' 权重 +{like_boost}")
            
            elif action == "dislike":
                # 分级惩罚：高权重 Tag 减少惩罚力度（可能是用户核心偏好）
                current_weight = profile.get(normalized, 0)
                weight_ratio = current_weight / max_weight if max_weight > 0 else 0
                # 高权重 Tag 最多减半惩罚
                adjusted_penalty = dislike_penalty * (1 - weight_ratio * 0.5)
                disliked_tags.append(normalized)
                
                await db.adjust_tag_weight(normalized, -adjusted_penalty)
                
                # 同时更新负向画像（用于主动排斥相似作品）
                await db.adjust_negative_weight(normalized, adjusted_penalty)
                
                await db.increment_tag_dislike(normalized)
                logger.debug(f"Tag '{normalized}' 权重 -{adjusted_penalty:.2f} (分级惩罚)")
        
        # 画师权重关联 + 自动屏蔽（连续 3 次 dislike 同画师）
        if illust.user_id:
            try:
                artist_delta = 1.0 if action == "like" else -1.0
                await db.update_artist_score(illust.user_id, artist_delta)
                logger.debug(f"画师 {illust.user_id} ({illust.user_name}) 权重 {artist_delta:+.1f}")

                # dislike 时检查：累计 dislike 评分 <= -3 且未屏蔽 → 自动屏蔽画师
                if action == "dislike":
                    artist_score = await db.get_artist_score(illust.user_id)
                    if artist_score <= -3 and not await db.is_artist_blocked(illust.user_id):
                        await db.block_artist(illust.user_id, illust.user_name)
                        auto_blocked_artists.append({
                            "artist_id": illust.user_id,
                            "artist_name": illust.user_name,
                        })
                        logger.info(f"画师 {illust.user_id} ({illust.user_name}) 累计评分 {artist_score}，已自动屏蔽")
            except Exception as e:
                logger.error(f"更新画师权重失败: {e}")

        # 记录反馈 - 只执行一次
        await db.record_feedback(illust.id, action)
        
        return {
            "action": action,
            "illust_id": illust.id,
            "disliked_tags": list(dict.fromkeys(disliked_tags)),
            "auto_blocked_artists": auto_blocked_artists,
        }
