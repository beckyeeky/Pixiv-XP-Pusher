#!/usr/bin/env python3
"""Inspect and optionally classify high-weight, unclassified Preference Profile tags.

Default mode is read-only. Pass --apply only after reviewing the printed list.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import database as db
from classification_maintenance import ClassificationMaintenance
from config import load_config
from native_jev_judge import (
    native_state_store,
    native_task_lock_for_config,
    refresh_native_runtime_snapshot,
)


async def select_candidates(limit: int, min_weight: float) -> list[dict]:
    """Queue rows still eligible for automatic work.

    Automatically paused tags (a completed attempt without a verdict) and tags
    still inside their short backoff are excluded before the limit is applied,
    so this inspection shows the same batch an automatic run would pick.

    Inspection is strictly read-only and shares one implementation with
    ``preview_native_jev``: it never runs ``init_db``/migrations, so inspecting
    a host that has no database cannot create an empty one.  A missing or
    incompatible database reports no candidates (and exits nothing) instead of
    writing.
    """
    maintenance = ClassificationMaintenance(load_config(PROJECT_ROOT / "config.yaml"))
    try:
        return await maintenance.available_candidates(limit, min_weight)
    except (db.DatabaseNotInitializedError, db.DatabaseSchemaMismatchError) as exc:
        print(f"候选只读检查中止（未创建数据库）：{exc}", file=sys.stderr)
        return []


def import_legacy_pauses(config: dict, *, apply: bool) -> dict:
    """List (and optionally persist) provable historical holds.

    Only a ``tag:`` cooldown whose stored reason is a hold reason is treated as
    evidence of a completed attempt.  No ``unresolved`` row is imported, so the
    existing backlog stays exactly as reviewable as it is today.
    """
    store = native_state_store(config)
    if store is None:
        return {"backend": "other", "imported": [], "applied": False,
                "note": "当前配置未选择 native_jev 后端，暂停状态与该后端无关"}
    lock = native_task_lock_for_config(config)
    acquired = lock.acquire() if lock is not None else True
    if not acquired:
        return {"backend": "native_jev", "imported": [], "applied": False,
                "note": "另一个 Native Jev 任务正在进行，未做任何写入"}
    try:
        refresh_native_runtime_snapshot(config)
        store.reload()  # the lock is held: adopt the newest authored snapshot
        planned = store.import_legacy_holds(dry_run=not apply)
        return {
            "backend": "native_jev",
            "state_path": str(getattr(store, "_path", "")),
            "applied": bool(apply),
            "imported": planned,
            "note": (
                "仅导入 reason 属于 hold 且 key 为 tag:<名称> 的既有冷却记录；"
                "未处理过的历史 unresolved 不会被导入"
            ),
        }
    finally:
        if lock is not None:
            lock.release()


async def apply_candidates(candidates: list[dict], config: dict) -> dict:
    # This is the one writing path of this script: --apply is explicit and the
    # operator is expected to run it on a host whose schema already exists.  A
    # missing database is therefore initialized here (never in the read-only
    # inspection above).
    await db.init_db()
    tags = [item["tag"] for item in candidates]
    return await ClassificationMaintenance(config).run_tags(tags)


def load_reviewed_candidates(path: Path) -> tuple[list[str], dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    candidates = payload.get("candidates", payload) if isinstance(payload, dict) else payload
    if not isinstance(candidates, list):
        raise ValueError("reviewed file must contain a candidates list")
    tags = [item["tag"] if isinstance(item, dict) else item for item in candidates]
    if not all(isinstance(tag, str) and tag for tag in tags):
        raise ValueError("reviewed candidates must contain non-empty tag strings")
    selection = payload.get("selection", {}) if isinstance(payload, dict) else {}
    return list(dict.fromkeys(tags)), selection if isinstance(selection, dict) else {}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=40, help="maximum candidates to list (default: 40)")
    parser.add_argument("--min-weight", type=float, default=1.0, help="minimum absolute profile weight (default: 1.0)")
    parser.add_argument("--output", type=Path, help="write the read-only candidate list as JSON")
    parser.add_argument("--apply", action="store_true", help="persist Grounded Judge classifications for reviewed candidates")
    parser.add_argument("--reviewed-tags", type=Path, help="JSON candidate list explicitly approved for --apply")
    parser.add_argument(
        "--import-legacy-pauses", action="store_true",
        help="list provable historical native holds as automatic pauses (dry run)",
    )
    parser.add_argument(
        "--confirm-import", action="store_true",
        help="persist the listed pauses (requires --import-legacy-pauses)",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict:
    if args.confirm_import and not args.import_legacy_pauses:
        raise ValueError("--confirm-import requires --import-legacy-pauses")
    if args.import_legacy_pauses:
        return {
            "selection": {"limit": args.limit, "min_weight": args.min_weight},
            "import": import_legacy_pauses(
                load_config(PROJECT_ROOT / "config.yaml"), apply=args.confirm_import,
            ),
        }

    selection = {"limit": args.limit, "min_weight": args.min_weight}
    approved: list[str] = []
    if args.apply:
        if not args.reviewed_tags:
            raise ValueError("--apply requires --reviewed-tags from a reviewed inspection output")
        approved, reviewed_selection = load_reviewed_candidates(args.reviewed_tags)
        selection.update({key: reviewed_selection[key] for key in selection if key in reviewed_selection})

    candidates = await select_candidates(selection["limit"], selection["min_weight"])
    result = {"selection": selection, "candidates": candidates}
    if args.output:
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.apply:
        selectable = {item["tag"] for item in candidates}
        stale = [tag for tag in approved if tag not in selectable]
        if stale:
            raise ValueError(f"reviewed tags are no longer eligible: {', '.join(stale)}")
        result["maintenance"] = await apply_candidates(
            [item for item in candidates if item["tag"] in set(approved)],
            load_config(PROJECT_ROOT / "config.yaml"),
        )
    return result


def main(argv=None) -> int:
    args = parse_args(argv)
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
