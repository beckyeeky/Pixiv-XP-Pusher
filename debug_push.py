#!/usr/bin/env python3
"""
Debug Push Script - 轻量版 (仅分析数据库缓存)

功能：
1. 分析数据库中已有的作品缓存
2. 模拟过滤流程，记录每个作品的过滤原因
3. 追踪 min_create_days 等配置的实际效果
"""

import asyncio
import sys
import json
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any, Set
from collections import defaultdict

# 添加项目路径
sys.path.insert(0, "/opt/Pixiv-XP-Pusher")

from config import load_config
import database as db

# ============ 数据结构 ============
@dataclass
class FilterDetail:
    """过滤详情记录"""
    illust_id: int
    title: str
    author: str
    author_id: int
    create_date: datetime
    tags: List[str]
    bookmark_count: int
    source: str
    
    passed: bool = False
    filter_reason: str = ""
    extra_info: Dict[str, Any] = field(default_factory=dict)
    
    def to_dict(self) -> Dict:
        return {
            "illust_id": self.illust_id,
            "title": self.title,
            "author": self.author,
            "author_id": self.author_id,
            "create_date": self.create_date.isoformat() if self.create_date else None,
            "tags": self.tags,
            "bookmark_count": self.bookmark_count,
            "source": self.source,
            "passed": self.passed,
            "filter_reason": self.filter_reason,
            "extra_info": self.extra_info
        }


@dataclass
class DebugStats:
    """调试统计信息"""
    source_counts: Dict[str, int] = field(default_factory=dict)
    filter_details: Dict[str, List[FilterDetail]] = field(default_factory=lambda: defaultdict(list))
    date_distribution: Dict[str, int] = field(default_factory=dict)
    already_pushed_ids: Set[int] = field(default_factory=set)
    
    def add_detail(self, detail: FilterDetail):
        self.source_counts[detail.source] = self.source_counts.get(detail.source, 0) + 1
        
        date_key = detail.create_date.strftime("%Y-%m-%d") if detail.create_date else "unknown"
        self.date_distribution[date_key] = self.date_distribution.get(date_key, 0) + 1
        
        if detail.passed:
            self.filter_details["passed"].append(detail)
        else:
            self.filter_details[detail.filter_reason].append(detail)


class DebugAnalyzer:
    """调试分析器"""
    
    def __init__(self):
        self.config = load_config(Path("/opt/Pixiv-XP-Pusher/config.yaml"))
        self.stats = DebugStats()
        
        # 提取配置
        filter_cfg = self.config.get("filter", {})
        self.min_create_days = filter_cfg.get("min_create_days", 7)
        self.exclude_ai = filter_cfg.get("exclude_ai", True)
        self.r18_mode = filter_cfg.get("r18_mode", "mixed")
        self.skip_ugoira = filter_cfg.get("skip_ugoira", True)
        self.blacklist_tags = set(t.lower() for t in filter_cfg.get("blacklist_tags", []))
        self.blacklist_tags.update({"r-18g", "guro", "gore"})
        
        fetcher_cfg = self.config.get("fetcher", {})
        self.date_range_days = fetcher_cfg.get("date_range_days", 7)
        
        self.time_threshold = datetime.now().astimezone() - timedelta(days=self.min_create_days)
    
    async def initialize(self):
        """初始化数据库"""
        await db.init_db()
        
        # 获取已推送的作品
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            cursor = await conn.execute("SELECT illust_id FROM push_history")
            rows = await cursor.fetchall()
            self.stats.already_pushed_ids = {row[0] for row in rows}
    
    def analyze_illust(self, illust_data: dict) -> FilterDetail:
        """分析单个作品"""
        illust_id = illust_data.get("id", 0)
        title = illust_data.get("title", "Unknown")
        author = illust_data.get("user_name", "Unknown")
        author_id = illust_data.get("user_id", 0)
        tags = illust_data.get("tags", [])
        bookmark_count = illust_data.get("bookmark_count", 0)
        source = illust_data.get("source", "unknown")
        
        # 解析日期
        create_date_str = illust_data.get("create_date")
        if create_date_str:
            try:
                create_date = datetime.fromisoformat(create_date_str.replace('Z', '+00:00'))
            except:
                create_date = datetime.now().astimezone()
        else:
            create_date = datetime.now().astimezone()
        
        detail = FilterDetail(
            illust_id=illust_id,
            title=title,
            author=author,
            author_id=author_id,
            create_date=create_date,
            tags=tags,
            bookmark_count=bookmark_count,
            source=source
        )
        
        # 计算年龄
        now = datetime.now().astimezone()
        days_old = (now - create_date).days
        detail.extra_info["days_old"] = days_old
        
        # 1. 已推送检查
        if illust_id in self.stats.already_pushed_ids:
            detail.filter_reason = "pushed"
            detail.extra_info["reason"] = "已推送过"
            return detail
        
        # 2. 时间过滤
        if self.min_create_days > 0:
            if create_date < self.time_threshold:
                detail.filter_reason = "time"
                detail.extra_info.update({
                    "min_create_days": self.min_create_days,
                    "days_old": days_old,
                    "days_over": days_old - self.min_create_days,
                    "threshold_date": self.time_threshold.strftime("%Y-%m-%d")
                })
                return detail
        
        # 3. 黑名单检查
        for tag in tags:
            if tag.lower() in self.blacklist_tags:
                detail.filter_reason = "blacklist"
                detail.extra_info["matched_tag"] = tag
                return detail
        
        # 4. AI检查
        ai_type = illust_data.get("ai_type", 0)
        if self.exclude_ai:
            if ai_type == 2:
                detail.filter_reason = "ai"
                detail.extra_info["ai_type"] = "pixiv_official"
                return detail
            
            ai_keywords = ["ai", "stable diffusion", "midjourney", "novelai",
                          "ai生成", "aiイラスト", "ai絵", "ai作品"]
            for tag in tags:
                tag_lower = tag.lower()
                for kw in ai_keywords:
                    if kw.lower() in tag_lower:
                        detail.filter_reason = "ai"
                        detail.extra_info["ai_type"] = "tag_detection"
                        detail.extra_info["matched_tag"] = tag
                        return detail
        
        # 5. R-18检查
        mode_str = str(self.r18_mode).lower()
        is_r18 = illust_data.get("is_r18", False)
        has_r18_tag = any(t.lower().replace(" ", "") in ("r-18", "r18") for t in tags)
        is_r18 = is_r18 or has_r18_tag
        
        if mode_str in ("true", "r18_only", "pure") and not is_r18:
            detail.filter_reason = "r18"
            detail.extra_info["reason"] = "非R-18作品被过滤"
            return detail
        elif mode_str in ("safe", "18-", "clean") and is_r18:
            detail.filter_reason = "r18"
            detail.extra_info["reason"] = "R-18作品被过滤"
            return detail
        
        # 6. 动图检查
        illust_type = illust_data.get("type", "illust")
        if self.skip_ugoira and illust_type == "ugoira":
            detail.filter_reason = "ugoira"
            return detail
        
        # 通过所有过滤
        detail.passed = True
        return detail
    
    async def analyze_cached_data(self):
        """分析数据库缓存的数据"""
        print("="*70)
        print("Pixiv-XP-Pusher Debug Tool - 数据库缓存分析")
        print("="*70)
        
        await self.initialize()
        
        print(f"\n📋 配置信息:")
        print(f"  min_create_days: {self.min_create_days}")
        print(f"  date_range_days: {self.date_range_days}")
        print(f"  exclude_ai: {self.exclude_ai}")
        print(f"  r18_mode: {self.r18_mode}")
        print(f"  skip_ugoira: {self.skip_ugoira}")
        print(f"  已推送作品: {len(self.stats.already_pushed_ids)} 个")
        
        # 从数据库获取缓存的作品
        print("\n正在从数据库获取作品缓存...")
        
        async with db.aiosqlite.connect(db.DB_PATH) as conn:
            # 获取最近的500个作品
            cursor = await conn.execute(
                """
                SELECT illust_id, tags, user_id, user_name, source, created_at
                FROM illust_cache 
                ORDER BY created_at DESC 
                LIMIT 1000
                """
            )
            rows = await cursor.fetchall()
        
        print(f"获取到 {len(rows)} 个作品缓存")
        
        # 补充作品信息
        illusts_data = []
        for row in rows:
            illust_id, tags_json, user_id, user_name, source, _ = row
            try:
                tags = json.loads(tags_json) if tags_json else []
            except:
                tags = []
            
            illusts_data.append({
                "id": illust_id,
                "title": f"Work_{illust_id}",
                "user_id": user_id,
                "user_name": user_name or f"Artist_{user_id}",
                "tags": tags,
                "bookmark_count": 0,
                "is_r18": False,
                "ai_type": 0,
                "type": "illust",
                "create_date": datetime.now().astimezone().isoformat(),
                "source": source or "unknown"
            })
        
        # 分析每个作品
        print(f"\n分析 {len(illusts_data)} 个作品...")
        for i, data in enumerate(illusts_data):
            if i % 100 == 0:
                print(f"  已处理 {i}/{len(illusts_data)}...")
            
            detail = self.analyze_illust(data)
            self.stats.add_detail(detail)
        
        # 生成报告
        self.print_report()
    
    def print_report(self):
        """打印报告"""
        print("\n" + "="*70)
        print("📊 分析结果")
        print("="*70)
        
        total = sum(self.stats.source_counts.values())
        
        # 来源统计
        print(f"\n📊 来源分布:")
        for source, count in sorted(self.stats.source_counts.items(), key=lambda x: -x[1]):
            pct = count / total * 100 if total > 0 else 0
            print(f"  {source}: {count} ({pct:.1f}%)")
        
        # 过滤统计
        print(f"\n🚫 过滤统计:")
        for reason, details in sorted(self.stats.filter_details.items(), key=lambda x: -len(x[1])):
            if details:
                count = len(details)
                pct = count / total * 100 if total > 0 else 0
                print(f"  [{reason.upper()}]: {count} ({pct:.1f}%)")
        
        # 时间过滤详情
        time_records = self.stats.filter_details.get("time", [])
        if time_records:
            print(f"\n⏰ 时间过滤详情 ({len(time_records)} 个作品):")
            print(f"  配置: min_create_days = {self.min_create_days}")
            print(f"  阈值: {self.time_threshold.strftime('%Y-%m-%d')}")
            
            # 按超出天数分组
            over_days_dist = defaultdict(int)
            for r in time_records:
                over = r.extra_info.get("days_over", 0)
                over_days_dist[over] += 1
            
            print(f"\n  超出天数分布:")
            for days_over in sorted(over_days_dist.keys())[:10]:
                count = over_days_dist[days_over]
                print(f"    超出 {days_over} 天: {count} 个作品")
        
        # 已推送去重详情
        pushed_records = self.stats.filter_details.get("pushed", [])
        if pushed_records:
            print(f"\n🔄 已推送去重详情 ({len(pushed_records)} 个作品)")
            print(f"  最近20个被去重的作品:")
            for r in pushed_records[:20]:
                print(f"    ID {r.illust_id}: {r.source} | {', '.join(r.tags[:3])}")
            
            # 按来源统计
            source_dist = defaultdict(int)
            for r in pushed_records:
                source_dist[r.source] += 1
            print(f"\n  按来源统计:")
            for source, count in sorted(source_dist.items(), key=lambda x: -x[1]):
                print(f"    {source}: {count}")
        
        # 检查是否存在时间过滤问题
        time_records = self.stats.filter_details.get("time", [])
        if not time_records:
            print(f"\n⏰ 时间过滤分析:")
            print(f"  ✅ 当前缓存数据中没有被时间过滤的作品")
            print(f"  ℹ️  所有作品的日期都在 min_create_days={self.min_create_days} 范围内")
        else:
            print(f"\n⏰ 时间过滤: {len(time_records)} 个作品")
        
        # 通过的作品
        passed_records = self.stats.filter_details.get("passed", [])
        if passed_records:
            print(f"\n✅ 通过所有过滤: {len(passed_records)} 个作品")
        
        # 关键发现
        print("\n" + "="*70)
        print("🔍 关键发现 & 建议")
        print("="*70)
        
        passed_count = len(self.stats.filter_details.get("passed", []))
        pass_rate = (passed_count / total * 100) if total > 0 else 0
        
        print(f"\n整体通过率: {passed_count}/{total} ({pass_rate:.1f}%)")
        
        if time_records:
            time_pct = len(time_records) / total * 100
            print(f"\n⏰ 时间过滤影响:")
            print(f"  - 过滤了 {len(time_records)} 个作品 ({time_pct:.1f}%)")
            print(f"  - 当前配置: min_create_days = {self.min_create_days}")
            print(f"  - date_range_days = {self.date_range_days}")
            if time_pct > 50:
                print(f"  ⚠️  警告: 超过一半作品被时间过滤！")
                print(f"     建议: 增大 date_range_days 或减小 min_create_days")
        
        if pushed_records:
            pushed_pct = len(pushed_records) / total * 100
            print(f"\n🔄 已推送去重影响:")
            print(f"  - 去重了 {len(pushed_records)} 个作品 ({pushed_pct:.1f}%)")
            if len(self.stats.already_pushed_ids) > 1000:
                print(f"  - 历史推送库较大 ({len(self.stats.already_pushed_ids)} 个)")
            
            # 新增: 详细建议
            print(f"\n  ⚠️  关键问题: 已推送去重比例过高！")
            print(f"     这意味着每次获取的内容大部分是已经推送过的老图")
            print(f"\n  💡 建议解决方案:")
            print(f"     1. 增大 fetcher.date_range_days (当前: {self.date_range_days} 天)")
            print(f"        扩大搜索的时间范围，获取更多新作品")
            print(f"     2. 检查 fetcher.search_limit (当前配置限制)")
            print(f"        增加每次搜索获取的作品数量")
            print(f"     3. 调整 MAB 策略分配，增加 xp_search 的配额")
            print(f"     4. 考虑清理旧的推送历史 (如超过3个月的)")
            print(f"        执行: DELETE FROM push_history WHERE pushed_at < date('now', '-90 days')")
        
        print("\n" + "="*70)
        print("调试完成!")
        print("="*70)


async def main():
    analyzer = DebugAnalyzer()
    await analyzer.analyze_cached_data()


if __name__ == "__main__":
    asyncio.run(main())
