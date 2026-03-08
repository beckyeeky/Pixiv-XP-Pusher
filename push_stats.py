"""
推送统计收集和报告模块
用于收集推送任务各阶段统计数据并生成报告
"""
import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class PushStats:
    """推送任务统计数据收集器"""
    
    # 1. 内容获取阶段
    fetch_counts: dict[str, int] = field(default_factory=dict)
    fetch_total: int = 0
    
    # 2. 过滤阶段
    filter_before_count: int = 0
    filter_after_count: int = 0
    filter_reasons: dict[str, int] = field(default_factory=dict)
    
    # 3. 推送阶段
    push_success_count: int = 0
    push_failed_count: int = 0
    push_by_source: dict[str, int] = field(default_factory=dict)
    
    # 4. AI 处理统计
    ai_semantic_match_enabled: bool = False
    ai_scorer_enabled: bool = False
    ai_error_count: int = 0
    
    def record_fetch(self, source: str, count: int):
        """记录各来源获取数量"""
        self.fetch_counts[source] = count
        self.fetch_total += count
    
    def record_filter_start(self, count: int):
        """记录过滤前数量"""
        self.filter_before_count = count
    
    def record_filter_end(self, count: int):
        """记录过滤后数量"""
        self.filter_after_count = count
    
    def record_filter_reason(self, reason: str, count: int = 1):
        """记录过滤原因"""
        self.filter_reasons[reason] = self.filter_reasons.get(reason, 0) + count
    
    def record_push_success(self, source: str = "unknown"):
        """记录推送成功"""
        self.push_success_count += 1
        self.push_by_source[source] = self.push_by_source.get(source, 0) + 1
    
    def record_push_failed(self):
        """记录推送失败"""
        self.push_failed_count += 1
    
    def record_ai_enabled(self, semantic_match: bool = False, scorer: bool = False):
        """记录 AI 功能启用状态"""
        self.ai_semantic_match_enabled = semantic_match
        self.ai_scorer_enabled = scorer
    
    def record_ai_error(self, count: int = 1):
        """记录 AI 错误"""
        self.ai_error_count += count
    
    @property
    def filter_pass_rate(self) -> float:
        """计算过滤通过率"""
        if self.filter_before_count == 0:
            return 0.0
        return (self.filter_after_count / self.filter_before_count) * 100
    
    def format_report(self) -> str:
        """格式化统计报告为 Telegram 消息"""
        lines = []
        lines.append("✅ 今日精选推送完成")
        lines.append("")
        lines.append("📊 统计详情：")
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        
        # 获取统计
        lines.append(f"📥 获取：{self.fetch_total} 个作品")
        source_names = {
            'xp_search': 'XP搜索',
            'subscription': '订阅',
            'ranking': '排行榜',
            'related': '关联推荐',
            'engagement_artists': '互动画师'
        }
        for source, count in sorted(self.fetch_counts.items(), key=lambda x: -x[1]):
            if count > 0:
                name = source_names.get(source, source)
                lines.append(f"   ├─ {name}: {count}")
        # 修正最后一个符号为 └─
        if len([c for c in self.fetch_counts.values() if c > 0]) > 0:
            lines[-1] = lines[-1].replace("├─", "└─")
        
        lines.append("")
        
        # 筛选统计 - 区分"过滤跳过"和"推送失败"
        filtered_out = self.filter_before_count - self.filter_after_count
        pass_rate = self.filter_pass_rate
        
        lines.append(f"🎯 筛选：{self.filter_after_count} 个通过 ({pass_rate:.0f}%)")
        
        # 过滤原因（被过滤掉的作品）
        reason_names = {
            'pushed': '已推送过',
            'blacklist': '黑名单标签',
            'muted': '静音标签',
            'ai': 'AI 生成',
            'r18': 'R18 过滤',
            'ugoira': '动图过滤',
            'time': '时间过滤',
            'match_score': '匹配度不足',
            'bookmark_threshold': '收藏数不足'
        }
        
        # 显示被过滤掉的原因
        if filtered_out > 0:
            lines.append(f"   ├─ 过滤跳过: {filtered_out}")
            reasons_to_show = {k: v for k, v in self.filter_reasons.items() if v > 0}
            if reasons_to_show:
                for i, (reason, count) in enumerate(sorted(reasons_to_show.items(), key=lambda x: -x[1])):
                    name = reason_names.get(reason, reason)
                    is_last = (i == len(reasons_to_show) - 1) and (self.push_failed_count == 0)
                    symbol = "└─" if is_last else "├─"
                    lines.append(f"   │  {symbol} {name}: {count}")
        
        # 推送阶段
        total_to_push = self.filter_after_count
        if total_to_push > 0:
            if self.push_failed_count > 0:
                lines.append(f"   └─ 推送失败: {self.push_failed_count}")
        
        lines.append("")
        
        # 推送结果统计
        lines.append(f"📤 推送结果：{self.push_success_count} 个成功")
        if self.push_by_source:
            for i, (source, count) in enumerate(sorted(self.push_by_source.items(), key=lambda x: -x[1])):
                name = source_names.get(source, source)
                is_last = (i == len(self.push_by_source) - 1)
                symbol = "└─" if is_last else "├─"
                lines.append(f"   {symbol} {name}: {count}")
        
        # 如果有失败，明确显示
        if self.push_failed_count > 0:
            lines.append(f"   ⚠️ 推送失败: {self.push_failed_count} 个")
        
        lines.append("")
        
        # AI 统计
        ai_features = []
        if self.ai_semantic_match_enabled:
            ai_features.append("语义匹配")
        if self.ai_scorer_enabled:
            ai_features.append("AI精排")
        
        if ai_features:
            ai_status = "、".join(ai_features) + "启用"
        else:
            ai_status = "未启用"
        
        error_info = f"，{self.ai_error_count} 错误" if self.ai_error_count > 0 else "，0 错误"
        lines.append(f"🧠 AI：{ai_status}{error_info}")
        
        lines.append("━━━━━━━━━━━━━━━━━━━━")
        
        return "\n".join(lines)


# 全局实例，用于跨函数传递
_current_stats: Optional[PushStats] = None


def get_current_stats() -> Optional[PushStats]:
    """获取当前任务的统计对象"""
    return _current_stats


def set_current_stats(stats: Optional[PushStats]):
    """设置当前任务的统计对象"""
    global _current_stats
    _current_stats = stats


def create_stats() -> PushStats:
    """创建新的统计对象并设置为当前"""
    stats = PushStats()
    set_current_stats(stats)
    return stats
