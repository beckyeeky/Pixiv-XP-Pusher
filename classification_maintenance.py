"""Shared one-tag Grounded Judge maintenance operations."""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

import database as db
from grounded_judge import classify_single_tag, validate_ai_classification_record


USAGE_KEYS = ("input", "output", "thoughts", "tool_use_prompt", "total", "search_queries")


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_KEYS}


async def classify_and_activate_tag(tag: str, config: dict) -> dict:
    """Run the exact Grounded Judge operation used by one manual review."""
    translation = await db.get_translated_tag(tag)
    result = await classify_single_tag(tag, translation, config)
    result = validate_ai_classification_record(result, tag)
    activated = await db.activate_ai_tag_classification(
        result["tag"], result["classification"], result["explanation"], result["languages"],
    )
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
    summary = {"attempted": len(tags), "accepted": 0, "unresolved": 0, "failed": 0,
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
        except ValueError as exc:
            await db.mark_ai_tag_unresolved(tag)
            item = {"tag": tag, "status": "unresolved", "error": str(exc),
                    "usage": getattr(exc, "usage", {})}
        except Exception as exc:
            await db.mark_ai_tag_unresolved(tag)
            item = {"tag": tag, "status": "failed", "error": str(exc)}
        status = item.get("status", "failed")
        if status not in ("accepted", "unresolved", "failed", "human_override"):
            status = "failed"
            item["status"] = status
        summary[status] += 1
        usage = item.get("usage") or {}
        for key in USAGE_KEYS:
            summary["usage"][key] += int(usage.get(key) or 0)
        summary["items"].append(item)
    return summary
