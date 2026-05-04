"""
Tag 分类器
将 Pixiv 标签区分为视觉特征(feature)与 IP/copyright(ip)
"""
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import database as db
from profiler import DEFAULT_IP_TAGS
from utils import normalize_tag

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

logger = logging.getLogger(__name__)

DEFAULT_IP_TAGS_FILE = Path(__file__).parent / "data" / "ip_tags.json"


@dataclass(frozen=True)
class TagClassification:
    classification: str
    source: str


class TagClassifier:
    """IP / feature 标签分类器。"""

    def __init__(self, config: Optional[dict] = None, ip_tags: Optional[list[str] | str] = None):
        cfg = config or {}
        self.ttl_days = self._positive_int(cfg.get("ttl_days", 30), 30)
        self.batch_size = self._positive_int(cfg.get("batch_size", 50), 50)
        self.concurrency = self._positive_int(cfg.get("concurrency", 5), 5)
        self.model = cfg.get("model") or "deepseek-v4-flash"
        self.base_url = cfg.get("base_url") or "https://api.deepseek.com/v1"
        self.api_key = cfg.get("api_key", "")
        self.manual_ip_tags = self._load_manual_ip_tags(ip_tags)

        requested_enabled = cfg.get("enabled", False)
        self.enabled = bool(requested_enabled and HAS_OPENAI and self.api_key)
        if requested_enabled and not HAS_OPENAI:
            logger.warning("openai 依赖不可用，TagClassifier 已回退到手动 IP 列表")
        if requested_enabled and not self.api_key:
            logger.warning("tag_classifier.enabled=true 但未配置 api_key，已回退到手动 IP 列表")

        self.client = None
        if self.enabled:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)

    async def classify_tags(self, tags: list[str]) -> dict[str, TagClassification]:
        """批量分类标签，优先使用未过期缓存。"""
        normalized_tags = []
        for tag in tags:
            normalized = normalize_tag(tag)
            if normalized:
                normalized_tags.append(normalized)

        if not normalized_tags:
            return {}

        unique_tags = list(dict.fromkeys(normalized_tags))
        cached_rows = await db.get_tag_classifications(unique_tags, ttl_days=self.ttl_days)
        results = {}
        for tag, row in cached_rows.items():
            # 启用 AI 后，不复用低置信度 fallback 结果，避免历史回退缓存长期遮蔽 AI 分类
            if self.enabled and row["source"] == "fallback":
                continue
            # 手动 IP 列表可能更新；不要让旧 fallback(feature) 缓存遮蔽新配置。
            if row["source"] == "fallback" and tag in self.manual_ip_tags:
                continue
            results[tag] = TagClassification(
                classification=row["classification"],
                source=row["source"],
            )

        remaining = [tag for tag in unique_tags if tag not in results]
        if remaining and self.enabled and self.client:
            ai_results = await self._classify_with_ai(remaining)
            if ai_results:
                await db.save_tag_classifications(
                    [(tag, item.classification, item.source) for tag, item in ai_results.items()]
                )
                results.update(ai_results)
                remaining = [tag for tag in remaining if tag not in ai_results]

        if remaining:
            fallback_results = self._classify_with_manual_list(remaining)
            await db.save_tag_classifications(
                [(tag, item.classification, item.source) for tag, item in fallback_results.items()]
            )
            results.update(fallback_results)

        return results

    def _load_manual_ip_tags(self, ip_tags: Optional[list[str] | str]) -> set[str]:
        raw_tags: list[str] = []

        if isinstance(ip_tags, str):
            ip_tags_path = Path(ip_tags)
            if ip_tags_path.exists():
                try:
                    raw_tags = json.loads(ip_tags_path.read_text(encoding="utf-8"))
                except Exception as e:
                    logger.warning(f"加载 IP 标签文件失败 {ip_tags_path}: {e}")
        elif isinstance(ip_tags, list):
            raw_tags = ip_tags
        elif DEFAULT_IP_TAGS_FILE.exists():
            try:
                raw_tags = json.loads(DEFAULT_IP_TAGS_FILE.read_text(encoding="utf-8"))
            except Exception as e:
                logger.warning(f"加载默认 IP 标签文件失败 {DEFAULT_IP_TAGS_FILE}: {e}")

        if not raw_tags:
            raw_tags = list(DEFAULT_IP_TAGS)

        return {normalize_tag(tag.replace(":", "").replace("__", "_")) for tag in raw_tags if tag}

    def _classify_with_manual_list(self, tags: list[str]) -> dict[str, TagClassification]:
        return {
            tag: TagClassification(
                classification="ip" if tag in self.manual_ip_tags else "feature",
                source="manual" if tag in self.manual_ip_tags else "fallback",
            )
            for tag in tags
        }

    async def _classify_with_ai(self, tags: list[str]) -> dict[str, TagClassification]:
        batches = [
            tags[i:i + self.batch_size]
            for i in range(0, len(tags), self.batch_size)
        ]
        semaphore = asyncio.Semaphore(self.concurrency)
        merged: dict[str, TagClassification] = {}

        async def _run_batch(batch: list[str]) -> dict[str, TagClassification]:
            async with semaphore:
                return await self._classify_batch(batch)

        results = await asyncio.gather(
            *[_run_batch(batch) for batch in batches],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning(f"AI 标签分类批次失败: {result}")
                continue
            merged.update(result)

        return merged

    async def _classify_batch(self, tags: list[str]) -> dict[str, TagClassification]:
        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "你是 Pixiv 标签分类器，只输出 JSON。"},
                {"role": "user", "content": self._build_prompt(tags)},
            ],
            temperature=0.0,
        )
        content = response.choices[0].message.content or ""
        data = json.loads(self._strip_code_fences(content))

        ip_tags = {normalize_tag(tag) for tag in data.get("ip_tags", [])}
        feature_tags = {normalize_tag(tag) for tag in data.get("feature_tags", [])}

        results: dict[str, TagClassification] = {}
        for tag in tags:
            if tag in ip_tags:
                results[tag] = TagClassification("ip", "ai")
            elif tag in feature_tags:
                results[tag] = TagClassification("feature", "ai")
        return results

    def _build_prompt(self, tags: list[str]) -> str:
        return f"""请将下面这些 Pixiv 标签分类为两类：
- ip_tags: 作品所属 IP / 版权 / 系列 / 游戏 / 动漫 / 角色阵营
- feature_tags: 视觉特征 / 萌属性 / 穿着 / 动作 / 构图 / 题材

分类原则：
1. 像 `blue_archive`、`genshin_impact` 属于 ip_tags。
2. 像 `pantyhose`、`white_hair`、`cat_ears` 属于 feature_tags。
3. 如果标签明显是作品系列或世界观，归入 ip_tags。
4. 如果标签描述外观、服饰、姿态、场景或 fetish，归入 feature_tags。
5. 只返回输入中出现过的标签，不要扩展，不要解释。

输入标签：
{json.dumps(tags, ensure_ascii=False)}

输出 JSON 结构：
{{
  "ip_tags": ["tag1"],
  "feature_tags": ["tag2"]
}}"""

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("```json"):
            stripped = stripped[7:]
        elif stripped.startswith("```"):
            stripped = stripped[3:]
        if stripped.endswith("```"):
            stripped = stripped[:-3]
        return stripped.strip()

    @staticmethod
    def _positive_int(value, default: int) -> int:
        try:
            if isinstance(value, bool):
                raise ValueError
            return max(1, int(value))
        except (TypeError, ValueError):
            return default
