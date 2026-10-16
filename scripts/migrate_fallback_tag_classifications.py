#!/usr/bin/env python3
"""Audit and migrate legacy ``feature/fallback`` tag classifications."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import database
from config import get_singleton_provider, load_config
from profiler import XPProfiler
from tag_classification_migration import (
    backup_database,
    migrate_fallback_classifications,
)


class _OfflineClient:
    _logged_in = False

    async def get_bookmarks(self, *args, **kwargs):
        raise RuntimeError("cache-only profile rebuild attempted a network fetch")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "默认只读审计 feature/fallback；--apply 会先备份，再迁为 unresolved，"
            "并只用本地收藏缓存重建画像。"
        )
    )
    parser.add_argument(
        "--database", type=Path, default=PROJECT_ROOT / "data" / "pixiv_xp.db",
    )
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "config.yaml")
    parser.add_argument("--user-id", type=int, help="覆盖 Pixiv Provider user_id")
    parser.add_argument("--sample-limit", type=int, default=20)
    parser.add_argument("--apply", action="store_true")
    return parser


def _user_id(config: dict, explicit: int | None) -> int | None:
    if explicit:
        return int(explicit)
    provider = get_singleton_provider(config, "pixiv") or config.get("pixiv") or {}
    try:
        value = int(provider.get("user_id") or 0)
    except (TypeError, ValueError):
        value = 0
    return value or None


async def _rebuild_cached_profile(
    db_path: Path,
    config: dict,
    user_id: int,
) -> dict:
    original_path = database.DB_PATH
    database.DB_PATH = db_path
    try:
        profiler_cfg = config.get("profiler") or {}
        profiler = XPProfiler(
            client=_OfflineClient(),
            stop_words=profiler_cfg.get("stop_words"),
            discovery_rate=profiler_cfg.get("discovery_rate", 0.1),
            time_decay_days=profiler_cfg.get("time_decay_days", 180),
            saturation_threshold=profiler_cfg.get("saturation_threshold", 0.5),
            ip_tags=profiler_cfg.get("ip_tags") or profiler_cfg.get("ip_tags_file"),
            ip_weight_discount=profiler_cfg.get("ip_weight_discount", 1.0),
            boost_tags=profiler_cfg.get("boost_tags"),
        )
        profile = await profiler.build_profile(
            user_id=user_id,
            scan_limit=0,
            include_private=False,
            cache_only=True,
        )
        return {"user_id": user_id, "profile_tags": len(profile), "cache_only": True}
    finally:
        database.DB_PATH = original_path


def main() -> int:
    args = build_parser().parse_args()
    db_path = args.database.expanduser().resolve()
    config = load_config(args.config)
    report = migrate_fallback_classifications(
        db_path,
        apply=False,
        sample_limit=args.sample_limit,
    )
    if not args.apply:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    user_id = _user_id(config, args.user_id)
    if report["before"]["fallback_feature_count"] and not user_id:
        raise SystemExit(
            "--apply requires a Pixiv user_id so cached preference artifacts can be rebuilt"
        )
    backup_path = backup_database(db_path)
    original_path = database.DB_PATH
    database.DB_PATH = db_path
    try:
        database._init_db_sync()
    finally:
        database.DB_PATH = original_path
    applied = migrate_fallback_classifications(
        db_path,
        apply=True,
        sample_limit=args.sample_limit,
    )
    applied["backup"] = str(backup_path)
    if applied["updated_rows"]:
        applied["profile_rebuild"] = asyncio.run(
            _rebuild_cached_profile(db_path, config, int(user_id))
        )
    print(json.dumps(applied, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
