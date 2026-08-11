"""Bounded cached-vector retrieval for the Exploration lane."""

from __future__ import annotations

import asyncio
import logging
import math
import uuid
from dataclasses import dataclass
from typing import Callable, Iterable

import database as db
from embedder import profile_embedding_hash
from pixiv_client import Illust
from utils import normalize_tag


logger = logging.getLogger(__name__)
VECTOR_EXPLORATION_SOURCE = "semantic_vector_exploration"


@dataclass(frozen=True)
class VectorExplorationConfig:
    enabled: bool = False
    pool_limit: int = 1000
    candidate_limit: int = 40
    min_similarity: float = 0.60
    detail_concurrency: int = 5
    duplicate_similarity: float = 0.90

    @classmethod
    def from_mapping(cls, config: dict | None) -> "VectorExplorationConfig":
        cfg = config or {}
        return cls(
            enabled=bool(cfg.get("enabled", False)),
            pool_limit=max(1, min(int(cfg.get("pool_limit", 1000)), 10000)),
            candidate_limit=max(1, min(int(cfg.get("candidate_limit", 40)), 500)),
            min_similarity=max(-1.0, min(float(cfg.get("min_similarity", 0.60)), 1.0)),
            detail_concurrency=max(1, min(int(cfg.get("detail_concurrency", 5)), 20)),
            duplicate_similarity=max(-1.0, min(float(cfg.get("duplicate_similarity", 0.90)), 1.0)),
        )


@dataclass(frozen=True)
class VectorExplorationBatch:
    run_id: str | None
    candidates: list[Illust]
    duplicate_similarity: float = 0.90


def cosine_similarity(left: list[float], right: list[float]) -> float | None:
    if not left or len(left) != len(right):
        return None
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0 or right_norm == 0:
        return None
    return max(-1.0, min(dot / (left_norm * right_norm), 1.0))


def slate_profile_concentration(
    illusts: Iterable[Illust],
    profile: dict[str, float],
    resolve_tag: Callable[[str], str] = normalize_tag,
) -> float:
    """HHI of Preference Profile support represented in the selected slate."""
    support: dict[str, float] = {}
    for illust in illusts:
        for raw_tag in set(illust.tags or []):
            tag = resolve_tag(raw_tag)
            weight = max(0.0, float(profile.get(tag, profile.get(normalize_tag(raw_tag), 0.0))))
            if weight:
                support[tag] = support.get(tag, 0.0) + weight
    total = sum(support.values())
    if total == 0:
        return 0.0
    return sum((value / total) ** 2 for value in support.values())


def preference_profile_concentration(profile: dict[str, float]) -> float:
    """HHI of positive affinity weights in the Preference Profile itself."""
    weights = [max(0.0, float(value)) for value in profile.values()]
    total = sum(weights)
    if total == 0:
        return 0.0
    return sum((weight / total) ** 2 for weight in weights if weight)


def duplicate_semantic_rate(
    embeddings: list[list[float]],
    threshold: float = 0.90,
) -> float:
    """Share of comparable selected pairs at or above the duplicate threshold."""
    duplicates = comparable = 0
    for index, left in enumerate(embeddings):
        for right in embeddings[index + 1:]:
            similarity = cosine_similarity(left, right)
            if similarity is None:
                continue
            comparable += 1
            duplicates += similarity >= threshold
    return duplicates / comparable if comparable else 0.0


class SemanticVectorExplorer:
    """Retrieve a bounded set of cached semantic neighbours behind one interface."""

    def __init__(self, config: dict | None, *, model: str, detail_loader):
        self.config = VectorExplorationConfig.from_mapping(config)
        self.model = model
        self.detail_loader = detail_loader

    async def retrieve(
        self,
        *,
        user_id: int,
        profile: dict[str, float],
        exclude_ids: set[int] | None = None,
    ) -> VectorExplorationBatch:
        if not self.config.enabled or not profile:
            return VectorExplorationBatch(None, [])

        current_profile_hash = profile_embedding_hash(profile)
        user_embedding = await db.get_current_user_embedding(
            user_id, self.model, current_profile_hash,
        )
        if not user_embedding:
            logger.info("跳过 semantic vector Exploration：没有当前模型兼容的画像缓存")
            return VectorExplorationBatch(None, [])

        pool = await db.get_vector_exploration_pool(
            self.model,
            self.config.pool_limit,
            exclude_ids=exclude_ids,
        )
        scored = []
        for illust_id, embedding in pool:
            similarity = cosine_similarity(user_embedding, embedding)
            if similarity is not None and similarity >= self.config.min_similarity:
                scored.append((illust_id, similarity))
        scored.sort(key=lambda item: (-item[1], -item[0]))

        run_id = uuid.uuid4().hex
        await db.start_vector_exploration_run(
            run_id=run_id,
            user_id=user_id,
            model=self.model,
            profile_hash=current_profile_hash,
            pool_limit=self.config.pool_limit,
            pool_size=len(pool),
            candidate_limit=self.config.candidate_limit,
            similarity_threshold=self.config.min_similarity,
            duplicate_threshold=self.config.duplicate_similarity,
            profile_concentration=preference_profile_concentration(profile),
        )

        # Detail failures are expected for deleted or restricted works. Hydrate
        # a bounded surplus so one failure does not empty the candidate lane.
        hydration_input = scored[: min(len(scored), self.config.candidate_limit * 2)]
        semaphore = asyncio.Semaphore(self.config.detail_concurrency)

        async def load(item: tuple[int, float]):
            async with semaphore:
                try:
                    return item, await self.detail_loader(item[0])
                except Exception as exc:
                    logger.warning(
                        "semantic vector Exploration 详情加载失败: illust_id=%s error=%s",
                        item[0], exc,
                    )
                    return item, None

        loaded = await asyncio.gather(*(load(item) for item in hydration_input))
        candidates: list[Illust] = []
        audit_rows = []
        for (illust_id, similarity), illust in loaded:
            if illust is None:
                await db.delete_illust_embedding(illust_id)
                logger.info(
                    "移除不可用作品的 Embedding 缓存: illust_id=%s", illust_id,
                )
                continue
            illust.source = VECTOR_EXPLORATION_SOURCE
            illust.exploration_only = True
            illust.vector_similarity = similarity
            illust.vector_model = self.model
            illust.vector_exploration_run_id = run_id
            candidates.append(illust)
            audit_rows.append({
                "illust_id": illust_id,
                "source": VECTOR_EXPLORATION_SOURCE,
                "similarity": similarity,
                "model": self.model,
                "retrieval_rank": len(candidates),
                "tags": illust.tags,
            })
            if len(candidates) >= self.config.candidate_limit:
                break

        await db.record_vector_exploration_candidates(run_id, audit_rows)
        logger.info(
            "semantic vector Exploration: pool=%s matched=%s hydrated=%s model=%s run=%s",
            len(pool), len(scored), len(candidates), self.model, run_id,
        )
        return VectorExplorationBatch(
            run_id,
            candidates,
            duplicate_similarity=self.config.duplicate_similarity,
        )
