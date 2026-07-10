"""
Tag 分类器
将 Pixiv 标签归入推荐领域模型中的 Tag Category。
"""
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

import database as db
from profiler import DEFAULT_IP_TAGS
from tag_categories import (
    TAG_CATEGORY_ARTIST,
    TAG_CATEGORY_CHARACTER,
    TAG_CATEGORY_COPYRIGHT,
    TAG_CATEGORY_FEATURE,
    TAG_CATEGORY_NON_PREFERENCE,
    TAG_CATEGORY_UNRESOLVED,
    TagClassification,
    normalize_tag_category,
)
from tag_evidence import resolve_tag_evidence
from danbooru_evidence import DanbooruEvidenceLookup
from utils import normalize_tag

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    AsyncOpenAI = None
    HAS_OPENAI = False

logger = logging.getLogger(__name__)

DEFAULT_IP_TAGS_FILE = Path(__file__).parent / "data" / "ip_tags.json"


class TagClassifier:
    """Normalized Tag 的全局 Tag Category 分类器。"""

    def __init__(self, config: Optional[dict] = None, ip_tags: Optional[list[str] | str] = None):
        cfg = config or {}
        self.ttl_days = self._positive_int(cfg.get("ttl_days", 30), 30)
        self.batch_size = self._positive_int(cfg.get("batch_size", 50), 50)
        self.concurrency = self._positive_int(cfg.get("concurrency", 5), 5)
        self.model = cfg.get("model") or "deepseek-v4-flash"
        self.base_url = cfg.get("base_url") or "https://api.deepseek.com/v1"
        self.api_key = cfg.get("api_key", "")
        maintenance_cfg = cfg.get("maintenance") if isinstance(cfg.get("maintenance"), dict) else {}
        self.maintenance_max_tags = self._positive_int(maintenance_cfg.get("max_tags_per_run", 40), 40)
        self.maintenance_min_weight = float(maintenance_cfg.get("min_profile_weight", 0.0) or 0.0)
        self.prefer_unresolved_first = bool(maintenance_cfg.get("prefer_unresolved_first", True))
        self.judges = self._build_judges(cfg.get("judges"))
        self.manual_ip_tags = self._load_manual_ip_tags(ip_tags)
        self.danbooru_lookup = DanbooruEvidenceLookup(cfg.get("danbooru"))

        requested_enabled = cfg.get("enabled", False)
        self.enabled = bool(requested_enabled and HAS_OPENAI and self.api_key)
        if requested_enabled and not HAS_OPENAI:
            logger.warning("openai 依赖不可用，TagClassifier 已回退到手动 IP 列表")
        if requested_enabled and not self.api_key:
            logger.warning("tag_classifier.enabled=true 但未配置 api_key，已回退到手动 IP 列表")

        self.client = None
        self.judge_clients = {}
        if self.enabled:
            self.client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        if requested_enabled and HAS_OPENAI:
            for judge in self.judges:
                if judge["api_key"]:
                    self.judge_clients[judge["identity"]] = AsyncOpenAI(
                        api_key=judge["api_key"], base_url=judge["base_url"]
                    )

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
        # Multi-Judge installs accept machine categories only through maintenance
        # consensus; delivery must not turn one fast-path response into truth.
        if remaining and self.enabled and self.client and len(self.judges) == 1:
            ai_results = await self._classify_with_ai(remaining)
            if ai_results:
                await db.save_tag_classifications(
                    [(tag, item.classification, item.source) for tag, item in ai_results.items()]
                )
                results.update(ai_results)
                remaining = [tag for tag in remaining if tag not in ai_results]

        if remaining:
            if self.enabled and self.client:
                fallback_results = self._classify_unaccepted_ai_tags(remaining)
            else:
                fallback_results = self._classify_with_manual_list(remaining)
            await db.save_tag_classifications(
                [(tag, item.classification, item.source) for tag, item in fallback_results.items()]
            )
            results.update(fallback_results)

        return results

    async def maintain_profile_tags(self, tags: list[str] | dict[str, float], evidence_lookup=None) -> dict[str, TagClassification]:
        """Refresh observed profile-tag evidence; delivery never waits for this method."""
        normalized_tags = await self._select_maintenance_tags(tags)
        gathered = {tag: list(items) for tag, items in (await db.get_tag_evidence(normalized_tags)).items()}
        for tag in normalized_tags:
            gathered.setdefault(tag, [])
            if tag in self.manual_ip_tags:
                gathered[tag].append({"source": "manual", "classification": TAG_CATEGORY_COPYRIGHT, "confidence": 1.0})
        if evidence_lookup is None:
            supplied = await self._collect_machine_evidence(normalized_tags, gathered)
        elif evidence_lookup:
            supplied = await evidence_lookup(normalized_tags)
        else:
            supplied = {}
        for tag, items in supplied.items():
            gathered.setdefault(tag, []).extend(
                {"source": source, "classification": category, "confidence": confidence}
                for source, category, confidence in items
            )
        await db.save_tag_evidence([
            (tag, item["source"], item["classification"], item.get("confidence", 1.0))
            for tag, items in gathered.items() for item in items
        ])
        results = {tag: resolve_tag_evidence(tag, items) for tag, items in gathered.items()}
        await db.save_tag_classifications([(tag, item.classification, item.source) for tag, item in results.items()])
        return results

    async def _select_maintenance_tags(self, tags: list[str] | dict[str, float]) -> list[str]:
        if isinstance(tags, dict):
            profile = {normalize_tag(tag): float(weight) for tag, weight in tags.items() if normalize_tag(tag)}
            candidates = [tag for tag, weight in profile.items() if abs(weight) >= self.maintenance_min_weight]
            cached = await db.get_tag_classifications(candidates, ttl_days=self.ttl_days)
            def priority(tag):
                unresolved = cached.get(tag, {}).get("classification") == TAG_CATEGORY_UNRESOLVED
                return (0 if self.prefer_unresolved_first and unresolved else 1, -abs(profile[tag]), tag)
            return sorted(candidates, key=priority)[:self.maintenance_max_tags]
        return list(dict.fromkeys(normalize_tag(tag) for tag in tags if normalize_tag(tag)))[:self.maintenance_max_tags]

    async def _collect_machine_evidence(self, tags: list[str], cached: dict[str, list[dict]]) -> dict[str, list[tuple[str, str, float]]]:
        gathered: dict[str, list[tuple[str, str, float]]] = {}
        missing_danbooru = [tag for tag in tags if not any(item["source"] == "danbooru" for item in cached.get(tag, []))]
        if missing_danbooru:
            try:
                gathered.update(await self.danbooru_lookup.lookup(missing_danbooru))
            except Exception as exc:
                logger.warning("Danbooru evidence unavailable; using cached evidence and Judge votes: %s", exc)
        judge_evidence = await self._collect_judge_evidence(tags)
        for tag, items in judge_evidence.items():
            gathered.setdefault(tag, []).extend(items)
        return gathered

    async def _collect_judge_evidence(self, tags: list[str]) -> dict[str, list[tuple[str, str, float]]]:
        results: dict[str, list[tuple[str, str, float]]] = {}
        async def collect(judge):
            client = self.judge_clients.get(judge["identity"])
            if not client:
                return judge, {}
            try:
                return judge, await self._classify_with_client(tags, client, judge["model"])
            except Exception as exc:
                logger.warning("Judge %s failed: %s", judge["name"], exc)
                return judge, {}
        for judge, classifications in await asyncio.gather(*(collect(judge) for judge in self.judges)):
            for tag, classification in classifications.items():
                results.setdefault(tag, []).append((f"judge:{judge['identity']}", classification.classification, 1.0))
        return results

    def _build_judges(self, configured) -> list[dict]:
        raw = configured if isinstance(configured, list) else []
        if not raw:
            raw = [{"name": "legacy", "provider": "openai", "api_key": self.api_key, "base_url": self.base_url, "model": self.model}]
        unique, identities = [], set()
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            judge = {
                "name": str(item.get("name") or f"judge_{index + 1}"),
                "provider": str(item.get("provider") or "openai"),
                "api_key": item.get("api_key") or self.api_key,
                "base_url": item.get("base_url") or self.base_url,
                "model": item.get("model") or self.model,
            }
            identity = (judge["provider"], judge["base_url"].rstrip("/"), judge["model"])
            if identity not in identities:
                identities.add(identity)
                judge["identity"] = "|".join(identity)
                unique.append(judge)
        return unique

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
                classification=TAG_CATEGORY_COPYRIGHT if tag in self.manual_ip_tags else TAG_CATEGORY_FEATURE,
                source="manual" if tag in self.manual_ip_tags else "fallback",
            )
            for tag in tags
        }

    def _classify_unaccepted_ai_tags(self, tags: list[str]) -> dict[str, TagClassification]:
        return {
            tag: TagClassification(
                classification=TAG_CATEGORY_COPYRIGHT if tag in self.manual_ip_tags else TAG_CATEGORY_UNRESOLVED,
                source="manual" if tag in self.manual_ip_tags else "ai_unresolved",
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

    async def _classify_with_client(self, tags: list[str], client, model: str) -> dict[str, TagClassification]:
        batches = [tags[index:index + self.batch_size] for index in range(0, len(tags), self.batch_size)]

        async def classify_batch(batch):
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "你是 Pixiv 标签分类器，只输出 JSON。"},
                    {"role": "user", "content": self._build_prompt(batch)},
                ],
                temperature=0.0,
            )
            return self._parse_ai_classifications(
                json.loads(self._strip_code_fences(response.choices[0].message.content or "")), batch
            )

        grouped = await asyncio.gather(*(classify_batch(batch) for batch in batches), return_exceptions=True)
        merged = {}
        for result in grouped:
            if isinstance(result, Exception):
                raise result
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

        return self._parse_ai_classifications(data, tags)

    def _parse_ai_classifications(self, data: dict, tags: list[str]) -> dict[str, TagClassification]:
        input_tags = set(tags)
        assigned: dict[str, str] = {}

        def assign(raw_tag, raw_category):
            normalized = normalize_tag(str(raw_tag)) if raw_tag is not None else None
            if not normalized or normalized not in input_tags:
                return

            category = normalize_tag_category(str(raw_category))
            existing = assigned.get(normalized)
            if existing is not None and existing != category:
                assigned[normalized] = TAG_CATEGORY_UNRESOLVED
                return
            assigned[normalized] = category

        for key, category in (
            ("feature_tags", TAG_CATEGORY_FEATURE),
            ("character_tags", TAG_CATEGORY_CHARACTER),
            ("copyright_tags", TAG_CATEGORY_COPYRIGHT),
            ("ip_tags", TAG_CATEGORY_COPYRIGHT),
            ("artist_tags", TAG_CATEGORY_ARTIST),
            ("non_preference_tags", TAG_CATEGORY_NON_PREFERENCE),
            ("nonpreference_tags", TAG_CATEGORY_NON_PREFERENCE),
            ("unresolved_tags", TAG_CATEGORY_UNRESOLVED),
        ):
            for raw_tag in data.get(key, []) or []:
                assign(raw_tag, category)

        for key in ("classifications", "tag_categories", "tags"):
            payload = data.get(key)
            if isinstance(payload, dict):
                for raw_tag, raw_category in payload.items():
                    assign(raw_tag, raw_category)
            elif isinstance(payload, list):
                for item in payload:
                    if not isinstance(item, dict):
                        continue
                    raw_tag = item.get("tag") or item.get("normalized_tag") or item.get("name")
                    raw_category = item.get("category") or item.get("classification")
                    assign(raw_tag, raw_category)

        results: dict[str, TagClassification] = {}
        for tag, category in assigned.items():
            results[tag] = TagClassification(category, "ai")
        return results

    def _build_prompt(self, tags: list[str]) -> str:
        return f"""请将下面这些 Pixiv 标签分类为一个 Tag Category：
- feature_tags: 视觉特征 / 萌属性 / 穿着 / 动作 / 构图 / 题材，可跨角色和作品迁移
- character_tags: 具体虚构角色
- copyright_tags: 作品 IP / 版权 / 系列 / 游戏 / 动漫 / 漫画 / 世界观
- artist_tags: 创作者、画师、社团或作者身份
- non_preference_tags: 平台标签、活动标签、热度标签、元数据等不表达推荐偏好的标签
- unresolved_tags: 证据不足、含义冲突或你无法可靠分类的标签

分类原则：
1. 像 `blue_archive`、`genshin_impact` 属于 copyright_tags。
2. 像 `hoshino_(blue_archive)`、`hatsune_miku` 属于 character_tags。
3. 像 `pantyhose`、`white_hair`、`cat_ears` 属于 feature_tags。
4. 像画师名、作者名、社团名，归入 artist_tags。
5. 像 `high_resolution`、`commission`、`sample`、`pixivision` 这类元数据，归入 non_preference_tags。
6. 只返回输入中出现过的标签，不要扩展，不要解释；不确定就归入 unresolved_tags。

输入标签：
{json.dumps(tags, ensure_ascii=False)}

输出 JSON 结构：
{{
  "feature_tags": ["tag1"],
  "character_tags": ["tag2"],
  "copyright_tags": ["tag3"],
  "artist_tags": ["tag4"],
  "non_preference_tags": ["tag5"],
  "unresolved_tags": ["tag6"]
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
