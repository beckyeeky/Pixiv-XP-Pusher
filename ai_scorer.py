"""
AI 行为预测评分器 (LLM 精排)

借鉴 X 算法 Phoenix Scorer 的理念，使用 LLM 预测用户对作品的喜好程度。
仅在候选数量较少时启用（默认 < 50），作为粗排后的精排步骤。

Prompt 设计参考:
- 提供用户的 Top XP Tags
- 提供近期反馈历史（喜欢/不喜欢的标签）
- 让 LLM 输出喜爱概率分数
"""
import logging
import json
import asyncio
from typing import Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 尝试导入 OpenAI 客户端
try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False


@dataclass
class AIScoreConfig:
    """AI 评分配置"""
    enabled: bool = False
    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    max_candidates: int = 50  # 超过此数量时跳过 AI 评分
    score_weight: float = 0.3  # AI 分数在最终排序中的权重


class AIScorer:
    """
    AI 行为预测评分器
    
    使用 LLM 对候选作品进行二次精排，预测用户喜好。
    """
    
    PROMPT_TEMPLATE = """你是推荐系统评分器。根据用户偏好，给每个作品打"喜爱概率"分（0.0-1.0）。

用户偏好:
- 最爱 Tag: {top_tags}
- 最近喜欢的作品标签: {recent_likes}
- 最近不喜欢的作品标签: {recent_dislikes}

评分原则:
1. 优先判断视觉特征是否贴合用户喜好，例如发色、服饰、丝袜、耳朵、体型、姿态、构图、氛围、题材。
2. 作品所属 IP / 系列只能作为弱信号，不能因为命中用户常见 IP 就给高分。
3. 如果候选同时命中 IP 与视觉特征，视觉特征的匹配更重要。
4. 如果只有 IP 相符但视觉特征普通，分数应保持中性或偏低。
5. 分数代表“这张图本身看起来是否像用户会喜欢”，不是“是否来自用户熟悉的作品”。

候选作品:
{candidates}

返回 JSON 对象，格式: {{"scores": [{{"id": 123, "score": 0.85}}]}}
只返回 JSON，不要解释。"""

    def __init__(self, config: dict):
        """
        初始化 AI 评分器
        
        Args:
            config: ai.scorer 配置块
        """
        self.enabled = config.get("enabled", False)
        self.provider = config.get("provider", "openai")
        self.model = config.get("model", "gpt-4o-mini")
        self.max_candidates = config.get("max_candidates", 50)
        self.score_weight = config.get("score_weight", 0.3)
        
        self._client = None
        
        if not self.enabled:
            return
        
        if not HAS_OPENAI:
            logger.error("openai 库未安装，AI 评分功能将不可用")
            self.enabled = False
            return
        
        api_key = config.get("api_key", "")
        base_url = config.get("base_url", "")
        
        if not api_key:
            logger.warning("未配置 AI Scorer API Key，功能已禁用")
            self.enabled = False
            return
        
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url or None
        )
        logger.info(f"AI Scorer 已初始化 (model={self.model})")
    
    async def score_candidates(
        self,
        candidates: list,
        xp_profile: dict[str, float],
        recent_likes: list[str] = None,
        recent_dislikes: list[str] = None
    ) -> dict[int, float]:
        """
        对候选作品进行 AI 评分
        
        Args:
            candidates: 候选作品列表 (Illust)
            xp_profile: 用户 XP 画像 {tag: weight}
            recent_likes: 近期喜欢的作品标签
            recent_dislikes: 近期不喜欢的作品标签
            
        Returns:
            {illust_id: ai_score} 映射
        """
        if not self.enabled or not candidates:
            return {}
        
        # 超过阈值时跳过
        if len(candidates) > self.max_candidates:
            logger.debug(f"候选数量 {len(candidates)} 超过阈值 {self.max_candidates}，跳过 AI 评分")
            return {}
        
        try:
            # 构建 Prompt
            top_tags = [t for t, _ in sorted(xp_profile.items(), key=lambda x: x[1], reverse=True)[:10]]
            
            # 构建候选列表描述
            candidate_lines = []
            for illust in candidates:
                tags_str = ", ".join(illust.tags[:8])  # 只取前 8 个标签
                candidate_lines.append(f"- ID: {illust.id}, Tags: [{tags_str}]")
            
            prompt = self.PROMPT_TEMPLATE.format(
                top_tags=", ".join(top_tags),
                recent_likes=", ".join(recent_likes[:5]) if recent_likes else "无",
                recent_dislikes=", ".join(recent_dislikes[:5]) if recent_dislikes else "无",
                candidates="\n".join(candidate_lines)
            )
            
            # 调用 LLM
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )

            choice = response.choices[0]
            content = choice.message.content or ""
            finish_reason = getattr(choice, "finish_reason", None)
            scores = self._parse_scores(content)
            
            # 转换为字典
            result = {}
            for item in scores:
                illust_id = item.get("id")
                score = item.get("score", 0.5)
                if illust_id:
                    result[illust_id] = max(0.0, min(1.0, float(score)))
            
            logger.info(f"AI 评分完成: {len(result)}/{len(candidates)} 个作品")
            return result
            
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError, IndexError) as e:
            logger.error(
                "AI 评分响应无效: %s (content_length=%d, content_excerpt=%r, finish_reason=%s)",
                e,
                len(content.strip()) if "content" in locals() else 0,
                self._response_excerpt(content) if "content" in locals() else None,
                finish_reason if "finish_reason" in locals() else None,
            )
            return {}
        except Exception as e:
            logger.error("AI 评分请求失败: %s", e)
            return {}

    @staticmethod
    def _response_excerpt(content: object, limit: int = 160) -> str | None:
        text = " ".join(str(content or "").split())
        if not text:
            return None
        return text[:limit] + ("…" if len(text) > limit else "")

    @staticmethod
    def _parse_scores(content: object) -> list[dict]:
        text = str(content or "").strip()
        if text.startswith("```"):
            lines = text.splitlines()
            lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        payload = json.loads(text)
        if isinstance(payload, dict):
            scores = payload.get("scores")
        elif isinstance(payload, list):
            scores = payload  # compatibility with responses produced before JSON-object mode
        else:
            raise ValueError("AI scorer response must be a JSON object or array")

        if not isinstance(scores, list):
            raise ValueError("AI scorer response field 'scores' must be an array")
        if not all(isinstance(item, dict) for item in scores):
            raise ValueError("AI scorer scores must be objects")
        return scores
    
    def blend_scores(
        self,
        base_scores: dict[int, float],
        ai_scores: dict[int, float]
    ) -> dict[int, float]:
        """
        混合基础分数和 AI 分数
        
        公式: final = (1 - weight) * base + weight * ai
        
        Args:
            base_scores: 基础分数 {id: score}
            ai_scores: AI 分数 {id: score}
            
        Returns:
            混合后的分数
        """
        result = {}
        for illust_id, base_score in base_scores.items():
            ai_score = ai_scores.get(illust_id)
            if ai_score is not None:
                final = (1 - self.score_weight) * base_score + self.score_weight * ai_score
            else:
                final = base_score
            result[illust_id] = final
        return result
