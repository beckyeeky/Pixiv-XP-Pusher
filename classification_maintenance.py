"""Shared one-tag Grounded Judge maintenance operations."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Awaitable, Callable

import database as db
from grounded_judge import (
    GroundedJudgeDeferredError,
    classify_single_tag,
    validate_ai_classification_record,
)
from tag_categories import TAG_CATEGORY_UNRESOLVED
from utils import normalize_tag


USAGE_KEYS = ("input", "output", "thoughts", "tool_use_prompt", "total", "search_queries")
SUMMARY_STATE_KEY = "runtime.last_classification_maintenance_summary"
GROUNDING_TEXT_FIELDS = ("classifier_model", "search_provider", "search_pool_id")


def _bounded_text(value, limit: int) -> str:
    return str(value or "").strip()[:limit]


def grounding_provenance(result: dict) -> dict:
    """Build the bounded, non-secret provenance stored on one AI decision."""
    if not isinstance(result, dict) or not any(
        result.get(key) for key in (*GROUNDING_TEXT_FIELDS, "source_urls", "evidence_excerpt", "search_trace")
    ):
        return {}
    provenance = {"schema_version": 1, "backend": "search_first"}
    for key in GROUNDING_TEXT_FIELDS:
        value = _bounded_text(result.get(key), 200)
        if value:
            provenance[key] = value
    provenance["source_urls"] = [
        value for item in (result.get("source_urls") or [])[:5]
        if (value := _bounded_text(item, 1000))
    ]
    provenance["evidence_excerpt"] = [
        value for item in (result.get("evidence_excerpt") or [])[:3]
        if (value := _bounded_text(item, 500))
    ]
    provenance["search_trace"] = []
    for item in (result.get("search_trace") or [])[:4]:
        if not isinstance(item, dict):
            continue
        trace = {}
        for key in ("provider", "outcome", "pool_id"):
            value = _bounded_text(item.get(key), 200)
            if value:
                trace[key] = value
        if trace:
            provenance["search_trace"].append(trace)
    usage = {}
    raw_usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    for key in USAGE_KEYS:
        if key not in raw_usage:
            continue
        try:
            usage[key] = max(0, int(raw_usage[key]))
        except (TypeError, ValueError):
            continue
    provenance["usage"] = usage
    return provenance


def _positive_int(value, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class MaintenancePolicy:
    max_tags_per_run: int = 40
    min_profile_weight: float = 0.0
    prefer_unresolved_first: bool = True
    concurrency: int = 10

    @classmethod
    def from_config(cls, config: dict) -> "MaintenancePolicy":
        classifier = config.get("tag_classifier") if isinstance(config, dict) else {}
        classifier = classifier if isinstance(classifier, dict) else {}
        maintenance = classifier.get("maintenance")
        maintenance = maintenance if isinstance(maintenance, dict) else {}
        try:
            minimum = float(maintenance.get("min_profile_weight", 0.0) or 0.0)
        except (TypeError, ValueError):
            minimum = 0.0
        return cls(
            max_tags_per_run=_positive_int(maintenance.get("max_tags_per_run", 40), 40),
            min_profile_weight=minimum,
            prefer_unresolved_first=bool(
                maintenance.get("prefer_unresolved_first", True)
            ),
            concurrency=_positive_int(maintenance.get("concurrency", 10), 10),
        )


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


async def classify_and_activate_tag(tag: str, config: dict) -> dict:
    """Run the exact Grounded Judge operation used by one manual review."""
    translation = await db.get_translated_tag(tag)
    raw_result = await classify_single_tag(tag, translation, config)
    result = validate_ai_classification_record(raw_result, tag)
    provenance = grounding_provenance(raw_result)
    activated = await db.activate_ai_tag_classification(
        result["tag"], result["classification"], result["explanation"], result["languages"],
        grounding_provenance=provenance,
    )
    if isinstance(raw_result.get("usage"), dict):
        result["usage"] = raw_result["usage"]
    if not activated:
        return {**result, "status": "human_override"}
    return {**result, "status": "accepted"}


async def run_scheduled_maintenance(
    tags: list[str], config: dict,
    classify: Callable[[str, dict], Awaitable[dict]] = classify_and_activate_tag,
    concurrency: int = 10,
) -> dict:
    """Classify selected tags concurrently and retain a reviewable outcome for every tag."""
    try:
        concurrency = max(1, int(concurrency))
    except (TypeError, ValueError):
        concurrency = 10
    summary = {"attempted": len(tags), "accepted": 0, "unresolved": 0, "deferred": 0, "failed": 0,
               "human_override": 0, "usage": empty_usage(), "items": []}
    semaphore = asyncio.Semaphore(concurrency)

    async def classify_one(tag: str) -> dict:
        async with semaphore:
            return await classify(tag, config)

    results = await asyncio.gather(
        *(classify_one(tag) for tag in tags), return_exceptions=True
    )
    for tag, result in zip(tags, results):
        try:
            if isinstance(result, Exception):
                raise result
            item = result
        except GroundedJudgeDeferredError as exc:
            item = {"tag": tag, "status": "deferred", "error": str(exc),
                    "usage": getattr(exc, "usage", {})}
        except ValueError as exc:
            await db.mark_ai_tag_unresolved(tag)
            item = {"tag": tag, "status": "unresolved", "error": str(exc),
                    "usage": getattr(exc, "usage", {})}
        except Exception as exc:
            await db.mark_ai_tag_unresolved(tag)
            item = {"tag": tag, "status": "failed", "error": str(exc)}
        status = item.get("status", "failed")
        if status not in ("accepted", "unresolved", "deferred", "failed", "human_override"):
            status = "failed"
            item["status"] = status
        summary[status] += 1
        usage = item.get("usage") or {}
        for key in USAGE_KEYS:
            summary["usage"][key] += int(usage.get(key) or 0)
        summary["items"].append(item)
    return summary


class ClassificationMaintenance:
    """Own one complete bounded Classification Maintenance lifecycle."""

    def __init__(
        self,
        config: dict,
        *,
        classify: Callable[[str, dict], Awaitable[dict]] = classify_and_activate_tag,
        database_module=db,
    ):
        self.config = config if isinstance(config, dict) else {}
        self.policy = MaintenancePolicy.from_config(self.config)
        self._classify = classify
        self._db = database_module

    async def classify_tag(self, tag: str) -> dict:
        normalized = normalize_tag(tag)
        if not normalized:
            raise ValueError("必须提供有效标签")
        return await self._classify(normalized, self.config)

    async def preview(self, limit: int | None = None) -> list[dict]:
        effective_limit = self._effective_limit(limit)
        return await self._db.get_high_weight_unclassified_profile_tags(
            limit=effective_limit,
            min_profile_weight=self.policy.min_profile_weight,
        )

    async def run_eligible(self, limit: int | None = None) -> dict:
        candidates = await self.preview(limit)
        summary = await self.run_tags(item["tag"] for item in candidates)
        effective_limit = self._effective_limit(limit)
        return {
            **summary,
            "requested_limit": limit if isinstance(limit, int) else effective_limit,
            "effective_limit": effective_limit,
            "configured_limit": self.policy.max_tags_per_run,
            "min_profile_weight": self.policy.min_profile_weight,
        }

    async def run_reviewed(
        self,
        tags: list[str],
        *,
        limit: int | None = None,
    ) -> dict:
        approved = list(dict.fromkeys(
            normalized for tag in tags if (normalized := normalize_tag(tag))
        ))
        current = await self.preview(limit)
        current_tags = {item["tag"] for item in current}
        stale = [tag for tag in approved if tag not in current_tags]
        if stale:
            raise ValueError(
                f"reviewed tags are no longer eligible: {', '.join(stale)}"
            )
        return await self.run_tags(approved)

    async def run_profile(self, profile: list[str] | dict[str, float]) -> dict:
        selected = await self._select_profile_tags(profile)
        return await self.run_tags(selected)

    async def run_tags(self, tags) -> dict:
        selected = list(dict.fromkeys(
            normalized for tag in tags if (normalized := normalize_tag(tag))
        ))[:self.policy.max_tags_per_run]
        summary = await run_scheduled_maintenance(
            selected,
            self.config,
            self._classify,
            concurrency=self.policy.concurrency,
        )
        await self._db.set_state(
            SUMMARY_STATE_KEY,
            json.dumps(summary, ensure_ascii=False),
        )
        return summary

    async def _select_profile_tags(
        self,
        profile: list[str] | dict[str, float],
    ) -> list[str]:
        if not isinstance(profile, dict):
            return list(dict.fromkeys(
                normalized
                for tag in profile
                if (normalized := normalize_tag(tag))
            ))[:self.policy.max_tags_per_run]

        normalized_profile = {
            normalized: float(weight)
            for tag, weight in profile.items()
            if (normalized := normalize_tag(tag))
        }
        candidates = [
            tag
            for tag, weight in normalized_profile.items()
            if abs(weight) >= self.policy.min_profile_weight
        ]
        cached = await self._db.get_tag_classifications(
            candidates,
            ttl_days=self._tag_ttl_days(),
        )

        def priority(tag: str):
            unresolved = (
                cached.get(tag, {}).get("classification")
                == TAG_CATEGORY_UNRESOLVED
            )
            return (
                0 if self.policy.prefer_unresolved_first and unresolved else 1,
                -abs(normalized_profile[tag]),
                tag,
            )

        return sorted(candidates, key=priority)[:self.policy.max_tags_per_run]

    def _effective_limit(self, requested: int | None) -> int:
        if requested is None:
            return self.policy.max_tags_per_run
        return min(self.policy.max_tags_per_run, _positive_int(requested, 1))

    def _tag_ttl_days(self) -> int:
        classifier = self.config.get("tag_classifier")
        classifier = classifier if isinstance(classifier, dict) else {}
        return _positive_int(classifier.get("ttl_days", 30), 30)
