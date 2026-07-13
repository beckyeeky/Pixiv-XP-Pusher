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
from classification_maintenance import run_scheduled_maintenance
from config import load_config


async def select_candidates(limit: int, min_weight: float) -> list[dict]:
    await db.init_db()
    return await db.get_high_weight_unclassified_profile_tags(limit, min_weight)


async def apply_candidates(candidates: list[dict], config: dict) -> dict:
    tags = [item["tag"] for item in candidates]
    classifier = config.get("tag_classifier") if isinstance(config.get("tag_classifier"), dict) else {}
    maintenance = classifier.get("maintenance") if isinstance(classifier.get("maintenance"), dict) else {}
    concurrency = maintenance.get("concurrency", 10)
    return await run_scheduled_maintenance(tags, config, concurrency=concurrency)


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
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> dict:
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
